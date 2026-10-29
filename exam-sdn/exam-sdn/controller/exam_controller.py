"""Ryu (OpenFlow 1.3) controller for the Online Examination Network.

Run (note --observe-links, needed for topology discovery):
  EXAM_MODE=sdn      ryu-manager --observe-links controller/exam_controller.py
  EXAM_MODE=baseline ryu-manager --observe-links controller/exam_controller.py

Dynamic SDN behaviour (mode=sdn):
  * Routing      - LLDP topology discovery, shortest-path flows, loop-free flooding tree.
  * Monitoring   - port/flow statistics every EXAM_POLL s -> results/*.csv, events -> sdn_events.csv.
  * Prioritising - exam traffic (TCP 5000) gets its own high-priority flows; when a port exceeds
                   EXAM_CONGESTION_MBPS for 2 polls, all other traffic is rate-limited by an
                   OpenFlow meter (EXAM_BG_LIMIT_KBPS) until the load is gone (hysteresis).
  * Recovery     - on link loss, flows are flushed and re-routed over the surviving path.
  * Blocking     - the exam server reports UNAUTHORIZED logins over UDP; the host is dropped.
Baseline mode: same forwarding, but no exam class, no policing, no blocking, no reroute on failure.
"""
import csv
import json
import os
import socket
import sys
import time

from ryu.base import app_manager
from ryu.controller import ofp_event
from ryu.controller.handler import CONFIG_DISPATCHER, MAIN_DISPATCHER, DEAD_DISPATCHER, set_ev_cls
from ryu.lib import hub
from ryu.lib.packet import packet, ethernet, ipv4, tcp, ether_types
from ryu.ofproto import ofproto_v1_3
from ryu.topology import event as topo_event

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from netgraph import Graph  # noqa: E402

EXAM_PORT = 5000
PRIO_MISS, PRIO_L2, PRIO_EXAM, PRIO_BLOCK = 0, 10, 20, 300
COOKIE_PATH, COOKIE_BLOCK, METER_ID = 0x100, 0x300, 1
ALL = 0xFFFFFFFFFFFFFFFF


class ExamController(app_manager.RyuApp):
    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        env = os.environ.get
        self.mode = env("EXAM_MODE", "sdn")
        self.sdn = self.mode == "sdn"
        self.ctrl_port = int(env("EXAM_CTRL_PORT", "9999"))
        self.out_dir = env("EXAM_RESULTS_DIR", "results")
        self.poll = float(env("EXAM_POLL", "2"))
        self.thresh = float(env("EXAM_CONGESTION_MBPS", "8"))
        self.bg_kbps = int(env("EXAM_BG_LIMIT_KBPS", "2000"))
        self.hold = float(env("EXAM_POLICE_HOLD", "15"))
        os.makedirs(self.out_dir, exist_ok=True)

        self.graph, self.dps, self.host_loc = Graph(), {}, {}
        self.blocked, self.logins, self.exam_active = set(), {}, False
        self.prev, self.port_mbps, self.drop_prev, self.drop_delta = {}, {}, {}, {}
        self.meters = set()
        self.policing, self.hot, self.cool, self.police_since = False, 0, 0, 0.0
        self.t0, self.warned = time.time(), False

        self.port_f, self.port_w = self._csv("port_stats.csv", ["ts", "dpid", "port", "rx_bytes", "tx_bytes", "tx_mbps", "tx_dropped"])
        self.flow_f, self.flow_w = self._csv("flow_stats.csv", ["ts", "dpid", "exam_bytes", "other_bytes"])
        self.ev_f, self.ev_w = self._csv("sdn_events.csv", ["ts", "event", "detail"])
        self.logger.info("mode=%s listener=udp/%d out=%s", self.mode, self.ctrl_port, self.out_dir)
        hub.spawn(self._monitor)
        hub.spawn(self._listener)

    def _csv(self, name, header):
        f = open(os.path.join(self.out_dir, name), "w", newline="")
        w = csv.writer(f)
        w.writerow(header)
        return f, w

    def event(self, kind, detail=""):
        self.logger.info("EVENT %s %s", kind, detail)
        self.ev_w.writerow([round(time.time(), 3), kind, detail])
        self.ev_f.flush()

    # ------------------------------------------------------------ switches
    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
    def _features(self, ev):
        dp = ev.msg.datapath
        ofp, parser = dp.ofproto, dp.ofproto_parser
        self._add_flow(dp, PRIO_MISS, parser.OFPMatch(), [parser.OFPActionOutput(ofp.OFPP_CONTROLLER, ofp.OFPCML_NO_BUFFER)])
        for ip in self.blocked:
            self._block_on(dp, ip)

    @set_ev_cls(ofp_event.EventOFPStateChange, [MAIN_DISPATCHER, DEAD_DISPATCHER])
    def _state(self, ev):
        dp = ev.datapath
        if ev.state == MAIN_DISPATCHER:
            self.dps[dp.id] = dp
            if self.policing:
                self._add_meter(dp)
        else:
            self.dps.pop(dp.id, None)
            self.meters.discard(dp.id)

    @set_ev_cls(ofp_event.EventOFPPortStatus, MAIN_DISPATCHER)
    def _port_status(self, ev):
        m, dp = ev.msg, ev.msg.datapath
        down = bool(m.desc.state & dp.ofproto.OFPPS_LINK_DOWN)
        self.logger.info("PORT_STATUS dpid=%s port=%s link=%s", dp.id, m.desc.port_no, "DOWN" if down else "UP")

    @set_ev_cls(ofp_event.EventOFPErrorMsg, MAIN_DISPATCHER)
    def _error(self, ev):
        self.logger.warning("OpenFlow error dpid=%s type=%s code=%s", ev.msg.datapath.id, ev.msg.type, ev.msg.code)

    def _add_flow(self, dp, prio, match, actions, idle=0, cookie=0, meter=False):
        ofp, parser = dp.ofproto, dp.ofproto_parser
        inst = [parser.OFPInstructionMeter(METER_ID)] if meter else []
        inst.append(parser.OFPInstructionActions(ofp.OFPIT_APPLY_ACTIONS, actions))
        dp.send_msg(parser.OFPFlowMod(datapath=dp, cookie=cookie, priority=prio, match=match,
                                      instructions=inst, idle_timeout=idle))

    # ------------------------------------------------- topology / recovery
    @set_ev_cls(topo_event.EventLinkAdd)
    def _link_add(self, ev):
        s, d = ev.link.src, ev.link.dst
        if self.graph.add_link(s.dpid, s.port_no, d.dpid, d.port_no):
            self.event("LINK_UP", "s%s-s%s" % (s.dpid, d.dpid))
            if self.sdn:
                self._flush()          # let traffic return to the best path

    @set_ev_cls(topo_event.EventLinkDelete)
    def _link_del(self, ev):
        s, d = ev.link.src, ev.link.dst
        if self.graph.del_link(s.dpid, d.dpid):
            self.event("LINK_DOWN", "s%s-s%s" % (s.dpid, d.dpid))
            if self.sdn:
                self._flush()
                self.event("REROUTE", "flows flushed, re-routing over surviving links")

    def _flush(self):
        """Delete every learned path flow so traffic is re-routed via Packet-In."""
        for dp in list(self.dps.values()):
            o, p = dp.ofproto, dp.ofproto_parser
            dp.send_msg(p.OFPFlowMod(datapath=dp, cookie=COOKIE_PATH, cookie_mask=ALL, table_id=o.OFPTT_ALL,
                                     command=o.OFPFC_DELETE, out_port=o.OFPP_ANY, out_group=o.OFPG_ANY,
                                     match=p.OFPMatch()))

    # ------------------------------------------------------------ packet-in
    @set_ev_cls(ofp_event.EventOFPPacketIn, MAIN_DISPATCHER)
    def _packet_in(self, ev):
        msg = ev.msg
        dp = msg.datapath
        in_port = msg.match["in_port"]
        pkt = packet.Packet(msg.data)
        eth = pkt.get_protocols(ethernet.ethernet)[0]
        if eth.ethertype == ether_types.ETH_TYPE_LLDP or not self.graph.has_links():
            return                               # LLDP is handled by ryu.topology; wait for discovery
        ip = pkt.get_protocol(ipv4.ipv4)
        if self.sdn and ip and ip.src in self.blocked:
            return
        if not self.graph.is_link_port(dp.id, in_port):
            self.host_loc[eth.src] = (dp.id, in_port)      # only edge ports locate hosts
        dst = self.host_loc.get(eth.dst)
        if dst is None or int(eth.dst.split(":")[0], 16) & 1:
            return self._flood(dp, in_port, msg.data)
        path = self.graph.path(dp.id, dst[0])
        if path is None:
            return
        t = pkt.get_protocol(tcp.tcp)
        exam = bool(self.sdn and ip and t and EXAM_PORT in (t.src_port, t.dst_port))
        fields = dict(eth_src=eth.src, eth_dst=eth.dst)
        if exam:
            fields.update(eth_type=ether_types.ETH_TYPE_IP, ip_proto=6)
            fields["tcp_dst" if t.dst_port == EXAM_PORT else "tcp_src"] = EXAM_PORT
        hops = self.graph.hops(path, dst[1])
        self._install(hops, fields, exam)
        p = dp.ofproto_parser
        dp.send_msg(p.OFPPacketOut(datapath=dp, buffer_id=dp.ofproto.OFP_NO_BUFFER, in_port=in_port,
                                   actions=[p.OFPActionOutput(hops[0][1])], data=msg.data))

    def _install(self, hops, fields, exam):
        for dpid, out_port in hops:
            dp = self.dps.get(dpid)
            if dp is None:
                continue
            p = dp.ofproto_parser
            self._add_flow(dp, PRIO_EXAM if exam else PRIO_L2, p.OFPMatch(**fields), [p.OFPActionOutput(out_port)],
                           idle=30, cookie=COOKIE_PATH, meter=self.policing and not exam)

    def _flood(self, dp, in_port, data):
        ofp, p = dp.ofproto, dp.ofproto_parser
        links, tree = self.graph.link_ports(dp.id), self.graph.tree_ports(dp.id)
        acts = [p.OFPActionOutput(x) for x in dp.ports
                if x < ofp.OFPP_MAX and x != in_port and (x not in links or x in tree)]
        if acts:
            dp.send_msg(p.OFPPacketOut(datapath=dp, buffer_id=ofp.OFP_NO_BUFFER, in_port=in_port, actions=acts, data=data))

    # --------------------------------------------- app -> SDN events (UDP)
    def _listener(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.bind(("0.0.0.0", self.ctrl_port))
        while True:
            data, addr = s.recvfrom(4096)
            try:
                self._on_event(json.loads(data.decode()))
            except Exception as e:
                self.logger.warning("bad control message from %s: %s", addr, e)

    def _on_event(self, ev):
        kind = ev.get("event")
        self.logger.info("APP EVENT %s", ev)
        self.event("APP_" + str(kind), json.dumps(ev))
        if kind == "START":
            self.exam_active = True
        elif kind == "LOGIN":
            self.logins[ev["ip"]] = ev.get("student_id")
        elif kind == "UNAUTHORIZED" and self.sdn:
            self._block(ev["ip"])
        elif kind == "END":
            self.exam_active = False

    def _block(self, ip):
        if ip in self.blocked:
            return
        self.blocked.add(ip)
        for dp in list(self.dps.values()):
            self._block_on(dp, ip)
        self.logger.info("BLOCKED %s on %d switches", ip, len(self.dps))
        self.event("BLOCKED", ip)

    def _block_on(self, dp, ip):
        m = dp.ofproto_parser.OFPMatch(eth_type=ether_types.ETH_TYPE_IP, ipv4_src=ip)
        self._add_flow(dp, PRIO_BLOCK, m, [], cookie=COOKIE_BLOCK)

    # --------------------------------------------- monitoring + congestion
    def _monitor(self):
        while True:
            hub.sleep(self.poll)
            self._decide()
            for dp in list(self.dps.values()):
                ofp, p = dp.ofproto, dp.ofproto_parser
                dp.send_msg(p.OFPPortStatsRequest(dp, 0, ofp.OFPP_ANY))
                dp.send_msg(p.OFPFlowStatsRequest(dp))
                if dp.id in self.meters:
                    dp.send_msg(p.OFPMeterStatsRequest(dp, 0, ofp.OFPM_ALL))

    def _check_topology(self):
        if not self.warned and not self.graph.has_links() and len(self.dps) >= 2 and time.time() - self.t0 > 20:
            self.warned = True
            self.logger.error("No links discovered. Start with: ryu-manager --observe-links controller/exam_controller.py")

    def _decide(self):
        self._check_topology()
        if not self.sdn:
            return
        peak = max(self.port_mbps.values(), default=0.0)
        dropping = any(v > 2000 for v in self.drop_delta.values())   # meter still discarding bytes
        if not self.policing:
            self.hot = self.hot + 1 if peak > self.thresh else 0
            if self.hot >= 2:
                self.hot = 0
                self._police_on(peak)
        else:
            quiet = time.time() - self.police_since >= self.hold and not dropping and peak < self.thresh
            self.cool = self.cool + 1 if quiet else 0
            if self.cool >= 2:
                self._police_off()

    def _police_on(self, peak):
        self.policing, self.police_since, self.cool = True, time.time(), 0
        for dp in list(self.dps.values()):
            self._add_meter(dp)
        self._flush()
        self.logger.warning("CONGESTION peak=%.1f Mbps -> limiting non-exam traffic to %d kbps per switch", peak, self.bg_kbps)
        self.event("POLICE_ON", "peak_mbps=%.1f bg_limit_kbps=%d" % (peak, self.bg_kbps))

    def _police_off(self):
        self.policing = False
        self._flush()
        self.event("POLICE_OFF", "load cleared, background limit removed")

    def _add_meter(self, dp):
        if dp.id in self.meters:
            return
        o, p = dp.ofproto, dp.ofproto_parser
        band = p.OFPMeterBandDrop(rate=self.bg_kbps, burst_size=max(10, self.bg_kbps // 10))
        dp.send_msg(p.OFPMeterMod(datapath=dp, command=o.OFPMC_ADD,
                                  flags=o.OFPMF_KBPS | o.OFPMF_BURST | o.OFPMF_STATS,
                                  meter_id=METER_ID, bands=[band]))
        self.meters.add(dp.id)

    @set_ev_cls(ofp_event.EventOFPMeterStatsReply, MAIN_DISPATCHER)
    def _meter_stats(self, ev):
        dpid = ev.msg.datapath.id
        for m in ev.msg.body:
            if m.meter_id == METER_ID:
                dropped = sum(b.byte_band_count for b in m.band_stats)
                self.drop_delta[dpid] = dropped - self.drop_prev.get(dpid, dropped)
                self.drop_prev[dpid] = dropped

    @set_ev_cls(ofp_event.EventOFPPortStatsReply, MAIN_DISPATCHER)
    def _port_stats(self, ev):
        dp, now = ev.msg.datapath, time.time()
        for st in ev.msg.body:
            if st.port_no > dp.ofproto.OFPP_MAX:
                continue
            key = (dp.id, st.port_no)
            prev = self.prev.get(key)
            self.prev[key] = (now, st.tx_bytes)
            mbps = (st.tx_bytes - prev[1]) * 8 / (now - prev[0]) / 1e6 if prev and now > prev[0] else 0.0
            self.port_mbps[key] = mbps
            self.port_w.writerow([round(now, 3), dp.id, st.port_no, st.rx_bytes, st.tx_bytes, round(mbps, 3), st.tx_dropped])
        self.port_f.flush()

    @set_ev_cls(ofp_event.EventOFPFlowStatsReply, MAIN_DISPATCHER)
    def _flow_stats(self, ev):
        exam = sum(s.byte_count for s in ev.msg.body if s.priority == PRIO_EXAM)
        other = sum(s.byte_count for s in ev.msg.body if s.priority == PRIO_L2)
        self.flow_w.writerow([round(time.time(), 3), ev.msg.datapath.id, exam, other])
        self.flow_f.flush()

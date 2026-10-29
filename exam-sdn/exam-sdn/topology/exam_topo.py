#!/usr/bin/env python3
"""Mininet topology for the Online Examination Network.

                 h1-h4,h11 -- s2 --\\            /-- h9 (exam server), h12 (bg sink)
                                     s1 --[bottleneck]-- s4
                 h5-h8,h10 -- s3 --/    \\__ s5 __/        (only with --redundant)

h1-h8 students, h9 exam server, h10 background-traffic source (iperf),
h11 intruder (not on roster), h12 background sink. Hosts are 10.0.0.<N>/8.
s1 also gets a link to the root namespace (10.123.123.1) so the exam server
can send UDP events to the controller, which runs on the machine itself.

NOTE: --redundant creates a loop; the D1 controller floods and would storm.
Use it only after adding path computation (D2).
"""
import argparse
from functools import partial

from mininet.cli import CLI
from mininet.link import Link, TCLink
from mininet.log import setLogLevel
from mininet.net import Mininet
from mininet.node import Node, OVSSwitch, RemoteController
from mininet.topo import Topo

SERVER_IP = "10.0.0.9"
ROOT_IP = "10.123.123.1"
STUDENT_HOSTS = ["h%d" % i for i in range(1, 9)]


class ExamTopo(Topo):
    def build(self, redundant=False, bottleneck=10, access=100):
        s = {i: self.addSwitch("s%d" % i) for i in range(1, 5 + (1 if redundant else 0))}
        for i in range(1, 13):
            self.addHost("h%d" % i, ip="10.0.0.%d/8" % i)
        acc = dict(bw=access, delay="1ms")
        bn = dict(bw=bottleneck, delay="5ms", max_queue_size=100)
        for h in ("h1", "h2", "h3", "h4", "h11"):
            self.addLink(h, s[2], **acc)
        for h in ("h5", "h6", "h7", "h8", "h10"):
            self.addLink(h, s[3], **acc)
        for h in ("h9", "h12"):
            self.addLink(h, s[4], **acc)
        self.addLink(s[1], s[2], **acc)
        self.addLink(s[1], s[3], **acc)
        self.addLink(s[1], s[4], **bn)               # shared bottleneck
        if redundant:
            self.addLink(s[1], s[5], **bn)
            self.addLink(s[5], s[4], **bn)


def make_net(controller_ip="127.0.0.1", controller_port=6653, redundant=False, bottleneck=10):
    net = Mininet(topo=ExamTopo(redundant=redundant, bottleneck=bottleneck),
                  switch=partial(OVSSwitch, protocols="OpenFlow13"),
                  controller=None, link=TCLink, autoSetMacs=True)
    net.addController("c0", controller=RemoteController, ip=controller_ip, port=controller_port)
    root = Node("root", inNamespace=False)
    link = net.addLink(root, net.get("s1"), cls=Link)
    return net, root, link


def start(net, root, link):
    net.start()
    root.setIP(ROOT_IP, prefixLen=8, intf=link.intf1)


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--controller-ip", default="127.0.0.1")
    p.add_argument("--controller-port", type=int, default=6653)
    p.add_argument("--redundant", action="store_true")
    p.add_argument("--bottleneck", type=float, default=10, help="s1-s4 bandwidth (Mbps)")
    a = p.parse_args()
    setLogLevel("info")
    net, root, link = make_net(a.controller_ip, a.controller_port, a.redundant, a.bottleneck)
    start(net, root, link)
    CLI(net)
    net.stop()

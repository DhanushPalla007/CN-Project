#!/usr/bin/env python3
"""Run one experiment: N students (+ optional UDP background load) in Mininet.

Start the controller first (ryu-manager --observe-links ...), then:
  sudo python3 tests/run_experiment.py --students 10 --bg 0  --label sdn_n10_bg0
  sudo python3 tests/run_experiment.py --students 10 --bg 12 --label sdn_n10_bg12
Link failure:  sudo python3 tests/run_experiment.py --students 10 --redundant --fail-at 20 --label sdn_fail
For the baseline, restart the controller with EXAM_MODE=baseline and use label baseline_*.
Outputs go to results/<label>/ (client_*.csv, results.csv, server.log).
"""
import argparse
import os
import subprocess
import sys
import threading
import time

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)
from mininet.log import setLogLevel  # noqa: E402
from topology.exam_topo import make_net, start, STUDENT_HOSTS, SERVER_IP, ROOT_IP  # noqa: E402


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--students", type=int, default=10)
    p.add_argument("--bg", type=float, default=0, help="UDP background load (Mbps) h10->h12")
    p.add_argument("--duration", type=float, default=60)
    p.add_argument("--think", type=float, default=1.0)
    p.add_argument("--bottleneck", type=float, default=10)
    p.add_argument("--intruder", action="store_true", help="h11 tries to log in (not on roster)")
    p.add_argument("--label", required=True)
    p.add_argument("--redundant", action="store_true", help="add the backup path s1-s5-s4")
    p.add_argument("--fail-at", type=float, default=0, help="seconds after students start: take link s1-s4 down (needs --redundant)")
    p.add_argument("--restore-after", type=float, default=0, help="bring s1-s4 back up this many seconds later")
    a = p.parse_args()
    if a.fail_at and not a.redundant:
        p.error("--fail-at needs --redundant")

    out = os.path.join(ROOT, "results", a.label)
    os.makedirs(out, exist_ok=True)
    setLogLevel("warning")
    net, root, link = make_net(redundant=a.redundant, bottleneck=a.bottleneck)
    start(net, root, link)
    procs = []
    try:
        h = {n: net.get(n) for n in ["h%d" % i for i in range(1, 13)]}
        time.sleep(8)  # switches connect + LLDP topology discovery
        h["h9"].cmd("ping -c 2 -W 1 %s" % ROOT_IP)     # warm ARP/flows to controller

        srv = h["h9"].popen(
            ["python3", os.path.join(ROOT, "server/exam_server.py"),
             "--roster", os.path.join(ROOT, "data/roster.txt"),
             "--expected", str(a.students), "--lobby", "20", "--duration", str(a.duration),
             "--controller", "%s:9999" % ROOT_IP,
             "--results", os.path.join(out, "results.csv")],
            stdout=open(os.path.join(out, "server.log"), "w"), stderr=subprocess.STDOUT)
        time.sleep(1)

        if a.bg > 0:
            procs.append(h["h12"].popen(["iperf", "-s", "-u"], stdout=subprocess.DEVNULL))
            time.sleep(1)
            procs.append(h["h10"].popen(["iperf", "-c", h["h12"].IP(), "-u", "-b", "%gM" % a.bg,
                                         "-t", str(int(a.duration + 40))],
                                        stdout=subprocess.DEVNULL))
            time.sleep(3)                              # let congestion build

        clients = []
        for i in range(1, a.students + 1):
            sid = "S%03d" % i
            host = h[STUDENT_HOSTS[(i - 1) % len(STUDENT_HOSTS)]]
            clients.append(host.popen(
                ["python3", os.path.join(ROOT, "client/student_client.py"),
                 "--server", SERVER_IP, "--id", sid, "--name", "Student %s" % sid, "--think", str(a.think),
                 "--log", os.path.join(out, "client_%s.csv" % sid)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
            time.sleep(0.05)
        if a.fail_at:
            def link(state):
                net.configLinkStatus("s1", "s4", state)
                print("link s1-s4", state.upper(), "at", time.strftime("%H:%M:%S"), flush=True)
            for delay, state in ((a.fail_at, "down"), (a.fail_at + a.restore_after, "up")):
                if state == "down" or a.restore_after:
                    t = threading.Timer(delay, link, [state]); t.daemon = True; t.start()
        if a.intruder:
            time.sleep(2)
            clients.append(h["h11"].popen(
                ["python3", os.path.join(ROOT, "client/student_client.py"), "--server", SERVER_IP,
                 "--id", "INTRUDER", "--retries", "0", "--timeout", "5",
                 "--log", os.path.join(out, "client_INTRUDER.csv")]))
            time.sleep(3)
            print("intruder connectivity after block:\n",
                  h["h11"].cmd("nc -zv -w 2 %s 5000 2>&1" % SERVER_IP))

        srv.wait(timeout=20 + a.duration + 60)
        for c in clients:
            c.wait(timeout=30)
        print("done ->", out)
    finally:
        for pr in procs:
            pr.terminate()
        net.stop()


if __name__ == "__main__":
    main()

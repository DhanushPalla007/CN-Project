#!/usr/bin/env python3
"""Web exam inside Mininet: exam server on h9, web gateways on h1-h4, then the Mininet CLI.
Start the controller first, then:  sudo python3 tests/web_demo.py
Open http://10.0.0.1:8080 (student on h1), http://10.0.0.2:8080 (h2), ... in Firefox inside the VM."""
import os, subprocess, sys, time
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)
from mininet.cli import CLI
from mininet.log import setLogLevel
from topology.exam_topo import make_net, start, SERVER_IP, ROOT_IP

setLogLevel("warning")
net, root, link = make_net(); start(net, root, link)
out = os.path.join(ROOT, "results", "web"); os.makedirs(out, exist_ok=True)
try:
    time.sleep(8)  # LLDP topology discovery
    h = net.get("h9"); h.cmd("ping -c 2 -W 1 %s" % ROOT_IP)
    h.popen(["python3", os.path.join(ROOT, "server/exam_server.py"), "--per-student", "--duration", "900",
             "--roster", os.path.join(ROOT, "data/roster.txt"), "--controller", "%s:9999" % ROOT_IP,
             "--results", os.path.join(out, "results.csv")], stdout=open(os.path.join(out, "server.log"), "w"), stderr=subprocess.STDOUT)
    time.sleep(1)
    for i in range(1, 5):
        net.get("h%d" % i).popen(["python3", os.path.join(ROOT, "web/gateway.py"), "--server", SERVER_IP, "--log-dir", out],
                                 stdout=open(os.path.join(out, "gateway_h%d.log" % i), "w"), stderr=subprocess.STDOUT)
    print("\nOpen in Firefox (inside the VM):  http://10.0.0.1:8080   http://10.0.0.2:8080   http://10.0.0.3:8080   http://10.0.0.4:8080")
    print("Results are saved to", os.path.join(out, "results.csv"), "\nType 'exit' here to stop.\n")
    CLI(net)
finally:
    net.stop()

# Online Examination Network (TCP Sockets + SDN)

Jackfruit Mini Project: a multi-user online examination application built on low-level TCP sockets and run on a Mininet network managed by a Ryu (OpenFlow 1.3) SDN controller.

**Team:** 
1.Palla Dhanush PES2UG25CS722
2.Lakshmi Varshini PES2UG25CS720
3.Saideep Deepak Talekar PES2UG25CS683

## What it does
Students connect to one exam server over TCP, log in with Student ID and name, receive questions, submit answers and get a score. The server owns the timer and auto-submits when time runs out. The SDN controller discovers the topology, routes traffic, monitors links, **limits background traffic when the network is congested**, **re-routes around a failed link**, and **blocks hosts the exam server reports as unauthorized**. Exam traffic is compared against a plain baseline network under congestion and link failure.

## Architecture
```
 h1-h4, h11 -- s2 --\                           /-- h9  exam server (TCP 5000)
                      s1 ==[10 Mbps]== s4 ----<
 h5-h8, h10 -- s3 --/   \__ s5 __/ (backup)     \-- h12 iperf sink

 h1-h8 students   h10 background UDP source   h11 intruder (not on roster)

 Ryu controller  <-- OpenFlow 1.3 --> all switches
 Exam server     --- UDP events (START, LOGIN, UNAUTHORIZED, END) ---> controller
 Browser --HTTP--> web gateway (on h1-h4) --TCP--> exam server
```

## Requirement coverage
| Requirement | Implementation |
|---|---|
| Concurrent TCP clients | `server/exam_server.py` (thread per client), `client/student_client.py`, web gateway |
| Deliver questions, accept answers, timer, results | Server-side timer with TIME_SYNC, auto-submit, scoring, `results.csv` (ID, name, score) |
| Web exam (ID + name, 15 min, 10 questions) | `web/index.html` + `web/gateway.py`, server option `--per-student` |
| Monitor application traffic | Port and flow statistics every 2 s (`port_stats.csv`, `flow_stats.csv`), exam flows counted separately |
| Dynamic SDN | Congestion limit (OpenFlow meter), link-failure reroute and recovery, blocking |
| Socket-SDN integration | UDP events from the server change controller behaviour (for example UNAUTHORIZED installs a drop rule) |
| Test under congestion, response time, fairness | `tests/run_experiment.py`, `tests/analyze.py` (latency percentiles, Jain's index), `tests/run_matrix.py`, `tests/plot.py` |

## Repository layout
```
common/protocol.py            length-prefixed JSON framing
server/exam_server.py         TCP exam server
client/student_client.py      automatic or --interactive student client
web/gateway.py, web/index.html   web exam
controller/exam_controller.py, netgraph.py   Ryu controller
topology/exam_topo.py         Mininet topology
tests/                        run_experiment, run_matrix, analyze, plot, web_demo
data/questions.json, roster.txt
results/                      plots and summary tables
```

## Requirements
Ubuntu (VM is fine), Mininet, Open vSwitch, iperf v2, Python 3 with matplotlib, and a Python 3.9 virtual environment for Ryu 4.34 (the system Python on recent Ubuntu is too new for Ryu).
```
sudo apt update
sudo apt install -y mininet openvswitch-switch iperf netcat-openbsd curl git unzip python3-matplotlib
curl -LsSf https://astral.sh/uv/install.sh | sh && source ~/.local/bin/env
uv venv --python 3.9 ~/ryu-env && source ~/ryu-env/bin/activate
uv pip install 'setuptools<58' wheel pbr
uv pip install --no-build-isolation ryu==4.34 eventlet==0.30.2
```

## Run it
Order matters: `sudo mn -c`, then the controller, then experiments.
```
# 1. exam app alone (no Mininet)
python3 server/exam_server.py --port 5055 --expected 2 --duration 30 &
python3 client/student_client.py --port 5055 --id S001 --name 'Test One' --think 0.3 &
python3 client/student_client.py --port 5055 --id S002 --name 'Test Two' --think 0.3

# 2. controller (terminal 1, never with sudo)
sudo mn -c
source ~/ryu-env/bin/activate
EXAM_MODE=sdn ryu-manager --observe-links controller/exam_controller.py     # or EXAM_MODE=baseline

# 3. experiments (terminal 2)
sudo python3 tests/run_experiment.py --students 10 --bg 12 --label sdn_n10_bg12          # congestion
sudo python3 tests/run_experiment.py --students 3 --duration 20 --intruder --label intruder   # blocking
sudo python3 tests/run_experiment.py --students 10 --redundant --think 4 --fail-at 10 --restore-after 20 --duration 90 --label sdn_fail
python3 tests/analyze.py results/sdn_n10_bg12

# 4. web exam: open http://10.0.0.1:8080 in Firefox inside the VM
sudo python3 tests/web_demo.py

# 5. full evaluation (starts its own controller; stop yours first) and charts
sudo nohup python3 tests/run_matrix.py --students 5 10 20 --bg 0 12 --reps 2 --failure < /dev/null > results/matrix.log 2>&1 &
python3 tests/plot.py
```
Student IDs must be listed in `data/roster.txt` (S001-S040 by default).

## SDN behaviour
| Situation | Controller reaction | Evidence |
|---|---|---|
| Port load above 8 Mbps for 2 polls | Non-exam traffic limited to 2 Mbps per switch by an OpenFlow meter, released after the load clears | `POLICE_ON` / `POLICE_OFF` in `sdn_events.csv` |
| Link failure | `LINK_DOWN`, flows flushed, traffic moved to s1-s5-s4; moved back after `LINK_UP` | `LINK_DOWN`, `REROUTE`, `LINK_UP` |
| Unauthorized login | Server sends UDP event, controller drops that host on every switch | `APP_UNAUTHORIZED`, `BLOCKED` |
Baseline mode uses the same forwarding but no exam class, no limit, no blocking and no reroute.

## Results (10 Mbps bottleneck, 2 repeats per condition, latencies in ms)
| Scenario | Students | Mode | Mean | 95th pct | Worst | Jain | Completed |
|---|---|---|---|---|---|---|---|
| Flood 12 Mbps | 5 | baseline | 2,844 | 4,613 | 50,310 | 0.77 | 100% |
| Flood 12 Mbps | 5 | SDN | 29 | 50 | 287 | 0.95 | 100% |
| Flood 12 Mbps | 10 | baseline | 1,334 | 4,406 | 22,320 | 0.78 | 100% |
| Flood 12 Mbps | 10 | SDN | 28 | 54 | 284 | 0.98 | 100% |
| Flood 12 Mbps | 20 | baseline | 1,466 | 5,192 | 43,140 | 0.86 | 92.5% |
| Flood 12 Mbps | 20 | SDN | 94 | 249 | 1,869 | 0.80 | 100% |
| Link failure (20 s) | 10 | baseline | 1,482 | 22,148 | 33,445 | 0.99 | 100% |
| Link failure (20 s) | 10 | SDN | 35 | 48 | 274 | 0.98 | 100% |
| No background load | 5 / 10 / 20 | baseline | 25 / 24 / 26 | 37 / 35 / 41 | 73 / 57 / 84 | 0.99+ | 100% |
| No background load | 5 / 10 / 20 | SDN | 27 / 32 / 33 | 41 / 79 / 172 | 88 / 378 / 703 | 0.96-0.99 | 100% |

Full tables: `results/summary_table.md`. Charts:

![Mean latency](results/plots/mean_req_latency_ms.png)
![95th percentile latency](results/plots/p95_req_latency_ms.png)
![Fairness](results/plots/jain_fairness.png)
![Link failure](results/plots/link_failure.png)

**Reading the results.** Under congestion the SDN controller keeps exam latency near the idle-network level (16 to 99 times lower mean latency than the baseline) and completed every student, while the baseline lost 7.5% of students at 20 students. On a link failure the controller reroutes within milliseconds; the baseline stalls for the whole outage.

## Limitations
- Two repeats per condition: results are indicative, not statistically tested.
- The controller needs about 4 s (two 2 s polls) to detect congestion, so a short exam with many students is partly unprotected at the start. At 20 students SDN fairness (0.80) was below the baseline (0.86); the baseline figure is also affected by students who did not finish.
- With no background load, SDN mode is slightly slower than the baseline (extra first-packet handling for the exam flow class); the tails are larger at 20 students.
- Fixed congestion threshold (8 Mbps) and a single shared bottleneck link; the meter depends on Open vSwitch support.
- Automated runs answer at random, so their scores are not meaningful; the web gateway only checks the roster ID (no password).

## Future work
Use the exam server's START event to protect exam traffic before congestion is detected, per-student rate fairness, and statistical analysis over more repeats.

## Documentation
- [Final report with evidence (PDF)](docs/Final_Report_with_Evidence.pdf)
- [Detailed explanation and viva notes (PDF)](docs/Detailed_Explanation.pdf)
- Evidence screenshots and charts: `docs/evidence/`

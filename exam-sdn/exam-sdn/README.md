# Online Examination Network (TCP + SDN) – Jackfruit Mini Project

Multi-user online exam over TCP sockets, deployed in Mininet and managed by a Ryu OpenFlow 1.3 controller.
This is the **D1 starter**: working app, topology, controller with monitoring + app-driven blocking, and an experiment harness.

## Layout
```
common/protocol.py        length-prefixed JSON framing (4-byte length + JSON)
server/exam_server.py     threaded TCP server: login/roster, questions, answers, timer, scoring, events -> controller
client/student_client.py  scripted student; logs per-request latency to CSV
controller/exam_controller.py   Ryu app: learning switch, exam-class flows, stats, UDP event listener, host blocking
topology/exam_topo.py     Mininet topology (10 Mbps bottleneck s1-s4)
tests/run_experiment.py   one experiment run (N students, optional UDP background load, optional intruder)
tests/analyze.py          latency / completion / Jain fairness summary -> results/summary.csv
data/questions.json, data/roster.txt
```

## Topology
```
h1-h4,h11 -- s2 --\                      /-- h9 (exam server 10.0.0.9)
                    s1 ==[10 Mbps]== s4 -
h5-h8,h10 -- s3 --/                      \-- h12 (iperf sink)
```
h1–h8 students · h10 background source · h11 intruder (not on roster).
s1 also has a link to the root namespace (10.123.123.1): the controller listens there on UDP 9999
for application events from the exam server (this is the socket → SDN integration channel).

## Protocol
Client→server: `LOGIN, GET_QUESTION, ANSWER, SUBMIT, PING`
Server→client: `LOGIN_OK, START, QUESTION, ACK, TIME_SYNC, RESULT, PONG, ERROR`
Exam starts when `--expected` students have logged in (or after `--lobby` s). Server auto-submits at timeout.
Dropped clients can reconnect with the same ID and resume at the next unanswered question.

## App event → SDN reaction
| Event (UDP to controller) | Reaction implemented now | D2 hook |
|---|---|---|
| `START` | logged, exam_active=true | install meters/queues for exam class |
| `LOGIN` | IP↔student mapping stored | per-student fairness rules |
| `UNAUTHORIZED` | drop rule (prio 300) for that IP on all switches | – |
| `END` | exam_active=false | remove exam rules |

## Setup (Ubuntu 20.04, Python 3.8 recommended for Ryu)
```
sudo apt install mininet openvswitch-switch iperf
pip install -r requirements.txt
```

## Run
Terminal 1 (controller, from repo root):
```
EXAM_MODE=sdn ryu-manager --observe-links controller/exam_controller.py
```
Terminal 2:
```
sudo mn -c
sudo python3 tests/run_experiment.py --students 10 --bg 0  --label sdn_n10_bg0
sudo python3 tests/run_experiment.py --students 10 --bg 12 --label sdn_n10_bg12 --intruder
python3 tests/analyze.py results/sdn_n10_bg12
```
Baseline: restart controller with `EXAM_MODE=baseline` and use `--label baseline_...`.
Interactive: `sudo python3 topology/exam_topo.py` (Mininet CLI).
Controller logs (`CONGESTION`, `APP EVENT`, `BLOCKED`, `PORT_STATUS`) print in terminal 1; stats CSVs go to `results/`.

## Standalone app test (no Mininet)
```
python3 server/exam_server.py --port 5055 --expected 2 --duration 30
python3 client/student_client.py --port 5055 --id S001 &  python3 client/student_client.py --port 5055 --id S002
```

## Status / TODO for D2
- [x] TCP app, timer, scoring, roster, resume, latency logging
- [x] Topology, controller, port/flow stats, app-driven blocking
- [ ] Redundant path (`--redundant`) needs controller path computation (current learning switch would loop)
- [ ] Meters/queues to prioritise exam class + per-student fairness; congestion-triggered response
- [ ] Link-failure reroute on `PORT_STATUS`
- [ ] Experiment matrix (5/10/20 students × 0/50/90 % load, baseline vs SDN) + graphs

## Web exam (15 minutes, 10 questions)
Browsers cannot open raw TCP sockets, so `web/gateway.py` serves the page and opens one real TCP connection per student to the exam server (protocol unchanged).
```
# terminal 1: controller          # terminal 2 (inside Mininet):
source ~/ryu-env/bin/activate     sudo python3 tests/web_demo.py
EXAM_MODE=sdn ryu-manager --observe-links controller/exam_controller.py
```
Open `http://10.0.0.1:8080` (h1), `10.0.0.2` (h2) ... in Firefox inside the VM. Student IDs must be in `data/roster.txt`.
From Windows: `ssh -N -L 8081:10.0.0.1:8080 exam-vm`, then open `http://localhost:8081`.
Results: `results/web/results.csv` (scores, names) and `client_<ID>.csv` latency logs for `tests/analyze.py`.
Without Mininet: `python3 server/exam_server.py --per-student --duration 900 --roster data/roster.txt` and `python3 web/gateway.py`, then open `http://127.0.0.1:8080`.

## Dynamic SDN behaviour (D2)
The controller must be started with `--observe-links` (LLDP topology discovery).

| Situation | Controller reaction | Evidence |
|---|---|---|
| Exam traffic (TCP 5000) | own high-priority flows, counted separately | `flow_stats.csv` |
| Link load > 8 Mbps for 2 polls | all non-exam traffic rate-limited by an OpenFlow meter (2 Mbps per switch); released after the load clears (hysteresis) | `sdn_events.csv`: `POLICE_ON` / `POLICE_OFF` |
| Link failure (needs `--redundant`) | `LINK_DOWN` -> flows flushed -> traffic re-routed via s1-s5-s4 | `sdn_events.csv`: `LINK_DOWN`, `REROUTE` |
| Unauthorized login | server sends UDP event -> drop rule on all switches | `BLOCKED` |
Baseline mode (`EXAM_MODE=baseline`): same forwarding but no exam class, no policing, no blocking and no reroute (a static network).
Tunables (env vars): `EXAM_CONGESTION_MBPS` (8), `EXAM_BG_LIMIT_KBPS` (2000), `EXAM_POLICE_HOLD` (15 s), `EXAM_POLL` (2 s).

### Scenarios
```
# congestion: compare SDN vs baseline
sudo python3 tests/run_experiment.py --students 10 --bg 12 --label sdn_n10_bg12
# link failure at t=20 s, restored 20 s later (restart the controller in baseline mode and repeat for the comparison)
sudo python3 tests/run_experiment.py --students 10 --redundant --fail-at 20 --restore-after 20 --duration 90 --label sdn_fail
```
Compare `p95_req_latency_ms`, `max_req_latency_ms`, `completion_rate` and `jain_fairness` in `results/summary.csv`.

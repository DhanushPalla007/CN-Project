#!/usr/bin/env python3
"""Multi-threaded TCP online-exam server.

- One thread per client connection, shared state guarded by locks.
- Authoritative server-side timer, periodic TIME_SYNC, auto-submit at expiry.
- Roster check; duplicate-login rejection; reconnect/resume for dropped clients.
- Sends application events (START / UNAUTHORIZED / END) to the SDN controller
  over UDP so the network can react (socket <-> SDN integration).
"""
import argparse
import csv
import json
import os
import socket
import sys
import threading
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from common.protocol import send_msg, recv_msg, ProtocolError  # noqa: E402


class Session:
    def __init__(self, sid):
        self.sid = sid
        self.name = ""
        self.start_time = None
        self.end_time = None
        self.conn = None
        self.addr = None
        self.answers = {}
        self.submitted = False
        self.auto = False
        self.score = None
        self.finished_at = None
        self.send_lock = threading.Lock()


class ExamServer:
    def __init__(self, a):
        self.a = a
        with open(a.questions) as f:
            self.questions = json.load(f)
        self.roster = None
        if a.roster:
            with open(a.roster) as f:
                self.roster = {l.strip() for l in f if l.strip() and not l.startswith("#")}
        self.ctrl = None
        if a.controller:
            host, port = a.controller.rsplit(":", 1)
            self.ctrl = (host, int(port))
        self.udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sessions = {}
        self.lock = threading.Lock()
        self.started = threading.Event()
        self.done = threading.Event()
        self.start_time = None
        self.end_time = None
        self.t0 = time.time()
        if a.per_student:
            self.started.set()

    # ---------------------------------------------------------------- helpers
    def log(self, *args):
        print("[%.2f]" % (time.time() - self.t0), *args, flush=True)

    def notify(self, event, **kw):
        """Best-effort UDP event to the SDN controller."""
        if not self.ctrl:
            return
        try:
            kw["event"] = event
            self.udp.sendto(json.dumps(kw).encode(), self.ctrl)
        except OSError as e:
            self.log("notify failed:", e)

    def remaining(self, s=None):
        end = s.end_time if (self.a.per_student and s) else self.end_time
        if end is None:
            return self.a.duration
        return max(0.0, end - time.time())

    def _send(self, s, obj):
        with s.send_lock:
            conn = s.conn
            if conn is None:
                return False
            try:
                send_msg(conn, obj)
                return True
            except OSError:
                return False

    def _score(self, s):
        return sum(1 for i, c in s.answers.items() if self.questions[i]["answer"] == c)

    def _finalize(self, s, auto):
        with self.lock:
            if not s.submitted:
                s.submitted = True
                s.auto = auto
                s.score = self._score(s)
                s.finished_at = time.time()
        if self.a.per_student:
            with self.lock:
                done = [x for x in self.sessions.values() if x.submitted]
            self._write_results(done)

    def _result_msg(self, s):
        return {"type": "RESULT", "score": s.score, "total": len(self.questions),
                "auto_submitted": s.auto,
                "review": [{"index": i, "text": q["q"], "options": q["options"],
                            "chosen": s.answers.get(i), "correct": q["answer"]}
                           for i, q in enumerate(self.questions)]}

    @staticmethod
    def _err(reason):
        return {"type": "ERROR", "reason": reason}

    # ---------------------------------------------------------- per-client
    def handle(self, conn, addr):
        s = None
        try:
            conn.settimeout(self.a.idle)
            conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            msg = recv_msg(conn)
            sid = str(msg.get("student_id", "")).strip()
            name = str(msg.get("name", "")).strip()[:60]
            if msg["type"] != "LOGIN" or not sid:
                send_msg(conn, self._err("bad_login"))
                return
            if self.roster is not None and sid not in self.roster:
                send_msg(conn, self._err("unauthorized"))
                self.log("UNAUTHORIZED", sid, addr[0])
                self.notify("UNAUTHORIZED", ip=addr[0], student_id=sid)
                return
            with self.lock:
                s = self.sessions.get(sid)
                if s is None:
                    s = self.sessions[sid] = Session(sid)
                busy = s.conn is not None
                if not busy:
                    s.conn, s.addr = conn, addr
                    if name:
                        s.name = name
            if busy:
                send_msg(conn, self._err("duplicate_login"))
                return
            if self.a.per_student and s.end_time is None:
                s.start_time = time.time()
                s.end_time = s.start_time + self.a.duration
            nxt = next((i for i in range(len(self.questions)) if i not in s.answers),
                       len(self.questions))
            self._send(s, {"type": "LOGIN_OK", "num_questions": len(self.questions),
                           "duration": self.a.duration, "next_index": nxt, "remaining": self.remaining(s)})
            self.log("LOGIN", sid, "(%s)" % s.name if s.name else "", addr[0], "(resume)" if s.answers else "")
            self.notify("LOGIN", ip=addr[0], student_id=sid)
            if s.submitted:
                self._send(s, self._result_msg(s))
                return
            if self.started.is_set():
                self._send(s, {"type": "START", "remaining": self.remaining(s),
                               "num_questions": len(self.questions)})
            self._loop(s, conn)
        except (ConnectionError, socket.timeout, OSError):
            pass
        except ProtocolError as e:
            self.log("protocol error from", addr[0], e)
            try:
                send_msg(conn, self._err("malformed"))
            except OSError:
                pass
        finally:
            if s is not None:
                with self.lock:
                    if s.conn is conn:
                        s.conn = None
            try:
                conn.close()
            except OSError:
                pass

    def _loop(self, s, conn):
        nq = len(self.questions)
        while True:
            m = recv_msg(conn)
            t = m["type"]
            if t == "PING":
                self._send(s, {"type": "PONG", "remaining": self.remaining(s)})
                continue
            if t == "SUBMIT":
                self._finalize(s, auto=False)
                self._send(s, self._result_msg(s))
                self.log("SUBMIT", s.sid, "score", s.score)
                continue
            if t not in ("GET_QUESTION", "ANSWER"):
                self._send(s, self._err("unknown_type"))
                continue
            if not self.started.is_set():
                self._send(s, self._err("not_started"))
                continue
            if s.submitted or self.remaining(s) <= 0:
                self._send(s, self._err("time_expired"))
                continue
            idx = m.get("index")
            if not isinstance(idx, int) or not 0 <= idx < nq:
                self._send(s, self._err("bad_index"))
                continue
            if t == "GET_QUESTION":
                q = self.questions[idx]
                self._send(s, {"type": "QUESTION", "index": idx, "text": q["q"],
                               "options": q["options"], "remaining": self.remaining(s)})
            else:
                choice = m.get("choice")
                if not isinstance(choice, int) or not 0 <= choice < len(self.questions[idx]["options"]):
                    self._send(s, self._err("bad_choice"))
                    continue
                s.answers[idx] = choice
                self._send(s, {"type": "ACK", "index": idx})

    # ------------------------------------------------------- exam lifecycle
    def _broadcast(self, build, only_active=True):
        with self.lock:
            targets = [s for s in self.sessions.values() if not (only_active and s.submitted)]
        for s in targets:
            self._send(s, build(s))

    def coordinator(self):
        a = self.a
        while True:
            time.sleep(0.2)
            with self.lock:
                logged = len(self.sessions)
            waited = time.time() - self.t0
            if logged >= a.expected or (logged > 0 and waited >= a.lobby):
                break
        self.start_time = time.time()
        self.end_time = self.start_time + a.duration
        self.started.set()
        self.log("EXAM START with", logged, "students, duration", a.duration, "s")
        self.notify("START", students=logged, duration=a.duration)
        self._broadcast(lambda s: {"type": "START", "remaining": a.duration,
                                   "num_questions": len(self.questions)})
        last = time.time()
        while time.time() < self.end_time:
            time.sleep(0.2)
            with self.lock:
                all_done = len(self.sessions) >= a.expected and all(
                    s.submitted for s in self.sessions.values())
            if all_done:
                break
            if time.time() - last >= a.sync:
                last = time.time()
                self._broadcast(lambda s: {"type": "TIME_SYNC", "remaining": self.remaining()})
        # finalize: auto-submit anyone still working
        with self.lock:
            sessions = list(self.sessions.values())
        for s in sessions:
            if not s.submitted:
                self._finalize(s, auto=True)
                self._send(s, self._result_msg(s))
        self._write_results(sessions)
        self.log("EXAM END")
        self.notify("END", students=len(sessions))
        time.sleep(a.grace)
        self.done.set()

    def _write_results(self, sessions):
        if not self.a.results:
            return
        os.makedirs(os.path.dirname(os.path.abspath(self.a.results)), exist_ok=True)
        with open(self.a.results, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["student_id", "name", "score", "total", "answered", "auto_submitted",
                        "finished_after_s"])
            for s in sorted(sessions, key=lambda x: x.sid):
                w.writerow([s.sid, s.name, s.score, len(self.questions), len(s.answers),
                            int(s.auto), round(s.finished_at - (s.start_time or self.start_time), 3)])

    def reaper(self):
        """Per-student mode: auto-submit anyone whose own timer has expired."""
        while True:
            time.sleep(0.5)
            with self.lock:
                todo = [x for x in self.sessions.values()
                        if x.end_time and not x.submitted and time.time() > x.end_time]
            for x in todo:
                self._finalize(x, auto=True)
                self._send(x, self._result_msg(x))
                self.log("AUTO-SUBMIT", x.sid, "score", x.score)

    def serve(self):
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind((self.a.host, self.a.port))
        srv.listen(128)
        srv.settimeout(0.5)
        self.log("listening on %s:%d" % (self.a.host, self.a.port))
        threading.Thread(target=self.reaper if self.a.per_student else self.coordinator, daemon=True).start()
        while not self.done.is_set():
            try:
                conn, addr = srv.accept()
            except socket.timeout:
                continue
            threading.Thread(target=self.handle, args=(conn, addr), daemon=True).start()
        srv.close()


def main():
    p = argparse.ArgumentParser(description="Online exam TCP server")
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--port", type=int, default=5000)
    p.add_argument("--questions", default=os.path.join(os.path.dirname(__file__), "..", "data", "questions.json"))
    p.add_argument("--roster", help="file of allowed student IDs (omit = allow all)")
    p.add_argument("--duration", type=float, default=60, help="exam length (s)")
    p.add_argument("--expected", type=int, default=1, help="start when this many students logged in")
    p.add_argument("--lobby", type=float, default=15, help="or start after this many seconds if >=1 logged in")
    p.add_argument("--sync", type=float, default=5, help="TIME_SYNC interval (s)")
    p.add_argument("--idle", type=float, default=120, help="per-connection idle timeout (s)")
    p.add_argument("--grace", type=float, default=2, help="seconds to linger after exam end")
    p.add_argument("--per-student", action="store_true", help="web mode: each student gets their own timer from login")
    p.add_argument("--controller", help="ip:port of controller UDP event listener")
    p.add_argument("--results", help="CSV path for final results")
    ExamServer(p.parse_args()).serve()


if __name__ == "__main__":
    main()

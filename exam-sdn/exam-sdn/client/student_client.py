#!/usr/bin/env python3
"""Scripted student client. Logs per-request latency to CSV for evaluation.

CSV columns: student_id,event,index,latency_ms,detail
events: login, question, answer_ack, submit, result_auto, disconnect, error
"""
import argparse
import csv
import os
import random
import socket
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from common.protocol import send_msg, recv_msg, ProtocolError  # noqa: E402


class ExamOver(Exception):
    pass


class ServerError(Exception):
    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


class Client:
    def __init__(self, a):
        self.a = a
        self.rows = []
        self.sock = None
        self.started = False
        self.nq = 0
        self.next_index = 0
        self.result = None

    def log(self, event, index="", latency=None, detail=""):
        ms = "" if latency is None else round(latency * 1000, 2)
        self.rows.append([self.a.id, event, index, ms, detail])

    # ----------------------------------------------------------- messaging
    def pump(self, expect):
        """Receive until a message of a wanted type arrives."""
        while True:
            m = recv_msg(self.sock)
            t = m["type"]
            if t == "TIME_SYNC":
                continue
            if t == "START":
                self.started = True
                if t in expect:
                    return m
                continue
            if t == "ERROR":
                raise ServerError(m.get("reason", "?"))
            if t == "RESULT" and "RESULT" not in expect:
                self.result = m
                raise ExamOver()
            if t in expect:
                return m

    def request(self, msg, expect):
        t0 = time.perf_counter()
        send_msg(self.sock, msg)
        m = self.pump(expect)
        return m, time.perf_counter() - t0

    # ----------------------------------------------------- interactive mode
    def ask(self, q, i):
        """Show a question and read the student's choice from the keyboard."""
        print("\nQuestion %d/%d   (about %d s left)" % (i + 1, self.nq, q.get("remaining", 0)))
        print(q["text"])
        for j, opt in enumerate(q["options"], 1):
            print("  %d) %s" % (j, opt))
        while True:
            try:
                raw = input("Your answer (1-%d): " % len(q["options"])).strip()
            except EOFError:
                raise SystemExit("input closed")
            if raw.isdigit() and 1 <= int(raw) <= len(q["options"]):
                return int(raw) - 1
            print("Please type a number between 1 and %d." % len(q["options"]))

    def show_result(self):
        if self.a.interactive and self.result:
            auto = " (time ran out, answers submitted automatically)" if self.result.get("auto_submitted") else ""
            print("\nResult for %s: %s / %s%s" % (self.a.id, self.result["score"], self.result["total"], auto))

    # -------------------------------------------------------------- session
    def session(self):
        a = self.a
        t0 = time.perf_counter()
        self.sock = socket.create_connection((a.server, a.port), timeout=a.timeout)
        self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        m, _ = self.request({"type": "LOGIN", "student_id": a.id, "name": a.name}, {"LOGIN_OK"})
        self.log("login", latency=time.perf_counter() - t0)  # includes TCP connect
        self.nq = m["num_questions"]
        self.next_index = m["next_index"]
        if a.interactive and not self.started:
            print("Logged in as %s. Waiting for the exam to start..." % a.id)
        while not self.started:
            self.pump({"START"})
        if a.interactive:
            print("Exam started! You have %d seconds in total." % m["duration"])
        for i in range(self.next_index, self.nq):
            q, lat = self.request({"type": "GET_QUESTION", "index": i}, {"QUESTION"})
            self.log("question", i, lat)
            if a.interactive:
                choice = self.ask(q, i)
            else:
                time.sleep(random.uniform(0.5 * a.think, a.think))
                choice = random.randrange(len(q["options"]))
            _, lat = self.request({"type": "ANSWER", "index": i, "choice": choice}, {"ACK"})
            self.log("answer_ack", i, lat)
            self.next_index = i + 1
        res, lat = self.request({"type": "SUBMIT"}, {"RESULT"})
        self.log("submit", latency=lat, detail="score=%s/%s" % (res["score"], res["total"]))
        self.result = res
        self.show_result()

    def run(self):
        attempts = 0
        while attempts <= self.a.retries:
            try:
                self.session()
                break
            except ExamOver:
                self.log("result_auto", detail="score=%s/%s" % (self.result["score"], self.result["total"]))
                self.show_result()
                break
            except ServerError as e:
                self.log("error", detail=e.reason)
                if e.reason == "time_expired":
                    try:
                        self.result = self.pump({"RESULT"})
                        self.show_result()
                    except Exception:
                        pass
                break
            except (OSError, ConnectionError, ProtocolError) as e:
                self.log("disconnect", detail=type(e).__name__)
                attempts += 1
                time.sleep(1)
            finally:
                if self.sock:
                    try:
                        self.sock.close()
                    except OSError:
                        pass
        self.save()

    def save(self):
        if not self.a.log:
            return
        os.makedirs(os.path.dirname(os.path.abspath(self.a.log)), exist_ok=True)
        with open(self.a.log, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["student_id", "event", "index", "latency_ms", "detail"])
            w.writerows(self.rows)


def main():
    p = argparse.ArgumentParser(description="Student exam client")
    p.add_argument("--server", default="127.0.0.1")
    p.add_argument("--port", type=int, default=5000)
    p.add_argument("--id", required=True)
    p.add_argument("--name", default="", help="student name sent at login")
    p.add_argument("--interactive", action="store_true", help="type your own answers (default: automatic random answers)")
    p.add_argument("--think", type=float, default=1.0, help="max think time per question (s)")
    p.add_argument("--timeout", type=float, default=120, help="socket timeout (s)")
    p.add_argument("--retries", type=int, default=3, help="reconnect attempts")
    p.add_argument("--log", help="CSV path for latency log")
    Client(p.parse_args()).run()


if __name__ == "__main__":
    main()

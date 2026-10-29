#!/usr/bin/env python3
"""Web gateway: browser <-> TCP exam server. One real TCP socket per student."""
import argparse, csv, json, os, re, secrets, socket, sys, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
from common.protocol import send_msg, recv_msg, ProtocolError  # noqa: E402

SESS, BY_ID, ARGS = {}, {}, None


class Done(Exception): pass
class Fail(Exception):
    def __init__(self, reason, code=400): self.reason, self.code = reason, code


class WS:
    def __init__(self, sid, name, sock):
        self.sid, self.name, self.sock = sid, name, sock
        self.lock, self.answers, self.rows, self.result = threading.Lock(), {}, [], None


def call(ws, msg, expect):
    """One request/reply on the student's TCP socket; returns (reply, seconds)."""
    t0 = time.perf_counter()
    send_msg(ws.sock, msg)
    while True:
        m = recv_msg(ws.sock); t = m["type"]
        if t in ("TIME_SYNC", "START"): continue
        if t == "ERROR": raise Fail(m.get("reason", "error"), 403 if m.get("reason") == "unauthorized" else 400)
        if t in expect: return m, time.perf_counter() - t0
        if t == "RESULT": ws.result = m; raise Done()


def note(ws, event, idx="", lat=None, detail=""):
    ws.rows.append([ws.sid, event, idx, "" if lat is None else round(lat * 1000, 2), detail])


def finish(ws):
    try: ws.sock.close()
    except OSError: pass
    if ARGS.log_dir:
        os.makedirs(ARGS.log_dir, exist_ok=True)
        with open(os.path.join(ARGS.log_dir, "client_%s.csv" % ws.sid), "w", newline="") as f:
            w = csv.writer(f); w.writerow(["student_id", "event", "index", "latency_ms", "detail"]); w.writerows(ws.rows)


def login(a):
    sid, name = str(a.get("student_id", "")).strip(), str(a.get("name", "")).strip()[:60]
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,20}", sid) or not name: raise Fail("Enter a valid Student ID and your name.")
    ws = BY_ID.get(sid)
    if ws is None:
        t0 = time.perf_counter()
        sock = socket.create_connection((ARGS.server, ARGS.port), timeout=30)
        ws = WS(sid, name, sock)
        m, _ = call(ws, {"type": "LOGIN", "student_id": sid, "name": name}, {"LOGIN_OK"})
        note(ws, "login", lat=time.perf_counter() - t0)
        ws.nq, ws.token = m["num_questions"], secrets.token_urlsafe(16)
        SESS[ws.token] = BY_ID[sid] = ws
    with ws.lock:
        if ws.result: raise Done()
        m, _ = call(ws, {"type": "PING"}, {"PONG"})
    return {"token": ws.token, "name": ws.name, "num_questions": ws.nq, "remaining": m["remaining"], "answered": sorted(ws.answers)}


def handle(ws, path, a):
    if ws.result: return {"done": True, "result": ws.result}
    if path == "/api/question":
        i = int(a["index"]); m, l = call(ws, {"type": "GET_QUESTION", "index": i}, {"QUESTION"}); note(ws, "question", i, l)
        return {"index": i, "text": m["text"], "options": m["options"], "remaining": m["remaining"], "chosen": ws.answers.get(i)}
    if path == "/api/answer":
        i, c = int(a["index"]), int(a["choice"]); _, l = call(ws, {"type": "ANSWER", "index": i, "choice": c}, {"ACK"})
        ws.answers[i] = c; note(ws, "answer_ack", i, l); return {"ok": True}
    if path == "/api/time":
        m, _ = call(ws, {"type": "PING"}, {"PONG"}); return {"remaining": m["remaining"]}
    if path == "/api/submit":
        m, l = call(ws, {"type": "SUBMIT"}, {"RESULT"}); ws.result = m
        note(ws, "submit", lat=l, detail="score=%s/%s" % (m["score"], m["total"])); finish(ws)
        return {"done": True, "result": m}
    raise Fail("not_found", 404)


class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def reply(self, obj, code=200, ctype="application/json"):
        b = obj if isinstance(obj, bytes) else json.dumps(obj).encode()
        self.send_response(code); self.send_header("Content-Type", ctype); self.send_header("Content-Length", str(len(b)))
        self.send_header("Cache-Control", "no-store"); self.end_headers(); self.wfile.write(b)
    def do_GET(self):
        u = urlparse(self.path)
        if u.path == "/": return self.reply(open(os.path.join(HERE, "index.html"), "rb").read(), 200, "text/html; charset=utf-8")
        self.route(u.path, {k: v[0] for k, v in parse_qs(u.query).items()})
    def do_POST(self):
        n = min(int(self.headers.get("Content-Length", 0)), 10000)
        try: body = json.loads(self.rfile.read(n) or b"{}")
        except ValueError: return self.reply({"error": "bad request"}, 400)
        self.route(self.path, body)
    def route(self, path, a):
        ws = None
        try:
            if path == "/api/login": out = login(a)
            else:
                ws = SESS.get(a.get("token"))
                if not ws: raise Fail("Session not found. Please log in again.", 401)
                with ws.lock: out = handle(ws, path, a)
            self.reply(out)
        except Done:
            w = ws or BY_ID.get(str(a.get("student_id", "")).strip())
            if w: finish(w)
            self.reply({"done": True, "result": w.result if w else None})
        except Fail as e: self.reply({"error": e.reason}, e.code)
        except (OSError, ProtocolError, ValueError, KeyError) as e:
            self.reply({"error": "Cannot reach the exam server (%s)" % type(e).__name__}, 502)


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="Web gateway for the exam server")
    p.add_argument("--server", default="127.0.0.1"); p.add_argument("--port", type=int, default=5000)
    p.add_argument("--listen", default="0.0.0.0"); p.add_argument("--http-port", type=int, default=8080)
    p.add_argument("--log-dir", help="write per-student latency CSVs here")
    ARGS = p.parse_args()
    print("gateway on http://%s:%d -> exam server %s:%d" % (ARGS.listen, ARGS.http_port, ARGS.server, ARGS.port), flush=True)
    ThreadingHTTPServer((ARGS.listen, ARGS.http_port), H).serve_forever()

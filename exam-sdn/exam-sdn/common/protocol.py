"""Length-prefixed JSON framing over TCP.

Frame = 4-byte big-endian length + UTF-8 JSON object with a "type" field.
TCP is a byte stream, so explicit framing is required to find message boundaries.

Client -> server: LOGIN, GET_QUESTION, ANSWER, SUBMIT, PING
Server -> client: LOGIN_OK, START, QUESTION, ACK, TIME_SYNC, RESULT, PONG, ERROR
"""
import json
import struct

_HDR = struct.Struct("!I")
MAX_MSG = 1 << 20  # 1 MiB sanity limit


class ProtocolError(Exception):
    pass


def send_msg(sock, obj):
    body = json.dumps(obj, separators=(",", ":")).encode("utf-8")
    sock.sendall(_HDR.pack(len(body)) + body)


def _recv_exact(sock, n):
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("peer closed connection")
        buf.extend(chunk)
    return bytes(buf)


def recv_msg(sock):
    (n,) = _HDR.unpack(_recv_exact(sock, _HDR.size))
    if n == 0 or n > MAX_MSG:
        raise ProtocolError("bad frame length %d" % n)
    try:
        msg = json.loads(_recv_exact(sock, n).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise ProtocolError("malformed JSON: %s" % e)
    if not isinstance(msg, dict) or "type" not in msg:
        raise ProtocolError("message missing 'type'")
    return msg

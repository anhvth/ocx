#!/usr/bin/env python3
"""Run real turns through a Codex app-server the same way the Desktop does (JSON-RPC).

Two transports:
  stdio  : spawns a FRESH `<codex> app-server` (tests the config on disk)
           app_server_turn.py stdio <codex-binary> <model> [--desktop-flags]
  socket : attaches to the LIVE server the Desktop started (WebSocket over unix socket)
           app_server_turn.py socket <path/to/app-server-control.sock> <model>

Always lists models, starts a thread, and sends TWO turns. Turn 2 matters: the Desktop
adds call_id-less codex_apps_* tool outputs from the second turn on, which is where
key-mode providers (Alibaba) used to 400.
Note: a fresh stdio server does not inject those Desktop items; only a real Desktop chat or
scripts/orphan_request.sh exercises that path.
Creates a visible test thread when used against the live socket.
"""
import base64, json, os, select, socket, struct, subprocess, sys, time

mode, target, model = sys.argv[1], sys.argv[2], sys.argv[3]
desktop_flags = "--desktop-flags" in sys.argv


class Stdio:
    def __init__(self, binary):
        cmd = [binary]
        if desktop_flags:
            cmd += ["-c", "features.code_mode_host=true"]
        cmd += ["app-server"]
        self.p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                  stderr=subprocess.DEVNULL, text=True)

    def send(self, o):
        self.p.stdin.write(json.dumps(o) + "\n"); self.p.stdin.flush()

    def recv(self, timeout):
        if select.select([self.p.stdout], [], [], timeout)[0]:
            line = self.p.stdout.readline()
            return json.loads(line) if line else None
        return None

    def close(self):
        self.p.kill()


class Ws:
    def __init__(self, path):
        self.s = socket.socket(socket.AF_UNIX); self.s.connect(path); self.s.settimeout(300)
        key = base64.b64encode(os.urandom(16)).decode()
        self.s.sendall((f"GET / HTTP/1.1\r\nHost: localhost\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                        f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
        self.buf = b""
        while b"\r\n\r\n" not in self.buf:
            self.buf += self.s.recv(4096)
        head, self.buf = self.buf.split(b"\r\n\r\n", 1)
        assert b" 101 " in head.split(b"\r\n")[0], head

    def send(self, o):
        d = json.dumps(o).encode(); m = os.urandom(4); n = len(d)
        h = bytes([0x81]) + (bytes([0x80 | n]) if n < 126 else
                             bytes([0xFE]) + struct.pack(">H", n) if n < 65536 else
                             bytes([0xFF]) + struct.pack(">Q", n))
        self.s.sendall(h + m + bytes(b ^ m[i % 4] for i, b in enumerate(d)))

    def _need(self, n):
        while len(self.buf) < n:
            self.buf += self.s.recv(65536)

    def recv(self, timeout):
        msg = b""
        while True:
            self._need(2); b0, b1 = self.buf[0], self.buf[1]; n = b1 & 0x7F; off = 2
            if n == 126: self._need(4); n = struct.unpack(">H", self.buf[2:4])[0]; off = 4
            elif n == 127: self._need(10); n = struct.unpack(">Q", self.buf[2:10])[0]; off = 10
            self._need(off + n); pl = self.buf[off:off + n]; self.buf = self.buf[off + n:]
            op = b0 & 0xF
            if op in (8, 9, 10):
                if op == 8: return None
                continue
            msg += pl
            if b0 & 0x80:
                return json.loads(msg)

    def close(self):
        self.s.close()


c = Stdio(target) if mode == "stdio" else Ws(target)


def until(pred, t=280):
    end = time.time() + t
    while time.time() < end:
        m = c.recv(1)
        if m is None:
            continue
        if "error" in m and m.get("id") is not None:
            print("RPC ERROR", json.dumps(m)[:500])
        if pred(m):
            return m
    raise SystemExit("timeout")


c.send({"id": 1, "method": "initialize", "params": {"clientInfo": {"name": "skill-verify", "version": "0"}}})
until(lambda m: m.get("id") == 1)
c.send({"method": "initialized"})
c.send({"id": 2, "method": "model/list", "params": {}})
r = until(lambda m: m.get("id") == 2)
print("models:", [x.get("model") for x in r["result"]["data"]])
c.send({"id": 3, "method": "thread/start", "params": {"model": model, "cwd": os.environ.get("TMPDIR", "/tmp")}})
r = until(lambda m: m.get("id") == 3)
th = r["result"]["thread"]["id"]
print("thread", th, "provider", r["result"].get("modelProvider"), "model", r["result"].get("model"))
ok = True
for i, prompt in enumerate(["Reply with exactly: TURN1_OK", "Reply with exactly: TURN2_OK"], start=1):
    c.send({"id": 10 + i, "method": "turn/start",
            "params": {"threadId": th, "input": [{"type": "text", "text": prompt}]}})
    texts = []

    def done(m):
        if m.get("method") == "item/completed" and m["params"]["item"].get("type") == "agentMessage":
            texts.append(m["params"]["item"].get("text"))
        return m.get("method") == "turn/completed" and m["params"].get("threadId") == th

    m = until(done)
    turn = m["params"]["turn"]
    print(f"turn{i}:", turn.get("status"), turn.get("error"), texts)
    ok = ok and not turn.get("error") and any(f"TURN{i}_OK" in (t or "") for t in texts)
c.close()
print("RESULT:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)

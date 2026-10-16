"""End-to-end: the link between the mod and server.py --mock, through a fake game folder.

The mod writes to log.txt and reads its save file; the backend tails the log and writes into the save.
* lua bridge : the mod's real bridge.lua + savefile.lua (Lua 5.3, like Isaac) under lupa.
* protocol   : a Python stand-in for the mod, for the edges: protocol mismatch, chunked lines, acks,
               the mod's own data surviving the backend's writes and the other way round.
"""
import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from lupa.lua53 import LuaRuntime

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
MOD = ROOT / "mod" / "llm_dungeon_master"
sys.path.insert(0, str(BACKEND))
from bridge import PROTO, LogTail  # noqa: E402

STATE = {"player": {"hp": {"red": 6, "soul": 2}, "resources": {"keys": 0, "bombs": 1, "coins": 3},
                    "stats": {"luck": 0}},
         "level": {"stage": 2, "needs": {"locked_doors_seen": 1, "bomb_targets": 2}}}


def fake_game(tmp):
    tmp = Path(tmp)
    save = tmp / "isaac" / "data" / "llm_dungeon_master" / "save1.dat"
    save.parent.mkdir(parents=True)
    (tmp / "log.txt").write_bytes(b"")
    return {"log": tmp / "log.txt", "save": save,
            "args": ["--log", str(tmp / "log.txt"), "--isaac-dir", str(tmp / "isaac")]}


def start_server(game, *args):
    p = subprocess.Popen([sys.executable, "server.py", "--mock", "--no-receipts", *game["args"], *args], cwd=BACKEND,
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    time.sleep(1.0)
    assert p.poll() is None, p.stdout.read()
    return p


def stop(srv):
    srv.terminate()
    return srv.communicate(timeout=5)[0]


# ------------------------------------------------------------------ the real bridge.lua
def make_lua(game):
    lua = LuaRuntime(unpack_returned_tuples=True)
    g = lua.globals()
    t0 = time.monotonic()

    def to_py(v):
        if g.type(v) == "table":
            keys = list(v.keys())
            if keys and all(isinstance(k, int) for k in keys) and sorted(keys) == list(range(1, len(keys) + 1)):
                return [to_py(v[i]) for i in range(1, len(keys) + 1)]
            return {str(k): to_py(v[k]) for k in keys}
        return v

    jsonmod = lua.table_from({"encode": lambda v: json.dumps(to_py(v), separators=(",", ":")),
                              "decode": lambda s: lua.table_from(json.loads(s), recursive=True)})
    real_require = g.require
    g.require = lambda name: jsonmod if name == "json" else real_require(name)

    def debug(s):
        with open(game["log"], "a", encoding="utf-8") as f:
            f.write("[INFO] - Lua Debug: " + s + "\n")

    def load():
        try:
            return game["save"].read_text(encoding="utf-8")
        except OSError:
            return ""

    g.Isaac = lua.table_from({"GetTime": lambda: int((time.monotonic() - t0) * 1000), "DebugString": debug})
    mod = lua.table_from({"SaveData": lambda _self, s: game["save"].write_text(s, encoding="utf-8"),
                          "HasData": lambda _self: game["save"].exists(),
                          "LoadData": lambda _self: load()})
    store = lua.execute(open(MOD / "dm" / "savefile.lua").read())
    store.init(mod)
    bridge = lua.execute(open(MOD / "dm" / "bridge.lua").read())
    cfg = lua.table_from({"POLL_MS": 20, "LOG_CHUNK": 300})     # small chunks: snapshots span several lines
    return lua, bridge, store, cfg


def pump(bridge, secs, until=None):
    end = time.monotonic() + secs
    while time.monotonic() < end:
        bridge.update()
        if until and until():
            return True
        time.sleep(0.016)
    return bool(until and until())


def test_lua_bridge():
    with tempfile.TemporaryDirectory() as d:
        game = fake_game(d)
        srv = start_server(game)
        try:
            lua, B, store, cfg = make_lua(game)
            store.save(lua.table_from({"cruelty": 4}))
            B.init(cfg, store, "RUN1")
            B.setRun("RUN1", True)
            assert pump(B, 5, B.ready), "never got welcome: " + B.status()
            assert B.seen() and B.problem() is None

            state = lua.table_from(STATE, recursive=True)
            # Ask early, then supersede with a fresher snapshot (same key).
            id1 = B.request("room_clear", "clear:1:84", state, lua.table_from({"enemies_left": 3}), 4000)
            time.sleep(0.05)
            id2 = B.request("room_clear", "clear:1:84", state, lua.table_from({"enemies_left": 1}), 4000)
            B.request("room_layout", "layout:1:85", state, lua.table_from({"room_type": "default"}), 8000)
            cands = lua.table_from({"candidates": [{"id": 1, "quality": 3}, {"id": 2, "quality": 0},
                                                   {"id": 3, "quality": 1}]}, recursive=True)
            B.request("item_choice", "items:1:0", state, cands, 8000)
            B.request("floor_plan", "floor:1:next", state, lua.table_from({}), 8000)

            keys = ["clear:1:84", "layout:1:85", "items:1:0", "floor:1:next"]
            assert pump(B, 6, lambda: all(B.peek(k) for k in keys)), "missing results"
            clear = B.peek("clear:1:84")
            assert clear.id == id2, f"expected superseding request {id2} to win, got {clear.id}"
            items = B.peek("items:1:0").decision
            order = [items.order[i] for i in range(1, len(items.order) + 1)]
            assert sorted(order) == [1, 2, 3] and order[0] == 2, order   # mock: worst quality first
            assert B.latencyAvg() > 0
            r = B.take("clear:1:84")
            assert r and not B.peek("clear:1:84")

            # The link's own round trip, shown on the debug HUD.
            assert pump(B, 5, lambda: "link" in B.status()), B.status()
            # Acks reach the backend, which drops what we've handled on its next write.
            assert pump(B, 5, lambda: len(json.loads(game["save"].read_text())["dm"]["msgs"]) <= 2)
            saved = json.loads(game["save"].read_text())
            assert saved["cruelty"] == 4 and saved["link"].startswith("RUN1-"), "the mod's data survives"
            print(f"  superseded id {id1} -> answered id {clear.id}; avg latency {B.latencyAvg():.0f}ms;"
                  f" p90 {B.latencyP90():.0f}ms; {B.status()}")
        finally:
            out = stop(srv)
        print("  server log:\n" + "\n".join("    " + line for line in out.strip().splitlines()))

        # The backend is gone: the bridge notices and says so.
        assert pump(B, 10, lambda: not B.ready()), "should notice the backend stopped"   # next ping + timeout
        assert B.problem().startswith("I'm not running")


# ------------------------------------------------------------------ a Python stand-in for the mod
class FakeMod:
    def __init__(self, game, chunk=900):
        self.log, self.save, self.chunk = game["log"], game["save"], chunk
        self.n, self.seen, self.boot = 0, 0, None

    def send(self, msg):
        line = json.dumps(msg, separators=(",", ":")).replace("%", "\\u0025")
        parts = [line[i:i + self.chunk] for i in range(0, len(line), self.chunk)]
        self.n += 1
        with open(self.log, "a", encoding="utf-8") as f:
            for i, part in enumerate(parts, 1):
                f.write(f"[INFO] - Lua Debug: [LLMDM:{self.n}:{i}/{len(parts)}]{part}\n")

    def file(self):
        try:
            return json.loads(self.save.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def save_own(self, data):
        self.save.write_text(json.dumps(data), encoding="utf-8")

    def recv(self, pred, secs=5):
        """New backend messages until one matches pred; returns it (receipt pushes are skipped)."""
        end = time.monotonic() + secs
        while time.monotonic() < end:
            dm = self.file().get("dm")
            if not dm:                           # the mod's own save, before the backend writes again
                time.sleep(0.02)
                continue
            if dm.get("boot") != self.boot:
                self.boot, self.seen = dm.get("boot"), 0
            for seq, msg in dm.get("msgs", []):
                if seq > self.seen:
                    self.seen = seq
                    if pred(msg):
                        return msg
            time.sleep(0.02)
        raise AssertionError("no matching message from the backend")


def test_protocol():
    with tempfile.TemporaryDirectory() as d:
        game = fake_game(d)
        srv = start_server(game, "--mock-latency", "0.6", "0.9")
        try:
            m = FakeMod(game, chunk=120)
            m.save_own({"link": "OLD", "cruelty": 9})
            m.send({"t": "hello", "session": "OLD", "proto": PROTO + 1, "run": "R", "new_run": True})
            w = m.recv(lambda x: x["t"] == "welcome")
            assert w == {"t": "welcome", "session": "OLD", "proto": PROTO}, "mismatch: just the backend's protocol"

            m.save_own({"link": "S", "cruelty": 9, "pills": {"3": "x"}})
            m.send({"t": "hello", "session": "S", "proto": PROTO, "run": "R", "new_run": True, "cruelty": 9})
            assert m.recv(lambda x: x["t"] == "welcome")["cruelty"] == 9
            m.send({"t": "settings", "cruelty": 3, "ack": m.seen, "boot": m.boot})
            assert m.recv(lambda x: x["t"] == "settings_ok")["cruelty"] == 3
            # A long request (several log lines, and a % that must survive the escaping) superseded by another.
            big = {**STATE, "note": "100% " + "x" * 400}
            m.send({"t": "req", "id": 1, "key": "k", "kind": "room_layout", "deadline_ms": 5000, "state": big, "ctx": {}})
            m.send({"t": "req", "id": 2, "key": "k", "kind": "room_layout", "deadline_ms": 5000, "state": big, "ctx": {}})
            m.send({"t": "req", "id": 3, "key": "t", "kind": "room_clear", "deadline_ms": 10, "state": STATE, "ctx": {}})
            got = {}
            while len(got) < 2:
                r = m.recv(lambda x: x["t"] == "res")
                got[r["key"]] = r
            assert got["k"]["id"] == 2 and got["k"]["ok"]                 # id 1 was cancelled
            assert got["t"]["ok"] is False and got["t"]["reason"] == "timeout"   # 400ms floor < 600ms mock

            # The mod saves over the file (dropping the backend's section): the backend puts it back.
            m.send({"t": "ping", "id": 7})
            time.sleep(0.15)
            own = {k: v for k, v in m.file().items() if k != "dm"}
            m.save_own({**own, "seed": "NEW"})
            assert m.recv(lambda x: x["t"] == "pong")["id"] == 7, "a message lost to the mod's save comes back"
            f = m.file()
            assert f["seed"] == "NEW" and f["pills"] == {"3": "x"} and f["cruelty"] == 9, "the mod's data is kept"

            # Acks: everything handled is dropped on the backend's next write.
            m.send({"t": "ack", "ack": m.seen, "boot": m.boot})
            m.send({"t": "ping", "id": 8})
            m.recv(lambda x: x["t"] == "pong")
            assert [seq for seq, _ in m.file()["dm"]["msgs"]] == [m.seen], "only the newest message is left"
            print("  protocol ok: mismatch, chunking, superseding, timeout, mod saves, acks")
        finally:
            stop(srv)


def test_log_tail():
    with tempfile.TemporaryDirectory() as d:
        log = Path(d) / "log.txt"
        log.write_bytes(b"[INFO] - Lua Debug: [LLMDM:1:1/1]{\"t\":\"old\"}\n")
        tail = LogTail(log)
        assert tail.messages() == [], "what was in the log before is skipped"
        with open(log, "ab") as f:
            f.write(b"[INFO] - something else\n[INFO] - Lua Debug: [LLMDM:2:1/2]{\"t\":\"a\",")
            f.write(b"\"x\":1}\n[INFO] - Lua Debug: [LLMDM:2:2/2]oops\n")      # not JSON once joined
            f.write(b"[INFO] - Lua Debug: [LLMDM:3:2/2]\"x\":1}\n")            # a message missing its first part
            f.write(b"[INFO] - Lua Debug: [LLMDM:4:1/2]{\"t\":\"b\",\r\n[INFO] - Lua Debug: [LLMDM:4:2/2]\"x\":2}\r\n")
            f.write(b"[INFO] - Lua Debug: [LLMDM:5:1/1]{\"t\":\"c\"")         # not finished yet
        assert tail.messages() == [{"t": "b", "x": 2}]
        with open(log, "ab") as f:
            f.write(b"}\n")
        assert tail.messages() == [{"t": "c"}], "a line is only read once it's complete"
        log.write_bytes(b"[INFO] - Lua Debug: [LLMDM:1:1/1]{\"t\":\"new game\"}\n")   # the game restarted
        assert tail.messages() == [{"t": "new game"}]


if __name__ == "__main__":
    for name, fn in [("lua bridge", test_lua_bridge), ("protocol", test_protocol), ("log tail", test_log_tail)]:
        print("TEST", name)
        fn()
        print("PASS", name)

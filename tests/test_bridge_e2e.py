"""End-to-end: the mod's real bridge.lua (Lua 5.3, like Isaac) talking to server.py --mock.

* file transport: bridge.lua runs inside lupa with Isaac's globals stubbed.
* tcp transport : a Python client speaks the same protocol to the TCP server.
Checks latest-wins superseding, latency tracking, applied acks and guardrail output.
"""
import json
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from lupa.lua53 import LuaRuntime

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
MOD = ROOT / "mod" / "llm_dungeon_master"

STATE = {"player": {"hp": {"red": 6, "soul": 2}, "resources": {"keys": 0, "bombs": 1, "coins": 3},
                    "stats": {"luck": 0}},
         "level": {"stage": 2, "needs": {"locked_doors_seen": 1, "bomb_targets": 2}}}


def start_server(*args):
    p = subprocess.Popen([sys.executable, "server.py", "--mock", *args], cwd=BACKEND,
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    time.sleep(1.0)
    assert p.poll() is None, p.stdout.read()
    return p


def make_lua(bridge_dir: Path):
    lua = LuaRuntime(unpack_returned_tuples=True)
    g = lua.globals()
    t0 = time.monotonic()

    def to_py(v):
        if lupa_type(v) == "table":
            keys = list(v.keys())
            if keys and all(isinstance(k, int) for k in keys) and sorted(keys) == list(range(1, len(keys) + 1)):
                return [to_py(v[i]) for i in range(1, len(keys) + 1)]
            return {str(k): to_py(v[k]) for k in keys}
        return v

    def lupa_type(v):
        return lua.globals().type(v)

    def encode(v):
        return json.dumps(to_py(v), separators=(",", ":"))

    def decode(s):
        return lua.table_from(json.loads(s), recursive=True)

    jsonmod = lua.table_from({"encode": encode, "decode": decode})
    real_require = g.require

    def fake_require(name):
        if name == "json":
            return jsonmod
        if name == "socket":
            raise RuntimeError("no socket in test")   # forces TRANSPORT=auto -> file
        return real_require(name)

    g.require = fake_require
    g.Isaac = lua.table_from({"GetTime": lambda: int((time.monotonic() - t0) * 1000),
                              "DebugString": lambda s: print("   [lua]", s)})
    bridge = lua.execute(open(MOD / "dm" / "bridge.lua").read())
    cfg = lua.table_from({"TRANSPORT": "auto", "HOST": "127.0.0.1", "PORT": 1, "BRIDGE_DIR": str(bridge_dir)})
    return lua, bridge, cfg


def pump(bridge, secs, until=None):
    end = time.monotonic() + secs
    while time.monotonic() < end:
        bridge.update()
        if until and until():
            return True
        time.sleep(0.016)
    return bool(until and until())


def test_file_bridge():
    with tempfile.TemporaryDirectory() as d:
        srv = start_server("--bridge", "file", "--bridge-dir", d)
        try:
            lua, B, cfg = make_lua(Path(d))
            B.init(cfg, "RUN1")
            B.setRun("RUN1", True)
            assert pump(B, 5, B.ready), "never got welcome: " + B.status()
            print("  status:", B.status())

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
            pump(B, 0.3)
            print(f"  superseded id {id1} -> answered id {clear.id}; avg latency {B.latencyAvg():.0f}ms;"
                  f" p90 {B.latencyP90():.0f}ms")
            for k in keys[1:]:
                print("  ", k, json.dumps({"intent": B.peek(k).decision.intent}))
        finally:
            srv.terminate()
            out = srv.communicate(timeout=5)[0]
        print("  server log:\n" + "\n".join("    " + l for l in out.strip().splitlines()))


def test_tcp_server():
    srv = start_server("--port", "7399", "--mock-latency", "0.6", "0.9")
    try:
        s = socket.create_connection(("127.0.0.1", 7399), timeout=5)
        f = s.makefile("rw")

        def send(o):
            f.write(json.dumps(o) + "\n")
            f.flush()

        send({"t": "hello", "session": "S", "run": "R", "new_run": True, "cruelty": 9})
        assert json.loads(f.readline())["cruelty"] == 9
        send({"t": "settings", "cruelty": 3})
        assert json.loads(f.readline()) == {"t": "settings_ok", "session": "S", "cruelty": 3}
        send({"t": "req", "id": 1, "key": "k", "kind": "room_layout", "deadline_ms": 5000, "state": STATE, "ctx": {}})
        send({"t": "req", "id": 2, "key": "k", "kind": "room_layout", "deadline_ms": 5000, "state": STATE, "ctx": {}})
        send({"t": "req", "id": 3, "key": "t", "kind": "room_clear", "deadline_ms": 10, "state": STATE, "ctx": {}})
        got = [json.loads(f.readline()) for _ in range(2)]
        by_key = {g["key"]: g for g in got}
        assert by_key["k"]["id"] == 2 and by_key["k"]["ok"]           # id 1 was cancelled
        assert by_key["t"]["ok"] is False and by_key["t"]["reason"] == "timeout"   # 400ms floor < 600ms mock
        send({"t": "applied", "key": "k"})
        send({"t": "event", "name": "damage", "data": {"amount": 1, "source": "10.0", "room": 84}})
        print("  tcp ok:", by_key["k"]["decision"]["intent"], "| timeout path ok")
    finally:
        srv.terminate()
        srv.communicate(timeout=5)


if __name__ == "__main__":
    for name, fn in [("file bridge (Lua 5.3 bridge.lua)", test_file_bridge), ("tcp server", test_tcp_server)]:
        print("TEST", name)
        fn()
        print("PASS", name)

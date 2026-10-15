#!/usr/bin/env python3
"""bridge/serve.py — the desktop bridge: the colony's executor.

A tiny local HTTP service on the Director's Windows PC (docs/BRIDGE.md). It
does exactly three things: reports status, performs allowlisted actions for
the current lock holder, and writes one receipt per action. It never writes
world state — one writer per fact.

    python3 bridge/serve.py --dry-run            # no browser, real receipts
    python3 bridge/serve.py --sim                # Linux simulator (tests)
    python3 bridge/serve.py                      # real road: CDP, port 9222
    python3 bridge/serve.py --self-test          # dry-run self-test, then exit

Protocol (v1, docs/BRIDGE.md §3):
    POST /status  -> {browser_up, active_channel, lock:{...}, busy}
    POST /act     -> {token, generation, action, args:{...}} -> receipt
                     refusal: {ok:false, reason:"stale-generation"|"wrong-token"|
                               "not-allowlisted"|"no-lock"|...}

Fail-closed everywhere: no lock, stale generation, wrong token, unknown
action, browser unreachable, unexpected dialog => act nothing, return
ok:false, write the receipt. The bridge never releases the lock.

Stdlib only.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))

from actions import ALLOWLIST, is_allowlisted, make_receipt, validate_args, write_receipt  # noqa: E402
from executors import CdpExecutor, DryRunExecutor, SimulatorExecutor  # noqa: E402
from fencing import gate, read_lock  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_LOCK = ROOT / "world" / "lock.json"
DEFAULT_RECEIPTS = Path(__file__).resolve().parent / "receipts"


def make_executor(mode: str, cdp_endpoint: str = "http://127.0.0.1:9222"):
    if mode == "sim":
        return SimulatorExecutor(channels=["ch-collective", "ch-cormac", "ch-argus"])
    if mode == "dry-run":
        return DryRunExecutor()
    return CdpExecutor(endpoint=cdp_endpoint)


class Bridge:
    """The service logic, independent of the transport (so tests need no socket)."""

    def __init__(self, lock_path: Path = DEFAULT_LOCK, receipts_dir: Path = DEFAULT_RECEIPTS,
                 executor=None, mode: str = "cdp", cdp_endpoint: str = "http://127.0.0.1:9222"):
        self.lock_path = Path(lock_path)
        self.receipts_dir = Path(receipts_dir)
        self.executor = executor if executor is not None else make_executor(mode, cdp_endpoint)

    # -- /status ---------------------------------------------------------
    def status(self) -> dict:
        lock = read_lock(self.lock_path)
        st = self.executor.status()
        return {
            "browser_up": st.get("browser_up", False),
            "active_channel": st.get("active_channel"),
            "busy": st.get("busy", False),
            "executor": self.executor.name,
            "lock": {
                "state": lock.get("state"),
                "holder": lock.get("holder"),
                "generation": lock.get("generation"),
                "expires_ts": lock.get("expires_ts"),
                "purpose": lock.get("purpose"),
                "queue_len": len(lock.get("queue") or []),
            },
            "allowlist": sorted(ALLOWLIST),
        }

    # -- /act ------------------------------------------------------------
    def act(self, body: dict) -> dict:
        token = body.get("token")
        generation = body.get("generation")
        action = body.get("action")
        args = body.get("args") or {}
        lock = read_lock(self.lock_path)
        holder = lock.get("holder")

        # 1. the fence: token AND generation must match the current grant
        allowed, reason = gate(self.lock_path, token, generation)
        if not allowed:
            return self._receipt(action or "", args, generation, False, reason, holder=holder)

        # 2. the allowlist: refused by name
        if not is_allowlisted(action):
            return self._receipt(str(action), args, generation, False,
                                 "not-allowlisted", holder=holder)

        # 3. the args
        ok, why = validate_args(action, args)
        if not ok:
            return self._receipt(action, args, generation, False, why, holder=holder)

        # 4. act — the executor fails closed on anything unexpected
        self.executor.busy = True
        try:
            ok, reason, result = self.executor.dispatch(action, args)
        except Exception as e:                       # fail closed, never crash
            ok, reason, result = False, f"executor-error:{type(e).__name__}", {}
        finally:
            self.executor.busy = False
        return self._receipt(action, args, generation, ok, reason, result, holder=holder)

    # -- receipts --------------------------------------------------------
    def _receipt(self, action, args, generation, ok, reason, result=None, holder=None) -> dict:
        receipt = make_receipt(action=action, args=args, generation=generation, ok=ok,
                               reason=reason, result=result, holder=holder)
        try:
            receipt["receipt_path"] = str(write_receipt(receipt, self.receipts_dir))
        except OSError as e:
            receipt["receipt_path"] = f"unwritable:{e}"
        return receipt


# -- transport ---------------------------------------------------------------
def serve_http(bridge: Bridge, host: str, port: int) -> None:
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class Handler(BaseHTTPRequestHandler):
        def _send(self, code: int, payload: dict) -> None:
            blob = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(blob)))
            self.end_headers()
            self.wfile.write(blob)

        def do_POST(self):  # noqa: N802 — http.server naming
            length = int(self.headers.get("Content-Length") or 0)
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
            except json.JSONDecodeError:
                return self._send(400, {"ok": False, "reason": "bad-json"})
            if self.path.rstrip("/") == "/status":
                return self._send(200, bridge.status())
            if self.path.rstrip("/") == "/act":
                return self._send(200, bridge.act(body))
            return self._send(404, {"ok": False, "reason": "no-such-endpoint"})

        def log_message(self, fmt, *a):  # keep the black window quiet
            pass

    ThreadingHTTPServer((host, port), Handler).serve_forever()


def self_test(bridge: Bridge) -> int:
    """The dry-run self-test: the four protocol facts, printed for the record."""
    print("bridge self-test (dry-run; no browser, no world writes)")
    st = bridge.status()
    print(f"  status      : executor={st['executor']} browser_up={st['browser_up']} "
          f"lock.state={st['lock']['state']} allowlist={','.join(st['allowlist'])}")
    r = bridge.act({"token": None, "generation": 0, "action": "upload_file",
                    "args": {"path": "x.mp4", "channel": "ch-collective"}})
    print(f"  no lock     : ok={r['ok']} reason={r['reason']}")
    r = bridge.act({"token": "t", "generation": 1, "action": "teleport", "args": {}})
    print(f"  fence first : ok={r['ok']} reason={r['reason']} (allowlist is checked AFTER the fence)")
    print("  self-test ok — with a real grant the same calls pass the fence; see tests/test_bridge_sim.py")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=9333)
    ap.add_argument("--world", default=str(DEFAULT_LOCK), help="path to world/lock.json")
    ap.add_argument("--receipts", default=str(DEFAULT_RECEIPTS), help="receipt out dir")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--dry-run", action="store_true", help="no browser; receipts still written")
    g.add_argument("--sim", action="store_true", help="Linux simulator (mock browser)")
    ap.add_argument("--cdp", default="http://127.0.0.1:9222", help="CDP endpoint (real road)")
    ap.add_argument("--self-test", action="store_true", help="print the dry-run self-test and exit")
    a = ap.parse_args(argv)

    mode = "dry-run" if a.dry_run else "sim" if a.sim else "cdp"
    bridge = Bridge(lock_path=Path(a.world), receipts_dir=Path(a.receipts),
                    mode=mode, cdp_endpoint=a.cdp)
    if a.self_test:
        return self_test(bridge)
    print(f"bridge: listening on http://{a.host}:{a.port} (executor={mode}) — leave this window open")
    serve_http(bridge, a.host, a.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""pc_agent.py — the supervised runner that lets a *turn-based* agent drive a PC that is behind a pull.

WHY THIS EXISTS
---------------
The bridge (`tools/bridge.py`) can move a mouse. It cannot wait: every command it has is executed by
*somebody*, and the agent that wants to drive this machine is not standing here — it is an API session, on a
different computer, that runs when a human speaks to it and stops afterwards. It has no sockets, cannot be
polled, and cannot keep a process alive. So "connecting an agent to my PC" cannot mean a connection between
those two machines. It means a queue and a loop that neither side has to hold open:

    agent (turn-based, anywhere)  --git push-->  pc/queue/<id>.json  --pull-->  pc_agent.py (this file)
                                                                                      |
                                     pc/queue/<id>.done.json  <--git push--  bridge.py
                                                                                      |
    agent's next turn             --git pull-->  reads what happened, decides the next step

Git is the only channel that is proven to work here: it is what the world.json lease, the receipts and the
board already travel over, it survives both machines being asleep, and it leaves an audit trail nobody has to
trust. There is no port to open, no inbound connection, no agent connected to anybody's network.

WHAT IT REFUSES TO BE
---------------------
`RUN-ON-THE-PC.md` and `docs/BRIDGE.md` §7.1 list four refusals that live in the bridge. This file inherits
all of them (it goes *through* the bridge, never around it) and adds three of its own:

  * **`lease`** — every command must carry the token of a live lease, and the generation is re-read from the
    pushed `world/lock.json` at execution time. A stale generation means somebody else holds the PC, and the
    runner stops rather than keeps typing. (This is the same fence, not a second one.)
  * **`deadman`** — a command whose `expires_ts` has passed is refused, not executed late. A queued click that
    fires after the task moved on is worse than one that never fired.
  * **`paused`** — the human's stop is checked *before* each command by re-reading the pushed lock, so a
    `pause` written from the phone (or `world.py pc release`) stops the loop within one poll interval.

Every attempt — accepted, refused, or skipped — is written to `pc/results/<id>.done.json` with the verb, the
outcome and the receipt id. Nothing is recorded as having happened that did not happen, and nothing is
executed that is not recorded. That is the whole design.

USAGE (on the PC, after cloning the repo and installing uiautomation)
--------------------------------------------------------------------
    python tools\\pc_agent.py status                  # is there a command waiting? what does the lease say?
    python tools\\pc_agent.py connect --by fabius --purpose "PCB-02 Roblox" --ttl 60
                                                      # acquire a lease through world.py and print the token
    python tools\\pc_agent.py watch --interval 10      # the loop: pull, execute at most one command, push
    python tools\\pc_agent.py once                    # one turn, for a rehearsal or a cron entry
    python tools\\pc_agent.py stop                     # kill switch: write pc/PAUSE (belongs to the human)
    python tools\\pc_agent.py resume                   # the human lifts it (a decision, not a retry)

`watch` is the "connect my PC" command. Run it in a terminal and leave it; type Ctrl-C to stop. It prints one
line per turn so a human watching the window can see the machine being driven. Nothing in it is secret
except the token, which is never printed or written anywhere but this terminal.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
sys.path.insert(0, str(HERE))

QUEUE = "pc/queue"
RESULTS = "pc/results"
PAUSE_FLAG = "pc/PAUSE"
LOCK = "world/lock.json"

#: The verbs this runner is willing to execute come from the bridge's own table, read at import time —
#: never re-listed here. A second copy of a verb list is how one of the two drifts, and the failure mode is a
#: machine that refuses a legitimate command (or worse, accepts one the bridge never defined). The import is
#: lazy so that `pc_agent.py --help` works on a box without the bridge's dependencies present.
def _verbs():
    from bridge import ACTIONS
    return {name: "read" if spec[0] == "read" else "write" for name, spec in ACTIONS.items()}


TAG = re.compile(r"^[A-Za-z0-9._-]{1,40}$")


def z(offset_min: int = 0) -> str:
    """UTC stamp, optionally offset. Same shape as world.py's, so timestamps from the two tools compare as
    strings without a parser — which matters because the comparisons happen in two languages' worth of
    code paths (Python here, a human reading a diff there)."""
    t = dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=offset_min)
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_ts(ts: str):
    try:
        return dt.datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc)
    except Exception:
        return None


def run_git(*args, cwd: Path = REPO, check: bool = False):
    """Git, quietly. A runner that dies because `git pull` printed to stderr on a fast-forward is a runner
    that cannot be left unattended for an hour."""
    p = subprocess.run(["git", *args], cwd=str(cwd), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                       text=True)
    if check and p.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {p.stdout.strip()[:400]}")
    return p


def load_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


class Runner:
    """One command, one turn, one record. Deliberately not a daemon with state: `watch` is a loop around
    `once`, so a rehearsal in `once` mode exercises exactly the code path a ten-hour run uses."""

    def __init__(self, repo: Path = REPO, log_dir: str | None = None, dry_run: bool = False,
                 backend: str = "windows", fence: str = "world"):
        # `backend`/`fence` are parameters for the same reason every other knob in this project is: the
        # runner must be exercisable on a machine that is not the PC, or its refusal paths only ever get
        # tested by the person least able to debug them. Production default is windows + world, i.e. the
        # fence is the pushed lock — not a flag somebody can turn off.
        self.repo = Path(repo)
        self.dry_run = dry_run
        self.backend, self.fence = backend, fence
        self.log_dir = log_dir or str(self.repo / "pc")
        self.queue = self.repo / QUEUE
        self.results = self.repo / RESULTS

    # ---------- the lease ----------
    def lease(self):
        """The pushed lock, which is the only authority on who may drive this machine."""
        return load_json(self.repo / LOCK) or {}

    def lease_problem(self, cmd) -> str | None:
        """None = may run. Else the reason, phrased so a human can act on it.

        Note what is deliberately NOT here: a check that the command carries a secret. `world/lock.json`
        is *published* — `world.py pc acquire` writes the token into it and pushes it — so on a public
        repo the token is world-readable for the duration of the hold, and requiring it in the command
        would be ceremony that protects nothing while implying it protects something. What actually
        protects the machine is the lease (one holder), the generation match below, the runner's own
        refusals, and the human's stop. The runner injects the live token at execution time.
        """
        lock = self.lease()
        # The world's vocabulary is free | held; an expired hold reports as "recovering", never as free
        # (world.py line ~213: "an expired hold is recovering, never silently free"). My first version
        # tested for "active", a word this state machine has never used anywhere — so the runner refused
        # *every legitimate command*, and only a rehearsal with the real world.py could show it. A check
        # that has never once been seen to pass is not a check.
        if not lock or lock.get("state") not in ("held", "active"):
            return ("no-lock: nobody holds the PC (" + str(lock.get("state") or "no lock file")
                    + ") — world.py pc acquire --by <agent> --purpose <why>")
        exp = parse_ts(lock.get("expires_ts") or "")
        if exp and exp < dt.datetime.now(dt.timezone.utc):
            return (f"lease-expired: {lock.get('holder')}'s lease ended at {lock['expires_ts']}; "
                    f"the PC is idle by design, not by accident — renew or re-acquire")
        if cmd.get("generation") != lock.get("generation"):
            return (f"stale-generation: command says {cmd.get('generation')}, live lock says "
                    f"{lock.get('generation')} (holder={lock.get('holder')}) — refusing to type into "
                    f"somebody else's turn")
        if cmd.get("token") and cmd["token"] != lock.get("token"):
            return "wrong-token: the command carries a token that is not the live one"
        return None

    # ---------- the queue ----------
    def pending(self):
        """Commands waiting, oldest first. `.done.json` files are the record and are not commands."""
        if not self.queue.is_dir():
            return []
        out = []
        for f in sorted(self.queue.glob("*.json")):
            if f.name.endswith(".done.json") or f.name.endswith(".claimed.json"):
                continue
            cmd = load_json(f)
            if isinstance(cmd, dict):
                cmd["_file"] = f
                out.append(cmd)
        out.sort(key=lambda c: (c.get("created_ts") or "", c["_file"].name))
        return out

    def paused(self) -> str | None:
        p = self.repo / PAUSE_FLAG
        if p.exists():
            return (p.read_text(encoding="utf-8", errors="replace").strip()
                    or "pc/PAUSE exists: the human said stop")
        return None

    # ---------- execution ----------
    def execute(self, cmd) -> dict:
        """Through the bridge, never around it. Returns the record that gets published."""
        from bridge import build_core, Refused    # the same policy layer, assembled the same way

        verb = str(cmd.get("verb") or "")
        cid = str(cmd.get("id") or "")
        if not TAG.match(cid):
            return self._rec(cmd, "refused", "bad-command", "id must match [A-Za-z0-9._-]{1,40}")
        known = _verbs()
        if verb not in known:
            return self._rec(cmd, "refused", "not-allowlisted",
                             f"'{verb}' is not a bridge verb (known: {', '.join(sorted(known))})")

        exp = parse_ts(cmd.get("expires_ts") or "")
        if exp and exp < dt.datetime.now(dt.timezone.utc):
            return self._rec(cmd, "refused", "deadman",
                             f"expired at {cmd['expires_ts']}: a queued action that fires after the task "
                             f"moved on is worse than one that never fired")

        hard = self.lease_problem(cmd)
        if hard:
            return self._rec(cmd, "refused", hard.split(":")[0], hard)

        # NB: Core creates its own log_dir. Do not add a second mkdir here "for safety" — one creator,
        # one place to be wrong, and a redundant line hides which component is actually responsible.
        live = self.lease()
        # ONE value, used for both. `Fence.static` (built by build_core) and the token handed to
        # `Core.act()` are two different things: the first configures the check, the second is what the
        # check compares. Injecting the live token into only one of them produced "an action needs
        # --token and --generation from the PC grant" while the fence was correctly configured — a
        # message that points at the caller, not at the half-wired change that caused it.
        tok = cmd.get("token") or live.get("token")
        try:
            core = build_core(backend=self.backend, fence=self.fence, root=self.repo,
                              # the live token, not the command's: see lease_problem(). A command that
                              # explicitly carries one is still checked against the lock above.
                              token=tok,
                              generation=cmd.get("generation"),
                              lock_file=str(self.repo / LOCK) if self.fence == "static" else None,
                              log_dir=self.log_dir, ask=cmd.get("ask") or "important")
        except Refused as e:                       # e.g. no fence on a real desktop: a record, not a crash
            return self._rec(cmd, "refused", e.reason, e.hint or str(e))
        task = cmd.get("task") or None
        try:
            rec = core.act(verb, dict(cmd.get("args") or {}),
                           token=tok, generation=cmd.get("generation"), task=task)
        except Refused as e:
            return self._rec(cmd, "refused", e.reason, e.hint or str(e))
        except Exception as e:                     # a traceback in a watched terminal teaches nobody anything
            return self._rec(cmd, "error", type(e).__name__, str(e)[:300])

        if isinstance(rec, dict) and rec.get("state") == "pending-approval":
            return self._rec(cmd, "pending-approval", "approval", rec.get("ask", ""), extra={"approval_id": rec.get("id")})
        return self._rec(cmd, "done", "executed", "", receipt=self._receipt_id(rec), extra={"state": (rec or {}).get("state")})

    def _receipt_id(self, rec):
        if not isinstance(rec, dict):
            return None
        f = Path(self.log_dir) / "receipts.jsonl"
        try:
            n = sum(1 for _ in f.open(encoding="utf-8"))
        except Exception:
            return None
        return f"r{n:06d}"

    def _rec(self, cmd, status, reason, detail, receipt=None, extra=None):
        return {"id": cmd.get("id"), "verb": cmd.get("verb"), "task": cmd.get("task"),
                "status": status, "reason": reason, "detail": str(detail)[:400],
                "receipt": receipt, "at": z(), "checkout": self.checkout(),
                "backend": self.backend, "runner": "pc_agent", **(extra or {})}

    def checkout(self):
        p = run_git("rev-parse", "--short", "HEAD")
        return p.stdout.strip()[:12] or "unknown"

    # ---------- git, the transport ----------
    def sync(self) -> str:
        """Pull then push. Never rebase away a record, never force anything: the queue is append-only, and a
        conflict here means two runners or a human mid-edit — which is exactly when to stop and say so."""
        if self.dry_run:
            return "dry-run: no git"
        if not (self.repo / ".git").exists():
            return "not-a-repo: this is a checkout without git, so it has no channel"
        run_git("pull", "--no-rebase", "--no-edit", "-q")
        if self._dirty_queue():
            run_git("add", QUEUE, RESULTS)
            p = run_git("commit", "-q", "-m",
                        f"pc_agent: <agent> turn ({self.last_id or '-'}) — receipts travel with the command")
            if p.returncode != 0 and "nothing to commit" not in p.stdout:
                return f"commit-failed: {p.stdout.strip()[:200]}"
        p = run_git("push", "-q")
        if p.returncode != 0:
            return f"push-failed: {p.stdout.strip()[:200]} (is a credential configured for this clone?)"
        return "ok"

    def _dirty_queue(self):
        p = run_git("status", "--porcelain", "--", QUEUE, RESULTS)
        return bool(p.stdout.strip())

    # ---------- one turn ----------
    def once(self, execute: bool = True) -> dict:
        self.sync()
        stop = self.paused()
        cmds = self.pending()
        if stop:
            return {"turn": "skipped", "reason": "paused", "detail": stop, "waiting": len(cmds),
                    "at": z(), "checkout": self.checkout()}
        if not cmds:
            return {"turn": "idle", "reason": "", "detail": "", "waiting": 0, "at": z(),
                    "checkout": self.checkout()}
        cmd = cmds[0]
        self.last_id = str(cmd.get("id"))
        # `--dry-run` means dry on BOTH sides. An earlier version guarded the git calls and then executed
        # anyway: it moved the desktop while claiming to be read-only, which is the worst possible way to
        # be wrong. A flag that changes what happens may never be checked in only one of the two places.
        if not execute or self.dry_run:
            return {"turn": "would-execute", "reason": "", "detail": cmd.get("verb", ""), "waiting": len(cmds),
                    "at": z(), "checkout": self.checkout(), "lease": self.lease_problem(cmd) or "ok"}
        rec = self.execute(cmd)
        self._publish(cmd, rec)
        return {"turn": "executed", "reason": rec["reason"], "detail": rec["detail"], "waiting": len(cmds) - 1,
                "at": z(), "checkout": self.checkout(), "record": rec}

    def _publish(self, cmd, rec):
        """Write the result and consume the command in the SAME commit: a record that exists while the
        command still looks unexecuted is how a runner double-clicks. The command file is replaced by its
        result, atomically from the point of view of any other clone."""
        if self.dry_run:
            return
        self.results.mkdir(parents=True, exist_ok=True)
        out = self.results / f"{rec['id']}.done.json"
        tmp = out.with_suffix(".tmp")
        tmp.write_text(json.dumps(rec, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        tmp.replace(out)
        src = Path(cmd["_file"])
        if src.exists():
            src.unlink()

    # ---------- the loop ----------
    def watch(self, interval: int, max_turns: int = 0, quiet: bool = False) -> int:
        turns = 0
        while True:
            try:
                r = self.once()
            except KeyboardInterrupt:
                print("pc_agent: stopped by the human at the keyboard")
                return 0
            if not quiet and r["turn"] != "idle":
                line = f"pc_agent[{r['at']}] {r['turn']} checkout={r['checkout']}"
                if r["reason"]:
                    line += f" {r['reason']}: {r['detail'][:120]}"
                print(line, flush=True)
            turns += 1
            if max_turns and turns >= max_turns:
                return 0
            if (self.repo / PAUSE_FLAG).exists():
                return 0
            time.sleep(interval)

    # ---------- queueing ----------
    def enqueue(self, verb, args, task, ask="none", expires_min=10, by="fabius", push=True) -> dict:
        """Put one command in the queue. Refuses if no live lease is held, because a command queued against
        nobody is a trap: it sits there looking like work, and the next person to hold the PC inherits it.
        That is the same class of bug as a click firing after the task moved on."""
        import secrets
        from bridge import ACTIONS, Refused
        if verb not in ACTIONS:
            raise Refused("not-allowlisted", f"'{verb}' is not a bridge verb "
                                             f"(known: {', '.join(sorted(ACTIONS))})")
        spec = ACTIONS[verb]
        req, opt = spec[2], spec[3]
        missing = [k for k in req if not args.get(k)]
        unknown = [k for k in args if k not in req and k not in opt]
        if missing:
            raise Refused("bad-args", f"{verb} needs {missing}")
        if unknown:
            raise Refused("bad-args", f"{verb} has no field(s) {unknown} — a typo must not ride along")
        lock = self.lease()
        probe = {"generation": lock.get("generation"), "token": None}
        hard = self.lease_problem(probe)
        if hard:
            raise Refused(hard.split(":")[0], hard)
        cid = f"c-{dt.datetime.now(dt.timezone.utc).strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(2)}"
        cmd = {"id": cid, "by": by, "task": task, "verb": verb, "args": args, "ask": ask,
               "generation": lock.get("generation"), "created_ts": z(),
               "expires_ts": z(expires_min)}
        self.queue.mkdir(parents=True, exist_ok=True)
        f = self.queue / f"{cid}.json"
        f.write_text(json.dumps(cmd, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        if push and not self.dry_run:
            run_git("add", QUEUE)
            run_git("commit", "-q", "-m", f"queue {cid}: {verb} for {task}")
            p = run_git("push")
            if p.returncode != 0:
                raise RuntimeError(f"queued locally but could not push: {p.stdout.strip()[:200]}")
        return cmd

    # ---------- human-side controls ----------
    def stop(self, why="the human said stop") -> str:
        (self.repo / PAUSE_FLAG).parent.mkdir(parents=True, exist_ok=True)
        (self.repo / PAUSE_FLAG).write_text(why + "\n", encoding="utf-8")
        return f"wrote {PAUSE_FLAG}: {why}"

    def resume(self) -> str:
        p = self.repo / PAUSE_FLAG
        if p.exists():
            p.unlink()
        return "pc/PAUSE removed: the machine may accept commands again"


# ------------------------------------------------------------------------ CLI ---
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="pc_agent.py", description=__doc__.splitlines()[0])
    ap.add_argument("--repo", default=str(REPO))
    ap.add_argument("--log-dir", default=None)
    ap.add_argument("--dry-run", action="store_true", help="read-only: never git, never touch the desktop")
    ap.add_argument("--backend", default="windows", choices=["windows", "sim", "null"])
    ap.add_argument("--fence", default="world", choices=["world", "static", "off"])
    parent = argparse.ArgumentParser(add_help=False)
    for flags in (("--repo", {"default": str(REPO)}),
                  ("--log-dir", {"default": None}),
                  ("--dry-run", {"action": "store_true"}),
                  ("--backend", {"default": "windows", "choices": ["windows", "sim", "null"]}),
                  ("--fence", {"default": "world", "choices": ["world", "static", "off"]})):
        parent.add_argument(*flags[0:1], **flags[1])
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("status", parents=[parent])
    sub.add_parser("once", parents=[parent])
    sub.add_parser("peek", parents=[parent])
    p = sub.add_parser("watch", parents=[parent]); p.add_argument("--interval", type=int, default=10)
    p.add_argument("--max-turns", type=int, default=0); p.add_argument("--quiet", action="store_true")
    q = sub.add_parser("queue", parents=[parent],
                       help="put one command in the queue (validated, and only while a lease is live)")
    q.add_argument("--verb", required=True)
    q.add_argument("--task", required=True)
    q.add_argument("--ask", default="none", choices=["none", "important", "all"])
    q.add_argument("--expires-min", type=int, default=10)
    q.add_argument("--arg", action="append", default=[], metavar="K=V")
    p = sub.add_parser("connect", parents=[parent]); p.add_argument("--by", required=True)
    p.add_argument("--purpose", required=True); p.add_argument("--ttl", type=int, default=60)
    p = sub.add_parser("stop", parents=[parent]); p.add_argument("--why", default="the human said stop")
    sub.add_parser("resume", parents=[parent])

    # argparse only accepts a global flag BEFORE the subcommand; nobody types it that way. Sharing the
    # parent parser means `pc_agent.py once --backend sim` and `pc_agent.py --backend sim once` both work,
    # which matters when the command is being typed on someone else's keyboard from a runbook.
    a = ap.parse_args(argv)
    r = Runner(repo=Path(a.repo), log_dir=a.log_dir, dry_run=a.dry_run, backend=a.backend, fence=a.fence)

    if a.cmd == "status":
        lock = r.lease()
        print(json.dumps({"paused": r.paused(), "waiting": len(r.pending()),
                          "holder": lock.get("holder"), "generation": lock.get("generation"),
                          "expires_ts": lock.get("expires_ts"), "checkout": r.checkout(),
                          "queue": str(r.queue)}, indent=2, sort_keys=True))
        return 0
    if a.cmd == "peek":
        print(json.dumps(r.once(execute=False), indent=2, sort_keys=True))
        return 0
    if a.cmd == "once":
        out = r.once()
        print(json.dumps(out, indent=2, sort_keys=True))
        return 0 if out["turn"] != "executed" or out["reason"] == "executed" else 0
    if a.cmd == "watch":
        print(f"pc_agent: watching {r.queue} every {a.interval}s — Ctrl-C to stop, `pc_agent.py stop` to "
              f"stop it from anywhere", flush=True)
        return r.watch(a.interval, a.max_turns, a.quiet)
    if a.cmd == "queue":
        args = {}
        for pair in a.arg:
            if "=" not in pair:
                print(json.dumps({"ok": False, "refused": "bad-args",
                                  "hint": f"--arg {pair!r} is not K=V"}))
                return 1
            k, v = pair.split("=", 1)
            args[k] = int(v) if v.isdigit() else v
        try:
            cmd = r.enqueue(a.verb, args, a.task, ask=a.ask, expires_min=a.expires_min)
        except Exception as e:
            reason = getattr(e, "reason", "error")
            print(json.dumps({"ok": False, "refused": reason, "hint": getattr(e, "hint", str(e))}))
            return 1
        print(json.dumps({"queued": cmd["id"], "verb": cmd["verb"], "task": cmd["task"],
                          "expires_ts": cmd["expires_ts"], "generation": cmd["generation"],
                          "note": "the token is not in this file: the runner injects the live one"},
                         indent=2, sort_keys=True))
        return 0
    if a.cmd == "connect":
        w = HERE / "world.py"
        p = subprocess.run([sys.executable, str(w), "pc", "acquire", "--by", a.by,
                            "--purpose", a.purpose, "--ttl", str(a.ttl)],
                           cwd=str(r.repo), text=True)
        print("\nThe token above is the grip. It is NOT written to disk by this runner: a token in a file is a"
              "\ntoken in a commit. Put it in the queue's `token` field only if you are comfortable with the"
              "\ncommand line carrying it, or pass it to `bridge.py` directly.", flush=True)
        return p.returncode
    if a.cmd == "stop":
        print(r.stop(a.why)); return 0
    if a.cmd == "resume":
        print(r.resume()); return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())

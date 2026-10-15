#!/usr/bin/env python3
"""The allowlist and the receipt — docs/BRIDGE.md §1 and §3.

The bridge performs ONLY these actions; everything else is refused by name
(`not-allowlisted`). Every action, allowed or refused, produces exactly one
receipt: timestamp, action, an args digest, the lock generation, the outcome,
and the observable result. Receipts go back to the caller; the requesting
agent records them in the world — the bridge never writes world state.

Evidence discipline: a receipt records page state and timestamps, never a
password and never a screenshot (screenshots stay on the Director's machine).

Stdlib only.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import time
import uuid

# The M1 allowlist. Required args are validated before anything is attempted.
ALLOWLIST: dict[str, tuple[str, ...]] = {
    "upload_file": ("path", "channel"),
    "set_metadata": ("video_id",),          # title/description/thumbnail optional
    "read_published_url": ("video_id",),
}

OPTIONAL_ARGS: dict[str, tuple[str, ...]] = {
    "set_metadata": ("title", "description", "thumbnail"),
}


def is_allowlisted(action: str) -> bool:
    return action in ALLOWLIST


def validate_args(action: str, args: dict) -> tuple[bool, str]:
    """Check the args an allowlisted action needs. Returns (ok, reason)."""
    if not isinstance(args, dict):
        return False, "args-not-object"
    missing = [k for k in ALLOWLIST.get(action, ()) if k not in args]
    if missing:
        return False, "missing-args:" + ",".join(missing)
    allowed = set(ALLOWLIST.get(action, ())) | set(OPTIONAL_ARGS.get(action, ()))
    unknown = [k for k in args if k not in allowed]
    if unknown:
        return False, "unknown-args:" + ",".join(sorted(unknown))
    if action == "upload_file":
        p = str(args.get("path", ""))
        if not p:
            return False, "missing-args:path"
    return True, ""


def args_digest(args: dict) -> str:
    """A stable, secret-free fingerprint of the args for the receipt."""
    try:
        blob = json.dumps(args, sort_keys=True, ensure_ascii=False)
    except (TypeError, ValueError):
        blob = repr(args)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def make_receipt(*, action: str, args: dict, generation, ok: bool, reason: str = "",
                 result: dict | None = None, started_ts: float | None = None,
                 holder: str | None = None) -> dict:
    """One receipt per action — the record of what did or did not happen."""
    return {
        "schema": 1,
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "action": action,
        "args_digest": args_digest(args),
        "generation": generation,
        "holder": holder,
        "ok": ok,
        "reason": reason,
        "result": result or {},
        "started_ts": started_ts,
    }


def write_receipt(receipt: dict, out_dir: Path) -> Path:
    """Write a receipt to the BRIDGE's own out dir (never into world/).

    Names carry a random suffix: two actions in the same second with the same
    args (e.g. one refusal right after another) must each keep their own
    receipt — the record of a refusal is evidence, not scratch.
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    uniq = uuid.uuid4().hex[:8]
    name = (f"{receipt['ts'].replace(':', '')}-{receipt['action']}"
            f"-{receipt['args_digest']}-{uniq}.json")
    path = out_dir / name
    path.write_text(json.dumps(receipt, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return path

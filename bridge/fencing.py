#!/usr/bin/env python3
"""Fencing — the gate every bridge action passes through (docs/WORLD.md §4).

The world grants a lock; the bridge is the only thing that can act. Acting on
a stale grant is the one failure that must be impossible: a crashed controller
must not be able to keep publishing. So before any action the bridge compares
the caller's token AND generation against the current grant in world/lock.json
and refuses, with a machine-readable reason, on any mismatch.

The bridge NEVER writes the lock or any world state — one writer per fact
(docs/BRIDGE.md §1). This module only reads.

Reasons (stable strings; receipts and tests depend on them):
    no-lock          nobody holds the PC — nothing is granted
    stale-generation generation does not match the current grant
    wrong-token      generation matches but the token does not
    expired          the grant is past expires_ts (state must go via sweep)

Stdlib only.
"""
from __future__ import annotations

import json
from pathlib import Path
import time


def utc_now() -> float:
    return time.time()


def read_lock(path: Path) -> dict:
    """Read world/lock.json. Missing/unreadable file => treated as no grant."""
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def check(lock: dict, token: str | None, generation, now: float | None = None) -> tuple[bool, str]:
    """Return (allowed, reason). allowed True => reason is ""."""
    now = utc_now() if now is None else now
    holder = lock.get("holder")
    if not holder or lock.get("state") not in ("held", "granted"):
        return False, "no-lock"

    expires = lock.get("expires_ts")
    if isinstance(expires, (int, float)) and now > expires:
        return False, "expired"

    try:
        want_gen = int(lock.get("generation"))
    except (TypeError, ValueError):
        return False, "no-lock"
    try:
        got_gen = int(generation)
    except (TypeError, ValueError):
        return False, "stale-generation"
    if got_gen != want_gen:
        return False, "stale-generation"

    if token is None or token != lock.get("token"):
        return False, "wrong-token"
    return True, ""


def gate(lock_path: Path, token: str | None, generation, now: float | None = None):
    """Convenience: read the lock file and check. Returns (allowed, reason)."""
    return check(read_lock(lock_path), token, generation, now)

#!/usr/bin/env python3
"""tools/world_build.py — compile world/ into app/world.json for the map (docs/WORLD.md §6).

    python3 tools/world_build.py --out app/world.json            # compile
    python3 tools/world_build.py --check app/world.json          # the phantom check
    python3 tools/world_build.py --preview preview.html          # one self-contained file

Deterministic: with the same world/ content and the same --now, the bytes are identical.
Default --now comes from the last commit that touched world/ (so a push that does not
change the world does not change the file — no churn), falling back to the wall clock.

The phantom check is the anti-simulation law made executable (CHARTER §3): every entity
the map can draw must have a source record under world/. Inject a fake avatar into the
compiled file and the check fails, by name.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import world  # noqa: E402  (canonical(), parse_ts())

CAPS = {"agents": 50, "channels": 50, "videos": 50, "threads": 100,
        "meetings": 10, "archive": 12, "history": 20}


def load_dir(root: Path, name: str, warnings: list) -> dict:
    """Every *.json under world/<name>/ — {id: record}. Broken files warn, never crash."""
    out, d = {}, root / "world" / name
    if not d.is_dir():
        warnings.append(f"world/{name}/ is missing — that section will be empty")
        return out
    for f in sorted(d.glob("*.json")):
        try:
            rec = json.loads(f.read_text(encoding="utf-8"))
        except Exception as e:
            warnings.append(f"world/{name}/{f.name} unreadable ({e}) — skipped")
            continue
        if not isinstance(rec, dict):
            warnings.append(f"world/{name}/{f.name} is not an object — skipped")
            continue
        if rec.get("schema") != 1:
            warnings.append(f"world/{name}/{f.name} schema={rec.get('schema')!r}, expected 1")
        key = rec.get("id") or rec.get("key") or f.stem
        rec["_source"] = f"world/{name}/{f.name}"   # the map shows where each record lives
        out[key] = rec
    return out


def world_head(root: Path):
    """(world_tree_id, iso-ts): the tree id of world/ at HEAD — content-derived.

    Content-derived on purpose (learned the honest way: the first scheme used
    `git log -1 -- world/`, which a depth-1 clone cannot resolve, so a stranger's
    clone disagreed with the committed file and the drift test caught it).
    The tree id changes exactly when world/ changes, is identical in shallow and
    full clones, and never depends on history. The timestamp (last commit that
    touched world/) is only used as the default --now."""
    try:
        t = subprocess.run(["git", "-C", str(root), "rev-parse", "--short=10", "HEAD:world"],
                           capture_output=True, text=True, timeout=20)
        ts = subprocess.run(["git", "-C", str(root), "log", "-1", "--format=%cI", "--", "world/"],
                            capture_output=True, text=True, timeout=20)
        if t.returncode == 0 and t.stdout.strip():
            tsx = ts.stdout.strip() if ts.returncode == 0 and ts.stdout.strip() else None
            return t.stdout.strip(), tsx
    except Exception:
        pass
    return None, None


def compile_world(root: Path, now_iso: str) -> dict:
    warnings: list = []
    agents = load_dir(root, "agents", warnings)
    channels = load_dir(root, "channels", warnings)
    videos = load_dir(root, "videos", warnings)
    threads = load_dir(root, "threads", warnings)
    meetings = load_dir(root, "meetings", warnings)
    archive = load_dir(root, "archive", warnings)

    lock = None
    lock_file = root / "world" / "lock.json"
    if lock_file.is_file():
        try:
            lock = json.loads(lock_file.read_text(encoding="utf-8"))
        except Exception as e:
            warnings.append(f"world/lock.json unreadable ({e})")
    else:
        warnings.append("world/lock.json is missing — the PC section will be empty")

    def strip(rec):
        return {k: v for k, v in rec.items() if k != "_source"}

    def tkey(rec, field="ts"):
        return rec.get(field) or ""

    agents_out = []
    for a in sorted(agents.values(), key=lambda r: r.get("id", "")):
        agents_out.append({**{k: a.get(k) for k in ("id", "role", "caps", "channel", "location",
                                                    "status", "last_seen_ts", "note")},
                           "_source": a["_source"]})

    channels_out = []
    for c in sorted(channels.values(), key=lambda r: r.get("key", "")):
        channels_out.append({**{k: c.get(k) for k in ("key", "title", "owner", "themes",
                                                      "status", "note")},
                             "_source": c["_source"]})

    videos_out = []
    for v in videos.values():
        hist = v.get("history") or []
        videos_out.append({"id": v.get("id"), "title": v.get("title"), "channel": v.get("channel"),
                           "owner": v.get("owner"), "stage": v.get("stage"),
                           "stage_name": v.get("stage_name"),
                           "updated_ts": (hist[-1]["ts"] if hist else None),
                           "_source": v["_source"]})
    videos_out.sort(key=lambda v: (v.get("stage") or 0, v.get("id") or ""))

    threads_out = []
    for t in threads.values():
        msgs = t.get("messages") or []
        threads_out.append({"id": t.get("id"), "subject": t.get("subject"), "type": t.get("type"),
                            "from": t.get("from"), "to": t.get("to"), "status": t.get("status"),
                            "msgs": len(msgs), "last_ts": (msgs[-1]["ts"] if msgs else None),
                            "_source": t["_source"]})
    threads_out.sort(key=lambda t: (t.get("last_ts") or "", t.get("id") or ""), reverse=True)

    meetings_out = sorted(({"id": m.get("id"), "subject": m.get("subject"),
                            "opened_by": m.get("opened_by"), "opened_ts": m.get("opened_ts"),
                            "outcome": m.get("outcome"), "_source": m["_source"]}
                           for m in meetings.values()),
                          key=lambda m: (m.get("opened_ts") or "", m.get("id") or ""), reverse=True)
    archive_out = sorted(({"id": a.get("id"), "title": a.get("title"), "owner": a.get("owner"),
                           "tags": a.get("tags"), "created_ts": a.get("created_ts"),
                           "deposited_to": a.get("deposited_to"), "_source": a["_source"]}
                          for a in archive.values()),
                         key=lambda a: (a.get("created_ts") or "", a.get("id") or ""), reverse=True)

    sha, ts = world_head(root)

    return {
        "schema": 1,
        "generated_ts": now_iso,
        "world_head": sha,
        "warnings": warnings,
        "counts": {"colonists": len(agents_out), "channels": len(channels_out),
                   "videos": len(videos_out), "threads_open": sum(1 for t in threads_out if t.get("status") == "open"),
                   "meetings": len(meetings), "archive": len(archive)},
        "lock": ({k: lock.get(k) for k in ("state", "holder", "purpose", "since_ts", "expires_ts",
                                           "generation", "renew_count", "max_hold_min")} |
                 {"queue": (lock.get("queue") or [])[:CAPS["agents"]],
                  "history": (lock.get("history") or [])[-CAPS["history"]:]}
                 if lock else None),
        "agents": agents_out[:CAPS["agents"]],
        "channels": channels_out[:CAPS["channels"]],
        "videos": videos_out[:CAPS["videos"]],
        "threads": threads_out[:CAPS["threads"]],
        "meetings": meetings_out[:CAPS["meetings"]],
        "archive": archive_out[:CAPS["archive"]],
    }


# ------------------------------------------------------------- the check ----
def check_phantoms(compiled: dict, root: Path) -> list:
    """Every entity the map can draw must have a source record under world/."""
    findings = []
    where = {"agents": ("agents", "id"), "channels": ("channels", "key"),
             "videos": ("videos", "id"), "threads": ("threads", "id"),
             "meetings": ("meetings", "id"), "archive": ("archive", "id")}
    for section, (sub, keyfield) in where.items():
        for item in compiled.get(section) or []:
            key = item.get(keyfield)
            if not key:
                findings.append(f"{section}: an entry has no {keyfield} — not drawable at all")
                continue
            if not (root / "world" / sub / f"{key}.json").is_file():
                findings.append(f"{section}: '{key}' has NO source record "
                                f"(world/{sub}/{key}.json missing) — PHANTOM")
            src = item.get("_source")
            if src and not (root / src).is_file():
                findings.append(f"{section}: '{key}' points at {src}, which does not exist")
    lock = compiled.get("lock")
    lf = root / "world" / "lock.json"
    if lock and lf.is_file():
        real = json.loads(lf.read_text(encoding="utf-8"))
        for f in ("state", "holder", "generation"):
            if lock.get(f) != real.get(f):
                findings.append(f"lock: compiled {f}={lock.get(f)!r} but world/lock.json says "
                                f"{real.get(f)!r} — the map would lie")
    if lock and lock.get("state") == "held" and not lock.get("holder"):
        findings.append("lock: state=held with no holder — impossible, refusing")
    return findings


# ------------------------------------------------------------------ main ----
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="compile world/ -> app/world.json (docs/WORLD.md §6)")
    ap.add_argument("--root", default=".")
    ap.add_argument("--out")
    ap.add_argument("--check", metavar="FILE", help="run the phantom check on a compiled file")
    ap.add_argument("--preview", metavar="FILE", help="write a self-contained map file")
    ap.add_argument("--now", default=None, help="pin generated_ts (ISO); default: world/ head")
    args = ap.parse_args(argv)

    root = Path(args.root).resolve()
    sha, ts = world_head(root)
    now_iso = args.now or ts or world.z()

    if args.check:
        compiled = json.loads(Path(args.check).read_text(encoding="utf-8"))
        findings = check_phantoms(compiled, root)
        for f in findings:
            print(f"PHANTOM: {f}")
        if findings:
            print(f"\n{len(findings)} phantom finding(s) — the map would show things that do not exist")
            return 1
        print("phantom check: clean — every drawn entity has its source record")
        return 0

    compiled = compile_world(root, now_iso)
    out = Path(args.out) if args.out else None
    if out:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(world.canonical(compiled))
        findings = check_phantoms(compiled, root)
        if findings:
            for f in findings:
                print(f"PHANTOM: {f}")
            return 1
        print(f"compiled {out} ({out.stat().st_size} bytes, world_head {sha}, "
              f"{len(compiled['warnings'])} warning(s))")

    if args.preview:
        html = build_preview(root, compiled)
        Path(args.preview).write_text(html, encoding="utf-8")
        print(f"preview {args.preview} ({len(html)} bytes, self-contained)")

    if not out and not args.preview:
        print(json.dumps(compiled, indent=2, ensure_ascii=False))
    return 0


def build_preview(root: Path, compiled: dict) -> str:
    """One file: styles and script inline, the world baked in (window.__WORLD__)."""
    app = root / "app"
    index = (app / "index.html").read_text(encoding="utf-8")
    css = (app / "app.css").read_text(encoding="utf-8")
    js = (app / "app.js").read_text(encoding="utf-8")
    data = json.dumps(compiled, ensure_ascii=False)
    index = index.replace('<link rel="stylesheet" href="./app.css">', f"<style>\n{css}\n</style>")
    index = index.replace('<script src="./app.js"></script>',
                          f"<script>window.__WORLD__ = {data};</script>\n<script>\n{js}\n</script>")
    return index


if __name__ == "__main__":
    sys.exit(main())

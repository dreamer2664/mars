#!/usr/bin/env python3
"""Check every `uiautomation` name this repo reaches for against the library's real source.

Why this exists: `winuia.py` was written against memory of the API and the Director's PC found the first
three invented names in a single run (`IsEnabled()` called, `CaptureToRect`, `WaitUntilReady`). Then the
shape-checker I wrote to catch that class of bug invented three names of its own (`Control.Close`,
`Control.GetInvokePattern`, `Control.GetValuePattern` — those live on the `IUIAutomation` COM interface and
on `WindowPatternControl`, not on `Control`), so the *guard* was the thing failing. Guessing, twice.

This stops guessing. It reads the shipped `uiautomation.py` — from an installed package or a downloaded
`.whl` — and answers one question: does every name `tools/winuia.py` (and this repo's shape lists) touch
actually exist on the object it is touched on, with the kind (property vs method) that our code assumes?
Run it on any OS, no Windows required:

    python3 tools/api_check.py                      # auto-locates uiautomation or a wheel in /tmp
    python3 tools/api_check.py --wheel path/to/uiautomation-2.0.29-py3-none-any.whl
    python3 tools/api_check.py --json               # for CI / pasting into a report

Exit 0 = every name verified. Exit 1 = at least one mismatch, printed as `file:line NAME kind != expected`.
It cannot prove the behaviour is *right*, only that the call is possible — which is exactly the half I kept
getting wrong, and the half that costs a human a round trip.
"""
from __future__ import annotations

import argparse
import ast
import io
import json
import os
import re
import sys
import zipfile

PKG = "uiautomation"
MOD_IN_WHEEL = "uiautomation/uiautomation.py"
#: the object each attribute is read off, in our code
CONTROL_ATTRS = re.compile(r"\b(?:c|control|ctl|target|elem|el)\.([A-Z][A-Za-z0-9_]*)")
MODULE_ATTRS = re.compile(r"\b(?:auto|mod)\.([A-Z][A-Za-z0-9_]*)")
#: names our own lists claim the library must expose
LIST_NAMES = re.compile(r"REQUIRED[A-Z_]*\s*=\s*\((.*?)\)", re.S)


class Fail(Exception):
    pass


def find_source(wheel: str | None) -> tuple[str, str]:
    """Return (source_text, where_from). Prefers an explicit wheel, then an installed package, then any
    wheel lying around — so this runs on Linux, where the package cannot even be imported."""
    if wheel:
        if not os.path.exists(wheel):
            raise Fail(f"--wheel {wheel} does not exist")
        z = zipfile.ZipFile(wheel)
        if MOD_IN_WHEEL not in z.namelist():
            raise Fail(f"{wheel} has no {MOD_IN_WHEEL} (members: {z.namelist()[:6]})")
        return z.read(MOD_IN_WHEEL).decode("utf-8", "replace"), os.path.basename(wheel)
    for root in sys.path:
        cand = os.path.join(root, PKG, "uiautomation.py")
        if os.path.exists(cand):
            with io.open(cand, encoding="utf-8", errors="replace") as f:
                return f.read(), cand
    hits = [os.path.join(d, f) for d in ("/tmp", "/tmp/uia_dl", os.getcwd())
            for f in sorted(os.listdir(d) if os.path.isdir(d) else [])
            if f.startswith(PKG) and f.endswith(".whl")]
    if hits:
        return find_source(hits[0])
    raise Fail("no uiautomation source found: pass --wheel <file.whl> or `pip download uiautomation`")


def index(src: str) -> dict:
    """Parse the module and return {module_funcs, module_classes, class_members:{cls:{name:'property'|'method'}}}."""
    tree = ast.parse(src)
    mod_callables, mod_values, classes = set(), set(), {}

    def member_kind(node):
        for d in getattr(node, "decorator_list", []):
            dn = getattr(d, "id", None) or getattr(d, "attr", None)
            if dn == "property":
                return "property"
            if dn == "cached_property":
                return "property"
        return "method"

    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            mod_callables.add(node.name)
        elif isinstance(node, ast.ClassDef):
            members = {}
            for sub in node.body:
                if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    members[sub.name] = member_kind(sub)
                elif isinstance(sub, ast.Assign):
                    for t in sub.targets:
                        if isinstance(t, ast.Name):
                            members.setdefault(t.id, "value")
                elif isinstance(sub, ast.AnnAssign) and isinstance(sub.target, ast.Name):
                    members.setdefault(sub.target.id, "value")
            classes[node.name] = members
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    mod_values.add(t.id)
        elif isinstance(node, ast.ImportFrom) and node.module == "ctypes":
            pass
    # module-level names produced by other idioms (e.g. `Release = Close`, ctypes typedefs): scan textually
    for m in re.finditer(r"^([A-Za-z_][A-Za-z0-9_]*)\s*=", src, re.M):
        mod_values.add(m.group(1))
    for m in re.finditer(r"^def ([A-Za-z_]\w*)", src, re.M):
        mod_callables.add(m.group(1))
    for m in re.finditer(r"^class ([A-Za-z_]\w*)", src, re.M):
        classes.setdefault(m.group(1), {})
    return {"callables": mod_callables, "values": mod_values, "classes": classes}


def guarded_context(lines: list[str], lineno: int, span: int = 6) -> list[str]:
    """Source lines just above a call, where a `hasattr` guard would sit."""
    lo = max(0, lineno - 1 - span)
    return lines[lo:lineno]


def usages(path: str) -> list[dict]:
    """Every name this file reaches for, with line, host object and how we use it (call vs read)."""
    src = open(path, encoding="utf-8").read()
    lines = src.splitlines()
    out = []
    for i, line in enumerate(lines, 1):
        code = line.split("#", 1)[0]
        if not code.strip() or code.strip().startswith(('"', "'", "def class")):
            continue
        for host, rx in (("module", MODULE_ATTRS), ("Control", CONTROL_ATTRS)):
            for m in rx.finditer(code):
                name = m.group(1)
                tail = code[m.end():]
                called = tail.lstrip().startswith("(")
                # a trailing `(` several tokens away (e.g. `c.Name == x`) is a comparison, not a call
                if not called and re.match(r"^\([^)]*\)\s*[=<>!]", tail.lstrip()):
                    called = True
                out.append({"line": i, "host": host, "name": name, "called": called,
                            "text": code.strip()[:110]})
    return out


def declared_lists(path: str) -> dict[str, list[str]]:
    src = open(path, encoding="utf-8").read()
    out = {}
    for m in LIST_NAMES.finditer(src):
        names = re.findall(r'"([A-Za-z_][A-Za-z0-9_]*)"', m.group(1))
        if names:
            out[m.group(0).split("=")[0].strip()] = names
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--wheel", default=None, help="path to a uiautomation .whl (no install, no Windows)")
    ap.add_argument("--target", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "winuia.py"))
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)

    try:
        src, where = find_source(a.wheel)
        idx = index(src)
    except (Fail, OSError) as e:
        print(json.dumps({"ok": False, "error": str(e)}) if a.json else f"api_check: {e}")
        return 2

    problems, checked = [], 0
    optional: list[dict] = []
    guard_lines = open(a.target, encoding="utf-8").read().splitlines()
    for u in usages(a.target):
        checked += 1
        if u["host"] == "module":
            exists = u["name"] in idx["callables"] or u["name"] in idx["values"]
            kind = "callable" if u["name"] in idx["callables"] else ("value" if exists else "MISSING")
            if not exists:
                problems.append({**u, "found": kind, "why": "no such module-level name in uiautomation"})
            elif u["called"] and kind != "callable":
                problems.append({**u, "found": kind, "why": "read as a call but it is a plain value"})
            elif not u["called"] and kind == "callable" and re.search(r"\bis\s|\s==\s", u["text"]):
                pass                     # comparing a function object is odd but not our bug to name
        else:
            members = idx["classes"].get("Control", {})
            kind = members.get(u["name"])
            if kind is None:
                # Not on the base class. That is FATAL unless the call site asked the concrete object first
                # (`hasattr(c, "X")` on this or a nearby line), which is the correct idiom for a capability
                # only some control types have — GetInvokePattern on ButtonControl, Close on
                # WindowPatternControl. Guarded => fine and reported as such; unguarded => a crash on a
                # control the tree typed otherwise, which is the common case, not the exception.
                guarded = any(f'hasattr({tok}, "{u["name"]}")' in t for tok in ("c", "control", "ctl", "target")
                              for t in guarded_context(guard_lines, u["line"]))
                owners = sorted(c for c, ms in idx["classes"].items() if u["name"] in ms)[:3]
                if guarded:
                    optional.append({**u, "found": "on " + (", ".join(owners) or "?"),
                                     "why": "not on Control, but the call site asks the object with "
                                            "hasattr() — correct for a per-control capability"})
                    continue
                problems.append({**u, "found": "MISSING", "why": "not on Control, and the call site does "
                                 f"not guard for it" + (f" (it is on {', '.join(owners)})" if owners else "")})
            elif u["called"] and kind == "property":
                problems.append({**u, "found": "property",
                                 "why": "called with () but it is a PROPERTY — calling it is the exact bug "
                                        "the 19:06Z run hit (`TypeError: 'bool' object is not callable`)"})
            elif not u["called"] and kind == "method":
                problems.append({**u, "found": "method",
                                 "why": "read as a value but it is a method — a bound method is truthy, "
                                        "so `if c.Exists:` passes even when the control vanished"})

    for list_name, names in declared_lists(a.target).items():
        for n in names:
            checked += 1
            if list_name.upper().startswith("REQUIRED_CONTROL"):
                if n not in idx["classes"].get("Control", {}):
                    owners = sorted(c for c, ms in idx["classes"].items() if n in ms)[:3]
                    problems.append({"line": 0, "host": "Control", "name": n, "called": None,
                                     "found": "MISSING", "why": f"{list_name} demands Control.{n}, which "
                                     f"does not exist" + (f" (it is on {', '.join(owners)})" if owners else ""),
                                     "text": f"[{list_name}]"})
            elif list_name.upper() == "REQUIRED":
                if n not in idx["callables"] and n not in idx["values"]:
                    problems.append({"line": 0, "host": "module", "name": n, "called": None,
                                     "found": "MISSING", "why": f"{list_name} demands auto.{n}",
                                     "text": f"[{list_name}]"})
    # our *negative* claim matters too: verify_shapes() rejects Control members that are methods.
    for n in ("Name", "IsEnabled", "IsOffscreen", "BoundingRectangle", "ProcessId", "NativeWindowHandle"):
        k = idx["classes"].get("Control", {}).get(n)
        if k == "method":
            problems.append({"line": 0, "host": "Control", "name": n, "called": False, "found": "method",
                             "why": "verify_shapes() asserts this is a property; this library says method",
                             "text": "[verify_shapes]"})
        checked += 1

    res = {"ok": not problems, "source": where, "target": os.path.basename(a.target),
           "guarded_optional": len(optional),
           "checked": checked, "control_members": len(idx["classes"].get("Control", {})),
           "module_callables": len(idx["callables"]),
           "problems": problems}
    if a.json:
        print(json.dumps(res, indent=1))
    else:
        print(f"api_check: {where} — Control has {res['control_members']} members, module "
              f"{res['module_callables']} callables; {checked} names used by {res['target']} verified")
        for o in optional:
            print(f"  ok {o['line']}: {o['host']}.{o['name']} — {o['why']} ({o['found']})")
        for p in problems:
            loc = f"{p['line']}:" if p["line"] else ""
            print(f"  {loc} {p['host']}.{p['name']}  ->  {p['why']}")
            if p.get("text") and not p["text"].startswith("["):
                print(f"        {p['text']}")
        print("RESULT:", "clean" if not problems else f"{len(problems)} MISMATCH(ES) — do not ship")
    return 0 if not problems else 1


if __name__ == "__main__":
    sys.exit(main())

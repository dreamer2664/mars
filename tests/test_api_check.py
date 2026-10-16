"""Tests for `tools/api_check.py` — the tool that checks the Windows backend's assumptions about the
`uiautomation` library without needing Windows.

These tests are unusual in that their subject is a *checker*. Two things must hold for it to be worth
running before handing an instruction to a human: it must find a correctly installed library in every layout
(it reported "cannot find" on a flat-module install for exactly one missing comma, and the earlier version
found nothing at all for a normal `site-packages` install because it looked one directory level wrong);
and it must FAIL when the code is wrong, or it is theatre. So both directions are asserted here, on fixtures
shaped like real installs, plus one negative control that un-guards a call and demands a red result.

The fixtures are copies of a downloaded wheel's members, not the wheel: they must be built from whatever
source is at hand, and the file's skip reason says so out loud rather than pretending the check ran.
"""
from __future__ import annotations

import io
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
TOOLS = os.path.join(HERE, os.pardir, "tools")
CHECKER = os.path.abspath(os.path.join(TOOLS, "api_check.py"))
WINUIA = os.path.abspath(os.path.join(TOOLS, "winuia.py"))

WHEEL_GUESSES = ["/tmp/uia_dl/uiautomation-2.0.29-py3-none-any.whl"]


def _members() -> dict[str, str] | None:
    """Read the three files a real `uiautomation` install contains, from a wheel on disk or an installed
    package. None means "no library source here" — the tests skip loudly rather than silently passing."""
    for w in WHEEL_GUESSES:
        if os.path.exists(w):
            z = zipfile.ZipFile(w)
            return {os.path.basename(n): z.read(n).decode("utf-8", "replace")
                    for n in z.namelist() if n.endswith("uiautomation.py") or n.endswith("version.py")
                    or n.endswith("__init__.py")}
    for root in sys.path:
        d = os.path.join(root, "uiautomation")
        if os.path.isdir(d):
            out = {}
            for f in ("uiautomation.py", "version.py", "__init__.py"):
                if os.path.exists(os.path.join(d, f)):
                    out[f] = open(os.path.join(d, f), encoding="utf-8", errors="replace").read()
            if "uiautomation.py" in out:
                return out
    return None


MEMBERS = _members()


def _build_site(members: dict[str, str], parent: str, kind: str = "package") -> str:
    """Write the library out the way pip installs it, so a subprocess with PYTHONPATH=<this> sees a real
    install. No fake: the bytes come from the wheel/package itself (see _members)."""
    site = os.path.join(parent, kind)
    os.makedirs(site, exist_ok=True)
    if kind == "package":
        d = os.path.join(site, "uiautomation")
        os.makedirs(d, exist_ok=True)
        for f, txt in members.items():
            with io.open(os.path.join(d, f), "w", encoding="utf-8") as fh:
                fh.write(txt)
    else:
        with io.open(os.path.join(site, "uiautomation.py"), "w", encoding="utf-8") as fh:
            fh.write(members["uiautomation.py"])
    return site


def _run(args: list[str], cwd: str, pythonpath: str = "", isolated: bool = False) -> tuple[int, str]:
    """Run the checker in a subprocess. `isolated=True` adds -S (do not process site-packages) so the run
    sees ONLY the given PYTHONPATH — needed by the "no library installed" test, which otherwise passes for
    the wrong reason on a machine that has the library, and FAILS for the wrong reason on one that does.
    That is not hypothetical: this test suite is run both on CI (no library) and on the PC (library
    installed), and only one of those was holding when it was written."""
    env = dict(os.environ, PYTHONPATH=pythonpath or "")
    py = [sys.executable, "-S"] if isolated else [sys.executable]
    p = subprocess.run([*py, CHECKER, *args], cwd=cwd, env=env,
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    return p.returncode, p.stdout


class Layouts(unittest.TestCase):
    """`find_source` must locate the library in the shapes pip actually produces."""

    @classmethod
    def setUpClass(cls):
        if not MEMBERS:
            raise unittest.SkipTest("no uiautomation source available here: no wheel in "
                                    + ", ".join(WHEEL_GUESSES) + " and no installed package. This suite is "
                                    "the gate for the Windows backend, so do NOT read a skip as a pass.")
        cls.tmp = tempfile.mkdtemp(prefix="api-check-")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(getattr(cls, "tmp", None), ignore_errors=True)

    def _site(self, kind: str) -> str:
        return _build_site(MEMBERS, self.tmp, kind)

    def test_package_layout_found_without_flags(self):
        rc, out = _run(["--target", WINUIA], self.tmp, self._site("package"))
        self.assertEqual(rc, 0, out)
        self.assertIn("RESULT: clean", out)
        self.assertIn("uiautomation/uiautomation.py", out)   # it names WHERE it looked

    def test_flat_module_layout_found_without_flags(self):
        """The bug this test exists for: a candidate built as `os.path.join(root, PKG + ".py")` iterated the
        string into `root/u/i/a/u/t/o/m/a/t/i/o/n/./p/y`, so a flat install looked like no install."""
        rc, out = _run(["--target", WINUIA], self.tmp, self._site("flat"))
        self.assertEqual(rc, 0, out)
        self.assertIn("RESULT: clean", out)
        self.assertNotIn("/u/i/a/", out, "path components must be tuples, not bare strings")

    def test_missing_library_refuses_with_actionable_words(self):
        rc, out = _run(["--target", WINUIA], self.tmp, os.path.join(self.tmp, "nothing-here"),
                       isolated=True)
        self.assertEqual(rc, 2, out)
        for want in ("cannot find the uiautomation source", "import uiautomation", "--wheel"):
            self.assertIn(want, out, out)

    def test_version_is_named_not_assumed(self):
        """`uiautomation` has no `__version__`; it keeps VERSION in version.py. A checker that prints
        'unknown' for a good install teaches people to distrust the field that would catch a bad one."""
        site = self._site("package")
        if "version.py" not in MEMBERS:
            self.skipTest("the source at hand has no version.py to copy")
        rc, out = _run(["--target", WINUIA], self.tmp, site)
        self.assertEqual(rc, 0, out)
        self.assertRegex(out, r"uiautomation \d+\.\d+", out.splitlines()[0])

    def test_negative_control_an_unguarded_call_is_red(self):
        """A checker that cannot fail is not a gate. Copy winuia.py with one `hasattr` guard removed, point
        the checker at the copy, and demand a mismatch naming that line."""
        with io.open(WINUIA, encoding="utf-8") as fh:
            src = fh.read()
        needle = 'if button == "left" and hasattr(c, "GetInvokePattern"):'
        self.assertIn(needle, src, "winuia.py no longer has the guard this control relies on — update the test")
        bad = src.replace(needle, 'if button == "left":')
        path = os.path.join(self.tmp, "winuia_broken.py")
        with io.open(path, "w", encoding="utf-8") as fh:
            fh.write(bad)
        rc, out = _run(["--target", path], self.tmp, self._site("package"))
        self.assertEqual(rc, 1, out)
        self.assertIn("Control.GetInvokePattern", out)
        self.assertIn("does not guard", out)

    def test_a_property_called_like_a_method_is_red(self):
        """The 19:06Z field failure, made permanent: `if c.IsEnabled():` is a TypeError on a real desktop
        and invisible to any Linux unit test, so the static checker must own it."""
        with io.open(WINUIA, encoding="utf-8") as fh:
            src = fh.read()
        bad = src.replace("if c.IsOffscreen and not c.IsEnabled:", "if c.IsOffscreen() and not c.IsEnabled():")
        self.assertNotEqual(bad, src, "the line the control targets has moved; update this test")
        path = os.path.join(self.tmp, "winuia_propbug.py")
        with io.open(path, "w", encoding="utf-8") as fh:
            fh.write(bad)
        rc, out = _run(["--target", path], self.tmp, self._site("package"))
        self.assertEqual(rc, 1, out)
        self.assertIn("IsOffscreen", out)


class TheRealFileIsClean(unittest.TestCase):
    """The claim the whole exercise rests on, executed rather than asserted in prose: the winuia.py that
    ships has no name in it that the library cannot serve."""

    def test_winuia_passes_its_own_checker(self):
        if not MEMBERS:
            self.skipTest("no uiautomation source at hand (see Layouts.setUpClass) — this is NOT a pass")
        tmp = tempfile.mkdtemp(prefix="api-clean-")
        self.addCleanup(shutil.rmtree, tmp, True)
        # an empty environment would fail to find the library and look like a problem with winuia.py,
        # which is a false alarm of exactly the kind this suite exists to prevent
        rc, out = _run(["--target", WINUIA, "--json"], tmp, _build_site(MEMBERS, tmp))
        self.assertEqual(rc, 0, out)
        import json
        d = json.loads(out)
        self.assertTrue(d["ok"], out)
        self.assertGreater(d["checked"], 30, f"the checker looked at only {d['checked']} names")
        self.assertEqual(d["problems"], [])


if __name__ == "__main__":
    unittest.main(verbosity=2)

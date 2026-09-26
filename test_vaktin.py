"""Vaktin's own tests: stdlib unittest, no watched repo, no network.

    python3 -m unittest test_vaktin -v
"""
import importlib.util
import json
import os
import tempfile
import time
import unittest

spec = importlib.util.spec_from_file_location("vaktin", os.path.join(os.path.dirname(__file__), "vaktin.py"))
v = importlib.util.module_from_spec(spec)
spec.loader.exec_module(v)

PLAYWRIGHT = """
2026-01-01T00:00:01.000Z   ✘  36 [desktop-light] › widgets.spec.mjs:188:3 › Widgets › the card names the missing half (6.1s)
2026-01-01T00:00:02.000Z   ✘  41 [desktop-light] › widgets.spec.mjs:188:3 › Widgets › the card names the missing half (retry #1) (6.2s)
2026-01-01T00:00:03.000Z   ✘  62 [mobile-dark] › widgets.spec.mjs:188:3 › Widgets › the card names the missing half (7.0s)
2026-01-01T00:00:04.000Z   ✘  70 [mobile-dark] › player.spec.mjs:1229:3 › Player › the escape hatch stays reachable (3.1s)
2026-01-01T00:00:05.000Z     Error: expect(received).toContain(expected) // indexOf
2026-01-01T00:00:06.000Z     Error: expect(received).toContain(expected) // indexOf
2026-01-01T00:00:07.000Z     Error: Open in list: not present/visible
2026-01-01T00:00:08.000Z   \x1b[31m  ✘  71 [mobile-dark] › player.spec.mjs:1229:3 › Player › the escape hatch stays reachable (retry #1) (3.0s)\x1b[0m
"""
PYTEST = """
2026-01-01T00:00:01.000Z FAILED tests/test_widgets.py::TestCard::test_names_the_half - AssertionError: assert 'x' in 'y'
2026-01-01T00:00:02.000Z ERROR tests/test_api.py::TestList::test_lists
2026-01-01T00:00:03.000Z FAILED tests/test_widgets.py::TestCard::test_names_the_half - AssertionError
2026-01-01T00:00:04.000Z E       AssertionError: assert 3 == 4
"""


class TestParse(unittest.TestCase):
    def test_playwright_lines_collapse_projects_and_retries(self):
        r = v.parse_failure_log(PLAYWRIGHT)
        ids = [t["id"] for t in r["tests"]]
        self.assertEqual(ids, ["widgets.spec.mjs:188:3 › Widgets › the card names the missing half",
                               "player.spec.mjs:1229:3 › Player › the escape hatch stays reachable"])
        self.assertEqual(r["tests"][0]["times"], 3)
        self.assertEqual(r["tests"][0]["projects"], ["desktop-light", "mobile-dark"])
        self.assertEqual(r["tests"][1]["times"], 2)          # the colour-coded retry line counts too
        self.assertEqual(r["errors"], ["expect(received).toContain(expected) // indexOf",
                                       "Open in list: not present/visible"])

    def test_pytest_lines(self):
        r = v.parse_failure_log(PYTEST)
        self.assertEqual([t["id"] for t in r["tests"]],
                         ["tests/test_widgets.py::TestCard::test_names_the_half", "tests/test_api.py::TestList::test_lists"])
        self.assertEqual(r["tests"][0]["times"], 2)
        self.assertEqual(r["errors"], ["assert 3 == 4"])

    def test_empty_and_garbage(self):
        self.assertEqual(v.parse_failure_log(""), {"tests": [], "errors": []})
        self.assertEqual(v.parse_failure_log(None), {"tests": [], "errors": []})
        self.assertEqual(v.parse_failure_log("just a line\nanother")["tests"], [])


class TestView(unittest.TestCase):
    def test_repeats_and_order(self):
        now = time.time()
        store = {"runs": {
            "1": {"id": "1", "created": now - 3 * 86400, "tests": [{"id": "a", "projects": [], "times": 1}], "fixed": {"sha": "abc1234"}},
            "2": {"id": "2", "created": now - 1 * 86400, "tests": [{"id": "a", "projects": [], "times": 1}, {"id": "b", "projects": [], "times": 1}], "fixed": None},
            "3": {"id": "3", "created": now - 200 * 86400, "tests": [{"id": "a", "projects": [], "times": 1}], "fixed": None},
        }}
        recs, reps = v.failures_view(store)
        self.assertEqual([r["id"] for r in recs], ["2", "1", "3"])
        self.assertEqual(len(reps), 1)
        self.assertEqual((reps[0]["id"], reps[0]["runs"], reps[0]["fixed"]), ("a", 2, False))   # the 200-day-old one is outside the window

    def test_store_roundtrip(self):
        with tempfile.TemporaryDirectory() as d:
            v.FAIL_DIR = d
            cfg = {"name": "my-service"}
            self.assertEqual(v.load_failures(cfg), {"runs": {}})
            v._save_failures(cfg, {"runs": {"9": {"id": "9"}}})
            self.assertEqual(v.load_failures(cfg)["runs"]["9"]["id"], "9")

    def test_section_renders_with_placeholder_data(self):
        p = {"failures": [{"id": "5", "workflow": "Gate", "conclusion": "failure", "branch": "main", "sha": "abc1234",
                           "title": "fix(widget): a subject", "created": time.time() - 3600, "mins": 31, "jobs": [],
                           "blocked": 0, "tests": [{"id": "w.spec.mjs:1:1 › W › t", "projects": ["desktop-light"], "times": 2}],
                           "errors": ["expect(x).toBe(y)"], "fixed": None},
                          {"id": "6", "workflow": "Gate", "conclusion": "failure", "branch": "v1.2.3", "sha": "def5678",
                           "title": "release: bump", "created": time.time() - 7200, "mins": 1, "jobs": [],
                           "blocked": 5, "tests": [], "errors": [], "fixed": {"sha": "0123abc", "title": "ci: retry", "created": 0, "id": "7"}}],
             "repeats": [{"id": "w.spec.mjs:1:1 › W › t", "runs": 3, "last": time.time(), "fixed": False}]}
        h = v.failures_section(p)
        for needle in ("1 próf", "óleyst", "GitHub ræsti aldrei 5 verk", "0123abc", "3×", "enn opið", "c-ftests"):
            self.assertIn(needle, h)
        self.assertIn("Engin skráð föll", v.failures_section({"failures": []}))


class TestOffice(unittest.TestCase):
    def test_reads_reports_and_the_week_ledger(self):
        with tempfile.TemporaryDirectory() as d:
            v.OFFICE_DIR = d
            os.makedirs(os.path.join(d, "reports", "gardener")); os.makedirs(os.path.join(d, "ledger")); os.makedirs(os.path.join(d, "handoffs"))
            open(os.path.join(d, "reports", "gardener", "2026-01-05_0300.md"), "w").write(
                "DONE\n# gardener\n## What I did\nfixed a test\n## Needs you\nmerge PR 12\n")
            open(os.path.join(d, "reports", "gardener", "2026-01-06_0300.md"), "w").write(
                "BLOCKED\n# gardener\n## Hvað ég gerði\nread only\n## Þarf Snorra\nallow bash\nand more\n")
            open(os.path.join(d, "handoffs", "gardener.md"), "w").write("go")
            open(os.path.join(d, "ledger", f"{v._office_week_key()}.json"), "w").write(json.dumps(
                {"runs": [{"role": "gardener", "cost": 1.5}, {"role": "gardener", "cost": 0.5}, {"role": "lead", "cost": 0.25}],
                 "usage": {"all": 37, "fable": 61, "ts": 0}}))
            o = v.office_data()
            g = [r for r in o["roles"] if r["role"] == "gardener"][0]
            self.assertEqual((g["status"], g["needs"], g["runs"], g["cost"], g["handoff"], g["reports"]),
                             ("BLOCKED", "allow bash and more", 2, 2.0, True, 2))
            lead = [r for r in o["roles"] if r["role"] == "lead"][0]        # in the ledger, no report yet
            self.assertEqual((lead["status"], lead["runs"]), ("—", 1))
            self.assertEqual((o["cost"], o["runs"], o["usage"]["all"]), (2.25, 3, 37))
            h = v.office_section(o)
            for needle in ("Skrifstofan", "fast", "allow bash", "2× · 2.00 USD", "/usage 37%", "ekkert enn"):
                self.assertIn(needle, h)
        v.OFFICE_DIR = "/nonexistent-office"
        self.assertIsNone(v.office_data())
        self.assertEqual(v.office_section(None), "")


class TestRunnerAlerts(unittest.TestCase):
    def test_alert_after_sustained_offline_and_on_recovery(self):
        sent = []
        v._runner_seen.clear()
        real = v.send_alert
        self.addCleanup(setattr, v, "send_alert", real)
        v.send_alert = lambda m: sent.append(m) or True
        v.watch_github_runners([{"name": "box", "online": False, "busy": False}])
        self.assertEqual(sent, [])                                    # one tick is a restart, not an outage
        v.watch_github_runners([{"name": "box", "online": False, "busy": False}])
        self.assertEqual(len(sent), 1); self.assertIn("AFTENGDUR", sent[0])
        v.watch_github_runners([{"name": "box", "online": False, "busy": False}])
        self.assertEqual(len(sent), 1)                                # once, not every tick
        v.watch_github_runners([{"name": "box", "online": True, "busy": False}])
        self.assertEqual(len(sent), 2); self.assertIn("aftur", sent[1])

    def test_no_command_means_no_alert(self):
        v.ALERT_CMD_FILE = "/nonexistent/alert-cmd"
        os.environ.pop("VAKTIN_ALERT_CMD", None)
        self.assertFalse(v.send_alert("x"))


if __name__ == "__main__":
    unittest.main()

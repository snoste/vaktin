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
            for needle in ("Skrifstofan", "fast", "allow bash", "2× · 2.00 USD", "/usage 37%", "ekkert enn", "skrifstofuborðið"):
                self.assertIn(needle, h)
        v.OFFICE_DIR = "/nonexistent-office"
        self.assertIsNone(v.office_data())
        self.assertEqual(v.office_section(None), "")


class TestTools(unittest.TestCase):
    def test_tools_from_env_and_local_mark(self):
        os.environ["VAKTIN_TOOLS"] = "Desk=https://desk.example:8788/;Colony=http://127.0.0.1:5274/"
        try:
            tl = v.tools()
        finally:
            os.environ.pop("VAKTIN_TOOLS", None)
        self.assertEqual([(t["name"], t["local"]) for t in tl], [("Desk", False), ("Colony", True)])
        h = v.page({"projects": [], "configured": False, "at": "1", "sessions": [], "runners": {"list": [], "events": []}})
        self.assertIn("Vaktin", h)


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


class TestRunnerPower(unittest.TestCase):
    ROWS = [{"name": "big", "online": True, "busy": False}, {"name": "big-2", "online": True, "busy": True},
            {"name": "small", "online": False, "busy": False}, {"name": "unknown", "online": True, "busy": False}]
    HOSTS = {"big": "u@a", "big-2": "u@a", "small": "u@b"}
    STORE = {"machines": {"u@a": {"cores": 8, "ram_gb": 31.2, "cpu": "X", "single": 60.0, "multi": 300.0, "at": 1},
                          "u@b": {"cores": 2, "ram_gb": 4, "cpu": "Y", "single": 20.0, "multi": 40.0, "at": 1}},
             "hosted": {"cores": 4, "ram_gb": 15.6, "cpu": "H", "single": 40.0, "multi": 120.0, "at": 1}}

    def test_a_bench_line_is_read_out_of_a_log(self):
        line = '2026-01-01T00:00:00Z VAKTIN_BENCH {"cores": 4, "ram_gb": 15.6, "cpu": "c", "os": "Linux", "single": 41.5, "multi": 120.2}'
        self.assertEqual(v.parse_bench("noise\n" + line + "\nmore")["multi"], 120.2)
        self.assertIsNone(v.parse_bench("nothing here"))
        self.assertIsNone(v.parse_bench("VAKTIN_BENCH {broken"))

    def test_runners_on_one_machine_share_a_row_and_a_ratio(self):
        pw = v.power_view(self.ROWS, self.STORE, self.HOSTS)
        first, second = pw["machines"]
        self.assertEqual(first["runners"], ["big", "big-2"])
        self.assertTrue(first["busy"] and first["online"])
        self.assertEqual((first["x"], first["x1"], first["pct"]), (2.5, 1.5, 100.0))
        self.assertFalse(second["online"])
        self.assertEqual(pw["hosted"]["pct"], 40.0)
        self.assertEqual(pw["loose"], ["unknown"])
        page = v.power_html(pw)
        self.assertIn("2,5×", page)
        self.assertIn("unknown", page)

    def test_a_busy_or_fresh_machine_is_left_alone(self):
        asked = []
        old = (v.load_power, v.save_power, v.runner_hosts)
        store = {"machines": {"u@b": {"multi": 40.0, "cores": 2, "at": 10 ** 9}}, "hosted": {"multi": 1.0, "at": 10 ** 9}}
        v.load_power, v.save_power, v.runner_hosts = (lambda: store), (lambda s: None), (lambda: dict(self.HOSTS, unknown="u@c"))
        try:
            rows = [dict(r, online=True) for r in self.ROWS]
            out = v.refresh_power(rows, now=10 ** 9 + 60, measure_fn=lambda t: asked.append(t) or {"multi": 9.0, "cores": 1},
                                  hosted_fn=lambda: self.fail("hosted is fresh"))
        finally:
            v.load_power, v.save_power, v.runner_hosts = old
        self.assertEqual(asked, ["u@c"])          # u@a is busy, u@b was measured a moment ago
        self.assertEqual(out["machines"]["u@c"]["multi"], 9.0)

    def test_a_host_line_cannot_smuggle_an_ssh_option(self):
        old = os.environ.get("VAKTIN_RUNNER_HOSTS")
        os.environ["VAKTIN_RUNNER_HOSTS"] = "a = u@h; b = -oProxyCommand=x; c = u@h extra; # d = u@h"
        try:
            hosts = v.runner_hosts()
        finally:
            if old is None:
                del os.environ["VAKTIN_RUNNER_HOSTS"]
            else:
                os.environ["VAKTIN_RUNNER_HOSTS"] = old
        self.assertEqual(hosts.get("a"), "u@h")
        self.assertNotIn("b", hosts)
        self.assertNotIn("c", hosts)


if __name__ == "__main__":
    unittest.main()


class TestRunTiming(unittest.TestCase):
    def test_epoch_takes_balena_millis(self):
        self.assertEqual(v._epoch("2026-09-28T15:20:34.871Z"), v._epoch("2026-09-28T15:20:34Z"))
        self.assertGreater(v._epoch("2026-09-28T15:20:34Z"), 0)
        self.assertEqual(v._epoch("garbage"), 0)

    def test_timing_cell_shows_only_what_is_known(self):
        cell = v.timing_cell({"gate": {"mins": 22, "conclusion": "success"}, "build_mins": 4, "total_mins": 31})
        self.assertIn("hlið 22 mín", cell)
        self.assertIn("bygging 4 mín", cell)
        self.assertIn("alls 31 mín", cell)
        self.assertEqual(v.timing_cell({}), "")
        self.assertIn("(failure)", v.timing_cell({"gate": {"mins": 5, "conclusion": "failure"}}))
        self.assertIn("1 klst 5 mín", v.timing_cell({"build_mins": 65}))

    def test_history_section_renders_runs(self):
        p = {"history": [
            {"name": "E2E control gate", "title": "feat: x", "ref": "main", "status": "completed",
             "conclusion": "success", "start": 1790000000, "mins": 22, "done": True, "url": "https://example.test/1"},
            {"name": "Tests", "title": "feat: y", "ref": "main", "status": "in_progress",
             "conclusion": "", "start": 1790000000, "mins": 3, "done": False, "url": ""}]}
        out = v.history_section(p)
        self.assertIn("Keyrslusaga", out)
        self.assertIn("tókst", out)
        self.assertIn("í gangi", out)
        self.assertIn("22 mín", out)
        self.assertIn("3 mín hingað til", out)
        self.assertEqual(v.history_section({"history": []}), "")

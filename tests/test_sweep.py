"""tools/sweep.py: parsing, verdicts, the recommendation, and that the TV's theme always comes back."""
import json
import tempfile
import unittest
from unittest import mock

from tools import sweep

GFX = """Stats since: 5103227470ns
Total frames rendered: 120
Janky frames: 3 (2.50%)
50th percentile: 7ms
90th percentile: 13ms
95th percentile: 16ms
99th percentile: 28ms
"""


class FakeTV:
    def __init__(self, theme=None, gfx=GFX, pss_kb=60000, fail_on=None):
        self.theme = theme  # what config.json on the TV holds, as a dict, or None
        self.gfx, self.pss_kb, self.fail_on = gfx, pss_kb, fail_on
        self.commands = []

    def shell(self, cmd):
        self.commands.append(cmd)
        if self.fail_on and self.fail_on in cmd:
            raise RuntimeError("connection dropped")
        if cmd.startswith("cat ") and "config.json" in cmd:
            return json.dumps(self.theme) if self.theme is not None else ""
        if cmd.startswith("dumpsys power"):
            return "  mWakefulness=Awake"
        if cmd.startswith("dumpsys gfxinfo") and "reset" not in cmd:
            return self.gfx
        if cmd.startswith("dumpsys meminfo"):
            return "    TOTAL PSS:    %d    TOTAL RSS:    98304\n" % self.pss_kb
        return ""

    def is_in_front(self, package):
        return True

    def home_component(self):
        return "com.gathin.tvhome/.MainActivity"


class FakeThemes:
    """Stands in for tools/theme.sh: records pushes and keeps FakeTV.theme in step with them."""

    def __init__(self, tv):
        self.tv, self.calls = tv, []

    def __call__(self, folder):
        self.calls.append(None if folder is None else json.loads((folder / "config.json").read_text()))
        self.tv.theme = self.calls[-1]


def run(tv, **kw):
    out = tempfile.mkdtemp()
    themes = FakeThemes(tv)
    s = sweep.Sweep(tv, themes, out, series=2, settle=0, key_gap=0, log=lambda *_: None, **kw)

    def go():
        with mock.patch.object(sweep.time, "sleep"):
            return s.run()
    return s, themes, go


class Parsing(unittest.TestCase):
    def test_gfxinfo(self):
        self.assertEqual(sweep.parse_gfxinfo(GFX), {"frames": 120, "jank_pct": 2.5, "p50_ms": 7, "p90_ms": 13,
                                                    "p95_ms": 16, "p99_ms": 28})

    def test_gfxinfo_empty(self):
        self.assertEqual(sweep.parse_gfxinfo("No process found")["frames"], None)

    def test_sequence_is_one_valid_shell_line(self):
        tv = FakeTV()
        sweep.Sweep(tv, None, tempfile.mkdtemp(), key_gap=0.4).press_sequence()
        self.assertEqual(len(tv.commands), 1)
        self.assertNotIn(";;", tv.commands[0])
        self.assertEqual(tv.commands[0].count("input keyevent"), len(sweep.SEQUENCE))


class Verdicts(unittest.TestCase):
    base = {"frames": 120, "jank_pct": 2.0, "p90_ms": 13, "pss_mb": 60}

    def test_pass(self):
        self.assertEqual(sweep.verdict(dict(self.base), self.base), ("pass", []))

    def test_each_limit(self):
        for change, word in (({"jank_pct": 7.5}, "jank"), ({"p90_ms": 33}, "90th"), ({"pss_mb": 75}, "PSS")):
            with self.subTest(change=change):
                v, why = sweep.verdict(dict(self.base, **change), self.base)
                self.assertEqual(v, "fail")
                self.assertIn(word, why[0])

    def test_no_frames_is_invalid_not_a_pass(self):
        self.assertEqual(sweep.verdict({"frames": None}, self.base)[0], "invalid")

    def test_recommend_takes_only_passing_extras(self):
        results = {"app_glow": {"verdict": "pass"}, "fade_rows": {"verdict": "fail"},
                   "advanced_effects": {"verdict": "pass"}, "no_animations": {"verdict": "pass"}}
        self.assertEqual(sweep.recommend(results), {"app_glow": True, "advanced_effects": True})


class Restore(unittest.TestCase):
    def test_no_theme_before_means_reset_after(self):
        s, themes, go = run(FakeTV(theme=None))
        summary = go()
        self.assertIsNone(themes.calls[-1])
        self.assertTrue(summary["restored"])

    def test_existing_theme_is_pushed_back(self):
        mine = {"columns": 5, "ambient_glow": False}
        s, themes, go = run(FakeTV(theme=dict(mine)))
        summary = go()
        self.assertEqual(themes.calls[-1], mine)
        self.assertTrue(summary["restored"])

    def test_restore_happens_even_when_a_measurement_fails(self):
        tv = FakeTV(theme=None, fail_on="dumpsys gfxinfo com.gathin.tvhome reset")
        s, themes, go = run(tv)
        with self.assertRaises(RuntimeError):
            go()
        self.assertIsNone(themes.calls[-1])
        self.assertIsNone(tv.theme)

    def test_writes_results_and_a_recommendation(self):
        s, themes, go = run(FakeTV())
        summary = go()
        self.assertTrue((s.out_dir / "result.json").exists())
        self.assertTrue((s.out_dir / "summary.md").exists())
        rec = json.loads((s.out_dir / "recommended" / "config.json").read_text())
        # Every variant renders the same healthy numbers here, so every extra passes and is confirmed together.
        self.assertEqual(rec, {"app_glow": True, "fade_rows": True, "advanced_effects": True})
        self.assertEqual(summary["results"]["recommended"]["verdict"], "pass")


if __name__ == "__main__":
    unittest.main()

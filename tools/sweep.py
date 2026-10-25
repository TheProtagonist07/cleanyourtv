#!/usr/bin/env python3
"""Measure what each of the launcher's visual effects costs on this TV, and recommend the richest set it affords.

Each variant is a theme config.json pushed with tools/theme.sh. For each one the script presses a fixed D-pad
sequence on Home and reads `dumpsys gfxinfo` (frames, jank, frame-time percentiles) and the launcher's PSS,
over a few series. A variant passes when it stays inside the limits below; the recommendation is the baseline
plus every effect that passed on its own, measured again together to confirm it.

The TV's own theme is put back at the end, whatever happens: the config.json that was there before the run
is saved first and pushed again, or, when there was none, the overlay is removed with theme.sh --reset. Note
that any theme push, the restore included, clears choices made in the launcher's own Settings screen
(columns, animations, wallpaper pick), as tools/theme.sh always does.

Usage:
    TV_HOST=192.168.1.50 tools/venv/bin/python tools/sweep.py
    TV_HOST=192.168.1.50 tools/venv/bin/python tools/sweep.py --series 5 --out-dir work/sweep-living-room

Writes result.json, summary.md and recommended/config.json under --out-dir (default work/sweep-<time>).
"""
import argparse
import json
import os
from pathlib import Path
import re
import statistics
import subprocess
import sys
import time

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:  # -I drops the script's folder from sys.path; bench.py lives next to us
    sys.path.insert(0, str(HERE))
import bench  # noqa: E402

PACKAGE = "com.gathin.tvhome"
THEME_DIR = "/sdcard/Android/data/%s/files" % PACKAGE

# Effects the launcher reads from a theme's config.json (Config.java) that cost frames or memory.
VARIANTS = [
    ("baseline", {}),
    ("no_animations", {"animations": False}),
    ("app_glow", {"app_glow": True}),
    ("fade_rows", {"fade_rows": True}),
    ("advanced_effects", {"advanced_effects": True}),
    ("no_ambient_glow", {"ambient_glow": False}),
]
# Effects that add something when on: candidates for the recommendation.
EXTRAS = ("app_glow", "fade_rows", "advanced_effects")

LIMITS = {"jank_pct": 5.0, "p90_ms": 20, "pss_delta_mb": 10}

SEQUENCE = ("DPAD_RIGHT", "DPAD_RIGHT", "DPAD_RIGHT", "DPAD_DOWN", "DPAD_LEFT", "DPAD_LEFT", "DPAD_LEFT",
            "DPAD_DOWN", "DPAD_RIGHT", "DPAD_RIGHT", "DPAD_UP", "DPAD_UP")


def parse_gfxinfo(text):
    """The app's totals since the last reset: frames, janky %, and the frame-time percentiles in ms."""
    def num(pattern):
        m = re.search(pattern, text)
        return float(m.group(1)) if m else None
    out = {"frames": num(r"Total frames rendered:\s*(\d+)"),
           "jank_pct": num(r"Janky frames:\s*\d+\s*\(([\d.]+)%\)")}
    for p in (50, 90, 95, 99):
        out["p%d_ms" % p] = num(r"%dth percentile:\s*(\d+)ms" % p)
    return out


def median_of(series, key):
    values = [s[key] for s in series if s.get(key) is not None]
    return round(statistics.median(values), 1) if values else None


def verdict(result, baseline):
    """pass/fail against LIMITS, and why. PSS is judged against the baseline, the rest absolutely."""
    reasons = []
    if not result.get("frames"):
        return "invalid", ["no frames rendered: the launcher was not in front, or the screen was off"]
    if result["jank_pct"] is not None and result["jank_pct"] > LIMITS["jank_pct"]:
        reasons.append("jank %.1f%% > %.0f%%" % (result["jank_pct"], LIMITS["jank_pct"]))
    if result["p90_ms"] is not None and result["p90_ms"] > LIMITS["p90_ms"]:
        reasons.append("90th percentile frame %.0f ms > %d ms" % (result["p90_ms"], LIMITS["p90_ms"]))
    if baseline and result.get("pss_mb") is not None and baseline.get("pss_mb") is not None:
        delta = result["pss_mb"] - baseline["pss_mb"]
        if delta > LIMITS["pss_delta_mb"]:
            reasons.append("PSS +%d MB > %d MB" % (delta, LIMITS["pss_delta_mb"]))
    return ("fail", reasons) if reasons else ("pass", [])


def recommend(results):
    """The baseline plus every extra that passed on its own."""
    config = {}
    for name in EXTRAS:
        r = results.get(name)
        if r and r["verdict"] == "pass":
            config.update(dict(VARIANTS)[name])
    return config


class Sweep:
    def __init__(self, tv, apply_theme, out_dir, series=3, settle=6.0, key_gap=0.4, log=print):
        self.tv, self.apply_theme, self.out_dir = tv, apply_theme, Path(out_dir)
        self.series, self.settle, self.key_gap, self.log = series, settle, key_gap, log

    # --- the TV's own theme, saved before and put back after -------------------------------------------
    def saved_theme(self):
        out = self.tv.shell("cat %s/config.json 2>/dev/null" % THEME_DIR)
        try:
            return json.loads(out) if out.strip() else None
        except ValueError:
            return None

    def restore(self, original):
        if original is None:
            self.apply_theme(None)
        else:
            self.apply_theme(self.write_theme("original", original))

    def write_theme(self, name, config):
        d = self.out_dir / "themes" / name
        d.mkdir(parents=True, exist_ok=True)
        (d / "config.json").write_text(json.dumps(config, indent=2) + "\n")
        return d

    # --- one variant ---------------------------------------------------------------------------------------
    def wait_for_home(self, timeout=20.0):
        end = time.time() + timeout
        while time.time() < end:
            if self.tv.is_in_front(PACKAGE):
                return True
            time.sleep(1.0)
        return False

    def press_sequence(self):
        """All keys in one shell call: a round trip per key would add the network's jitter to the gaps."""
        self.tv.shell("; ".join("input keyevent KEYCODE_%s; sleep %s" % (k, self.key_gap) for k in SEQUENCE))

    def measure(self, name, config):
        self.apply_theme(self.write_theme(name, config))
        time.sleep(self.settle)
        if not self.wait_for_home():
            return {"frames": None, "note": "Home was not in front after the theme push"}
        self.tv.shell("input keyevent KEYCODE_HOME")
        time.sleep(1.5)
        self.press_sequence()  # warm-up: first draws, caches, JIT
        series = []
        for _ in range(self.series):
            self.tv.shell("input keyevent KEYCODE_HOME")
            time.sleep(1.5)
            self.tv.shell("dumpsys gfxinfo %s reset" % PACKAGE)
            self.press_sequence()
            s = parse_gfxinfo(self.tv.shell("dumpsys gfxinfo %s" % PACKAGE))
            s["pss_mb"] = bench.pss_of(self.tv, PACKAGE)
            series.append(s)
        result = {k: median_of(series, k) for k in ("frames", "jank_pct", "p50_ms", "p90_ms", "p95_ms", "p99_ms",
                                                     "pss_mb")}
        result["series"] = series
        return result

    # --- the whole run -------------------------------------------------------------------------------------
    def run(self):
        if (self.tv.home_component() or "").split("/")[0] != PACKAGE:
            raise SystemExit("Home is not %s; install the launcher first (launcher/build-and-install.sh)" % PACKAGE)
        if "mWakefulness=Awake" not in self.tv.shell("dumpsys power | grep mWakefulness"):
            raise SystemExit("the TV's screen is off; turn it on and run again")
        self.out_dir.mkdir(parents=True, exist_ok=True)
        original = self.saved_theme()
        self.log("theme on the TV before the run: %s" % ("none" if original is None else "saved"))
        results = {}
        try:
            for name, config in VARIANTS:
                self.log("measuring %s ..." % name)
                results[name] = self.measure(name, config)
            base = results.get("baseline")
            for name, r in results.items():
                r["verdict"], r["why"] = verdict(r, None if name == "baseline" else base)
            rec = recommend(results)
            if rec:
                self.log("confirming the recommendation together: %s ..." % ", ".join(sorted(rec)))
                r = self.measure("recommended", rec)
                r["verdict"], r["why"] = verdict(r, base)
                results["recommended"] = r
                if r["verdict"] != "pass":
                    rec = {}  # together they do not fit; the baseline is the safe answer
        finally:
            self.log("restoring the TV's theme ...")
            self.restore(original)
        after = self.saved_theme()
        restored = (after == original)
        in_front = self.wait_for_home()
        summary = {"results": results, "recommended": rec, "limits": LIMITS, "restored": restored,
                   "home_in_front_after": in_front}
        (self.out_dir / "result.json").write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n")
        (self.out_dir / "recommended").mkdir(exist_ok=True)
        (self.out_dir / "recommended" / "config.json").write_text(json.dumps(rec, indent=2) + "\n")
        (self.out_dir / "summary.md").write_text(render(summary))
        return summary


def render(summary):
    lines = ["# Effects sweep", "", "Limits: jank at most %.0f%%, 90th-percentile frame at most %d ms, PSS at most "
             "+%d MB over the baseline." % (LIMITS["jank_pct"], LIMITS["p90_ms"], LIMITS["pss_delta_mb"]), "",
             "| Variant | Frames | Jank | p50 | p90 | p99 | PSS | Verdict |", "|---|---|---|---|---|---|---|---|"]
    for name, r in summary["results"].items():
        cell = lambda k, unit="": "?" if r.get(k) is None else "%g%s" % (r[k], unit)  # noqa: E731
        lines.append("| %s | %s | %s | %s | %s | %s | %s | %s%s |" % (
            name, cell("frames"), cell("jank_pct", "%"), cell("p50_ms", " ms"), cell("p90_ms", " ms"),
            cell("p99_ms", " ms"), cell("pss_mb", " MB"), r.get("verdict", "?"),
            ": " + "; ".join(r["why"]) if r.get("why") else ""))
    rec = summary["recommended"]
    lines += ["", "**Recommended config.json:** `%s`" % (json.dumps(rec) if rec else "{} (the defaults)"),
              "", "TV's own theme restored: %s. Home in front after the run: %s." % (
                  "yes" if summary["restored"] else "NO, check it", "yes" if summary["home_in_front_after"] else "NO")]
    return "\n".join(lines) + "\n"


def theme_applier(host):
    """Push a theme folder with tools/theme.sh, or None to remove the overlay (theme.sh --reset)."""
    def apply(folder):
        arg = "--reset" if folder is None else str(folder)
        env = dict(os.environ, TV_HOST=host)
        p = subprocess.run(["bash", str(HERE / "theme.sh"), arg], env=env, capture_output=True, text=True)
        if p.returncode != 0:
            raise RuntimeError("theme.sh %s failed: %s" % (arg, (p.stderr or p.stdout).strip()))
    return apply


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--host", default=os.environ.get("TV_HOST"), help="the TV's IP (default: $TV_HOST)")
    ap.add_argument("--port", type=int, default=int(os.environ.get("TV_PORT", 5555)))
    ap.add_argument("--series", type=int, default=3, help="measured series per variant (default 3)")
    ap.add_argument("--out-dir", default="work/sweep-%s" % time.strftime("%Y%m%d-%H%M%S"))
    args = ap.parse_args(argv)
    if not args.host:
        ap.error("set TV_HOST or pass --host")
    tv = bench.TV(args.host, args.port)
    try:
        summary = Sweep(tv, theme_applier(args.host), args.out_dir, series=args.series).run()
    finally:
        tv.close()
    print(render(summary))
    return 0 if summary["restored"] else 1


if __name__ == "__main__":
    sys.exit(main())

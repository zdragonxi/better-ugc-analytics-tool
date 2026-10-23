"""The Golden Ratio formula lives in roblox_sales/static/golden.js. These cases run in a
headless Chromium-family browser (Edge/Chrome/Chromium) when one is installed — offline, from
a local file — and are skipped otherwise. Set RSA_TEST_BROWSER to point at one explicitly."""
import os
import pathlib
import re
import shutil
import subprocess
import tempfile
import unittest

from helpers import ROOT

GOLDEN = os.path.join(ROOT, "roblox_sales", "static", "golden.js")

CASES = r"""
const eq = (a, b) => a === b || (typeof a === "number" && typeof b === "number" && Math.abs(a - b) < 1e-9);
const cases = [
  // pending, items, robux in range, sales in range, expected ratio, expected text
  [12000, 400, 3000, 150, (12000 / 400) / (3000 / 150), "1.50"],
  [500, 1000, 40000, 100, (500 / 1000) / 400, "0.0013"],
  [0, 10, 100, 10, 0, "0.0000"],
  [null, 10, 100, 10, null, "–"],          // pending unknown
  [100, 0, 100, 10, null, "–"],            // no items on sale
  [100, null, 100, 10, null, "–"],         // items count unknown
  [100, 10, 0, 0, null, "–"],              // no sales in range
  [100, 10, 0, 5, null, "–"],              // sales but 0 Robux: average is 0
];
const fails = [];
for (const [p, i, r, n, want, text] of cases) {
  const got = goldenRatio(p, i, r, n);
  if (!eq(got, want)) fails.push(`goldenRatio(${p},${i},${r},${n}) = ${got}, want ${want}`);
  if (formatGolden(got) !== text) fails.push(`formatGolden(${got}) = ${formatGolden(got)}, want ${text}`);
}
if (formatGolden(0.1) !== "0.10" || formatGolden(0.0999) !== "0.0999") fails.push("format threshold");
document.getElementById("out").textContent = fails.length ? "FAIL " + fails.join(" | ") : "ALLPASS";
"""


def find_browser():
    env = os.environ.get("RSA_TEST_BROWSER")
    if env:
        return env
    for p in (r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
              r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
              r"C:\Program Files\Google\Chrome\Application\chrome.exe"):
        if os.path.isfile(p):
            return p
    for name in ("chromium", "chromium-browser", "google-chrome", "microsoft-edge"):
        p = shutil.which(name)
        if p:
            return p
    return None


def run_cases(lib: str) -> str:
    """Run CASES against `lib` in a headless browser; return the text of <pre id="out">
    only. (The dumped DOM also contains the script source, so searching the whole dump
    for a verdict word would find it in the source and pass no matter what ran.)"""
    tmp = tempfile.mkdtemp(prefix="rsa-golden-")
    try:
        page = os.path.join(tmp, "golden_test.html")
        with open(page, "w", encoding="utf-8") as f:
            f.write(f'<!doctype html><meta charset="utf-8"><pre id="out">NOT RUN</pre>'
                    f"<script>{lib}</script><script>{CASES}</script>")
        dump = subprocess.run(
            [find_browser(), "--headless=new", "--disable-gpu", "--no-first-run",
             f"--user-data-dir={os.path.join(tmp, 'profile')}", "--dump-dom",
             pathlib.Path(page).as_uri()],
            capture_output=True, text=True, timeout=90, encoding="utf-8", errors="replace").stdout
        m = re.search(r'<pre id="out">(.*?)</pre>', dump, re.S)
        return m.group(1).strip() if m else "NO RESULT ELEMENT"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def golden_source() -> str:
    with open(GOLDEN, encoding="utf-8") as f:
        return f.read()


@unittest.skipUnless(find_browser(), "no headless Chromium-family browser found")
class GoldenRatioJsTests(unittest.TestCase):
    def test_formula_and_null_cases(self):
        self.assertEqual(run_cases(golden_source()), "ALLPASS")

    def test_negative_control_a_wrong_formula_is_caught(self):
        """The harness must report FAIL when the formula is wrong (no division by the average)."""
        lib = golden_source()
        broken = lib.replace("return (pending / itemsOnSale) / avgPerSale;", "return pending / itemsOnSale;")
        self.assertNotEqual(broken, lib)            # the mutation really happened
        self.assertTrue(run_cases(broken).startswith("FAIL"))

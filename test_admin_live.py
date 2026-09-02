"""The console's live-update contract.

Two failure modes are silent, which is why they are tested here rather than
eyeballed: a `_page()` that stops emitting `data-live` leaves every auto-updating
view frozen with no error anywhere, and a syntax error inside one of the JS blobs
embedded as a Python string simply means the script never runs — the page renders
fine and nothing in the log says otherwise.
"""
import re
import shutil
import subprocess
import tempfile
import unittest

import admin


class PageLiveAttr(unittest.TestCase):
    def test_refresh_emits_data_live_on_main(self):
        html = admin._page("T", "<p>x</p>", "dashboard", refresh=4)
        self.assertIn('<main data-live="4">', html)

    def test_refresh_no_longer_emits_meta_refresh(self):
        html = admin._page("T", "<p>x</p>", "dashboard", refresh=4)
        self.assertNotIn("http-equiv=\"refresh\"", html)

    def test_no_refresh_emits_plain_main(self):
        # Asserted on the tag, not on the whole page: _LIVE_JS mentions "data-live"
        # in its own source, so a substring check over the document would be a lie.
        html = admin._page("T", "<p>x</p>", "dashboard")
        self.assertIn("<main>", html)
        self.assertNotIn("<main data-live", html)

    def test_body_is_inside_main(self):
        html = admin._page("T", "<p>marker</p>", "dashboard", refresh=2)
        m = re.search(r"<main[^>]*>(.*)</main>", html, re.S)
        self.assertIsNotNone(m, "page must have a <main> element")
        self.assertIn("<p>marker</p>", m.group(1))


class EmbeddedScriptsParse(unittest.TestCase):
    """Every plain inline <script> the console emits must be valid ES5.

    `type=`-carrying scripts (model-viewer's module, the three.js importmap) are
    skipped: they are not plain scripts and `node --check` would reject them for
    reasons that say nothing about our code.
    """

    def test_inline_scripts_are_syntactically_valid(self):
        if not shutil.which("node"):
            self.skipTest("node not installed")
        html = admin._page("T", "<p>x</p>", "dashboard", refresh=4)
        blocks = re.findall(r"<script>(.*?)</script>", html, re.S)
        self.assertTrue(blocks, "expected at least one inline script in the page")
        for i, src in enumerate(blocks):
            with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as fh:
                fh.write(src)
                path = fh.name
            p = subprocess.run(["node", "--check", path],
                               capture_output=True, text=True)
            self.assertEqual(p.returncode, 0,
                             f"inline script #{i} is not valid JS:\n{p.stderr}")


class LiveScriptPresence(unittest.TestCase):
    def test_live_js_is_always_embedded(self):
        # Embedded unconditionally, like _SORT_JS: the script disables itself when
        # <main> carries no data-live, so a static page pays nothing for it.
        # Compared against the constant itself rather than a keyword — _SORT_JS also
        # mentions gwLiveHooks, so a keyword check would pass with _LIVE_JS missing.
        for kwargs in ({"refresh": 4}, {}):
            html = admin._page("T", "<p>x</p>", "dashboard", **kwargs)
            self.assertIn(admin._LIVE_JS, html, f"_LIVE_JS missing for {kwargs}")

    def test_live_js_declares_the_hook_array(self):
        self.assertIn("window.gwLiveHooks", admin._LIVE_JS)


if __name__ == "__main__":
    unittest.main()

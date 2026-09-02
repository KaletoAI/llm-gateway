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

    def test_page_level_script_constants_are_valid(self):
        if not shutil.which("node"):
            self.skipTest("node not installed")
        for name in ("_SCROLL_JS", "_SORT_JS", "_LIVE_JS", "_FILTER_JS", "_JOB_TICK"):
            blob = getattr(admin, name)
            for src in re.findall(r"<script>(.*?)</script>", blob, re.S):
                with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as fh:
                    fh.write(src)
                    path = fh.name
                p = subprocess.run(["node", "--check", path],
                                   capture_output=True, text=True)
                self.assertEqual(p.returncode, 0, f"{name} is not valid JS:\n{p.stderr}")


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

    def test_sort_and_filter_register_post_morph_hooks(self):
        # The server always renders insertion order and the morph reuses nodes, so
        # without these hooks a clicked sort order is undone on every tick and rows
        # morphed in fresh ignore an active filter.
        self.assertIn("gwLiveHooks.push", admin._SORT_JS)
        self.assertIn("gwLiveHooks.push", admin._FILTER_JS)

    def test_filter_no_longer_restores_focus_and_caret(self):
        # Reload compensation only: the morph never replaces a focused input, so the
        # caret needs no rescuing. The filter TEXT persistence stays — it serves real
        # navigation, which the morph does not cover.
        self.assertNotIn("setSelectionRange", admin._FILTER_JS)
        self.assertNotIn("beforeunload", admin._FILTER_JS)
        self.assertNotIn("selectionStart", admin._FILTER_JS)
        self.assertIn("sessionStorage.setItem", admin._FILTER_JS)
        # …and the load-time restore itself must not vanish with them: over-removal is
        # the direction this task is exposed to, and a missing getItem would be silent.
        self.assertIn("sessionStorage.getItem", admin._FILTER_JS)

    def test_scroll_memory_survives_for_real_navigation(self):
        # All three parts stay. <main> is the page's scroll container (body is
        # overflow:hidden), so its position is what F5 or a nav link back to a long
        # list would otherwise lose — the morph covers live updates, not navigation.
        # beforeunload is asserted too: without it the saves never reach the store,
        # and a partial revert that drops only the listener would look green.
        self.assertIn("|main", admin._SCROLL_JS)
        self.assertIn("|master", admin._SCROLL_JS)
        self.assertIn("beforeunload", admin._SCROLL_JS)

    def test_playground_uses_the_shared_live_mechanism(self):
        # The result column had its own poller because a full-page reload would have
        # wiped the form mid-edit. The morph's dirty-input rule covers that now.
        self.assertFalse(hasattr(admin, "_PG_POLL_JS"),
                         "_PG_POLL_JS should be gone — the live morph replaces it")
        self.assertFalse(hasattr(admin, "playground_status"),
                         "the result-fragment route should be gone")

    def test_voice_upload_uses_the_shared_live_mechanism(self):
        # The old poller ended in location.replace(...&vu=done) purely so the library
        # table would show the new entry. The morph updates that table in the same tick.
        self.assertFalse(hasattr(admin, "_VU_POLL_JS"),
                         "_VU_POLL_JS should be gone — the live morph replaces it")
        self.assertFalse(hasattr(admin, "voice_upload_status"),
                         "the upload-progress fragment route should be gone")


# Fixtures for the identity tests below. Built from what the three row templates
# actually read — nothing invented: `_job_row` needs id/status plus the created/
# updated pair `_job_dur_cell` measures, `_call_row` destructures a 15-column stats
# tuple whose first element is the call id, and `_dash_parked` reads a dict with a
# non-empty `parked_calls` list (it returns "" for an empty one, which would make the
# assertion pass for the wrong reason).
_JOB_FIXTURE = {
    "id": "9f3c1ab27de44b0e",
    "status": "done",
    "task": "image",
    "alias": "sdxl",
    "backend": "comfy-a",
    "created": 1_799_999_400,
    "updated": 1_799_999_460,
    "owner": "kai",
    "result_count": 2,
}

# (cid, ts, dur, backend, source, alias, model, endpoint, status, in, out, cost,
#  preview, has_body, reasoning)
_CALL_FIXTURE = (4711, 1_799_999_400, 1234, "local-llama", "10.0.0.5", "chat",
                 "gemma-4", "/v1/chat/completions", 200, 120, 45, 0.0012,
                 "hello", 1, "off:prefill")

_PARKED_FIXTURE = {"parked_calls": [
    {"alias": "chat", "source": "10.0.0.5", "waited_s": 3.2, "remaining_s": 56.8},
]}


class LiveIdentity(unittest.TestCase):
    """Stable identity for everything the morph reconciles.

    Without keys the morph matches positionally, and all three of these fail
    silently — the page still renders correctly, it just rewrites every cell,
    reuses a table node for a different table, or shows a sort UI that ignores
    clicks. Nothing but these assertions would notice.
    """

    def test_job_row_carries_the_job_id_as_key(self):
        row = admin._job_row(_JOB_FIXTURE, 1_800_000_000)
        self.assertIn('data-k="job-', row)
        self.assertIn(_JOB_FIXTURE["id"], row)

    def test_call_row_carries_the_call_id_as_key(self):
        row = admin._call_row(_CALL_FIXTURE, {})
        self.assertIn('data-k="call-', row)

    def test_parked_rows_stay_unkeyed(self):
        # Parked entries have no identity — only alias, source and two values
        # that change every tick. An invented key would be worse than none.
        self.assertNotIn("data-k", admin._dash_parked(_PARKED_FIXTURE))

    def test_keyof_accepts_data_sk_so_sortable_tables_have_identity(self):
        self.assertIn("data-sk", admin._LIVE_JS)

    def test_sort_js_wires_tables_it_has_not_seen(self):
        # A table the morph inserts mid-session gets no click handlers unless the
        # post-morph hook wires it — before this branch a refresh was a reload and
        # re-bound everything, so an unwired table is a regression, not a gap.
        self.assertIn("__gwWired", admin._SORT_JS)


if __name__ == "__main__":
    unittest.main()

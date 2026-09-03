"""The console is kind-neutral: every place that used to ask `== "meshy"` now asks the
adapters' kind helpers, so a second cloud backend (Tripo) needs no new branch. These
three fail SILENTLY in the browser — a job view that renders nothing for a Tripo run, an
editor that offers the wrong backends, a type select whose JS never reveals the cloud
option block — so they are pinned here.

Run: python -m unittest test_cloud_editor -v
"""
import os
import sys
import tempfile
import unittest

# `import main` reads ./config.yaml at import time — give it a minimal one in a temp cwd.
# The dir is needed ONLY for that import, so it is removed in the same finally that
# restores the cwd; leaving it to the finalizer raises a ResourceWarning under -W error.
_here = os.path.dirname(os.path.abspath(__file__))
_prev = os.getcwd()
_tmp = tempfile.TemporaryDirectory()
with open(os.path.join(_tmp.name, "config.yaml"), "w") as _f:
    _f.write('api_key: ""\nbackends: []\n')
os.chdir(_tmp.name)
sys.path.insert(0, _here)
try:
    import main
    import admin
    import adapters
    import meshy
    import tripo
finally:
    os.chdir(_prev)
    _tmp.cleanup()
    del _tmp


class AdminKindNeutral(unittest.TestCase):
    def test_cloud_table_reads_both_id_keys(self):
        import admin
        html = admin._cloud_table("Cloud", {"meshy_task_id": "m1", "request": {"a": 1},
                                            "endpoint": "image-to-3d"})
        self.assertIn("m1", html); self.assertIn("Meshy", html)
        html = admin._cloud_table("Cloud", {"cloud": "tripo", "cloud_task_id": "t1",
                                            "request": {"input": "tok"}, "endpoint": "rig",
                                            "tasks": [{"role": "rig-check", "task_id": "t0",
                                                       "credits": 0}]})
        self.assertIn("t1", html); self.assertIn("Tripo", html)
        self.assertIn("rig-check", html); self.assertIn("t0", html)
        self.assertEqual(admin._cloud_table("x", {"request": {}}), "")

    def test_same_kind_matches_backend_type(self):
        import admin, tripo
        admin._gen_backends = lambda: [{"name": "x", "type": "comfyui"}, {"name": "x", "type": "tripo"},
                                       {"name": "m", "type": "meshy"}]
        self.assertTrue(admin._same_kind([tripo.default_candidate("x")], "x"))
        self.assertFalse(admin._same_kind([tripo.default_candidate("x")], "m"))
        self.assertTrue(admin._same_kind([{"workflow_json": {}}], "x"))
        self.assertFalse(admin._same_kind([meshy.default_candidate("m")], "x"))

    def test_type_select_knows_every_cloud_url(self):
        import admin, tripo
        html = admin._type_select("tripo")
        self.assertIn(tripo.URL, html); self.assertIn(meshy.URL, html)
        self.assertIn('value="tripo" selected', html)
        self.assertIn("cloudopts", html)

    def test_type_select_replaces_another_kinds_url(self):
        """Switching meshy → tripo must not leave api.meshy.ai in the url field: the
        backend would be saved pointing at the wrong vendor and only fail at discovery,
        with an auth error that names the wrong service. The handler therefore overwrites
        a url that is blank OR equals ANOTHER kind's fixed URL — never a typed one."""
        import admin
        js = admin._type_select("meshy")
        self.assertIn("for(var k in cloudUrls)", js)          # every other kind is compared
        self.assertIn("u.value===cloudUrls[k]", js)           # …against the url field
        self.assertIn("u.value=cloudUrls[t]", js)             # …and replaced by the new kind's
        for url in (meshy.URL, tripo.URL):
            self.assertIn(url, js)
        self.assertNotIn("if(u&&!u.value)", js)               # the old fill-only rule is gone


if __name__ == "__main__":
    unittest.main()

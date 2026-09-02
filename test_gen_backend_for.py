"""Backend names are unique only PER TYPE (the store keys backends by (name, type)),
so a ComfyUI backend and a Meshy backend may both be called "gpu". Generation now spans
both types, and a bare-name lookup would happily hand a Meshy alias the GPU box — the
job then fails opaquely inside the wrong adapter. These tests pin the two lookups that
decide the kind: main._gen_backend_for (routing/cancel) and admin._same_kind (the editor's
"which backends may I add" filter).

Run: python -m unittest test_gen_backend_for -v
"""
import os
import sys
import tempfile
import unittest

# `import main` reads ./config.yaml at import time — give it a minimal one in a temp cwd.
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
finally:
    os.chdir(_prev)


COMFY = {"name": "gpu", "type": "comfyui"}
MESHY = {"name": "gpu", "type": "meshy"}
OTHER = {"name": "gpu2", "type": "comfyui"}
POOL = [COMFY, MESHY, OTHER]

MESHY_CAND = {"backend": "gpu", "meshy": {"task": "img2mesh"}}
WF_CAND = {"backend": "gpu", "workflow_json": {"1": {}}}


class TestGenBackendFor(unittest.TestCase):
    def test_meshy_candidate_picks_the_meshy_backend(self):
        self.assertIs(main._gen_backend_for("gpu", MESHY_CAND, POOL), MESHY)

    def test_workflow_candidate_picks_the_comfy_backend(self):
        self.assertIs(main._gen_backend_for("gpu", WF_CAND, POOL), COMFY)

    def test_order_does_not_decide(self):
        rev = list(reversed(POOL))
        self.assertIs(main._gen_backend_for("gpu", MESHY_CAND, rev), MESHY)
        self.assertIs(main._gen_backend_for("gpu", WF_CAND, rev), COMFY)

    def test_no_backend_of_that_kind_is_none(self):
        self.assertIsNone(main._gen_backend_for("gpu", MESHY_CAND, [COMFY, OTHER]))
        self.assertIsNone(main._gen_backend_for("gpu2", MESHY_CAND, POOL))

    def test_missing_candidate_means_workflow_kind(self):
        # cancel of a legacy row without a candidate: treat it as the workflow kind.
        self.assertIs(main._gen_backend_for("gpu", None, POOL), COMFY)

    def test_pool_defaults_to_the_enabled_generation_backends(self):
        prev = list(main._gen_backends)
        main._gen_backends[:] = POOL
        try:
            self.assertIs(main._gen_backend_for("gpu", MESHY_CAND), MESHY)
        finally:
            main._gen_backends[:] = prev


class TestSameKind(unittest.TestCase):
    def setUp(self):
        self._prev = admin._gen_backends
        admin._gen_backends = lambda: list(POOL)

    def tearDown(self):
        admin._gen_backends = self._prev

    def test_meshy_alias_accepts_only_the_meshy_backend(self):
        self.assertTrue(admin._same_kind([MESHY_CAND], "gpu"))
        self.assertFalse(admin._same_kind([MESHY_CAND], "gpu2"))

    def test_workflow_alias_accepts_only_the_comfy_backend(self):
        self.assertTrue(admin._same_kind([WF_CAND], "gpu"))
        self.assertTrue(admin._same_kind([WF_CAND], "gpu2"))

    def test_meshy_alias_rejects_a_same_named_comfy_only_pool(self):
        admin._gen_backends = lambda: [COMFY, OTHER]
        self.assertFalse(admin._same_kind([MESHY_CAND], "gpu"))

    def test_unknown_backend(self):
        self.assertFalse(admin._same_kind([WF_CAND], "nope"))


if __name__ == "__main__":
    unittest.main()

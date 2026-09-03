"""What `_run_job` does with an EXECUTION error — the arm that decides whether a user's
job dies on a broken backend or lands on a working one.

Why this file exists (it fails SILENTLY): every case here ends with a job row that
looks plausible on its own. A job that died on the first candidate reads like a genuine
workflow error; a fault charged to the wrong candidate quietly takes a healthy backend
out of rotation for 15 minutes; and a cloud candidate that fails over would re-run a
task the vendor already BILLED — none of it raises, and none of it shows up anywhere
except as a bill or an idle GPU.

Run: venv/bin/python -m unittest test_run_job_failover -v
"""
import asyncio
import os
import sys
import tempfile
import time
import types
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
    import scheduler
finally:
    os.chdir(_prev)
    _tmp.cleanup()
    del _tmp


class _FakeJobs:
    """Stands in for the jobs module: records the outcome instead of touching SQLite."""

    def __init__(self):
        self.status = None
        self.completed = None
        self.failed = None
        self.backend = None

    def set_status(self, job_id, st):
        self.status = st

    def set_backend(self, job_id, name):
        self.backend = name

    def complete(self, job_id, blobs, meta):
        self.completed = {"blobs": blobs, "meta": meta}

    def fail(self, job_id, msg, meta=None):
        self.failed = {"msg": msg, "meta": meta}


class _Adapter:
    """generate() either raises `boom` or returns one artifact."""

    def __init__(self, boom=None):
        self.boom = boom
        self.calls = 0

    async def generate(self, req):
        self.calls += 1
        if self.boom is not None:
            raise self.boom
        return types.SimpleNamespace(blobs=[b"art"], meta={})


class RunJobExecFailover(unittest.TestCase):

    def setUp(self):
        self.jobs = _FakeJobs()
        self._orig_jobs = main.jobs
        self._orig_free = main._free_comfy_vram
        main.jobs = self.jobs

        async def _no_free(backend, why):        # no ComfyUI to POST /free to
            return None

        main._free_comfy_vram = _no_free
        main.gen_exec_faults.clear()
        main.gen_speed.clear()
        main.backend_gen_window.clear()
        main.backend_inflight.clear()
        main.backend_adapters.clear()

    def tearDown(self):
        main.jobs = self._orig_jobs
        main._free_comfy_vram = self._orig_free
        main.gen_exec_faults.clear()
        main.backend_adapters.clear()

    def _run(self, cands, adapters_by_bid):
        main.backend_adapters.update(adapters_by_bid)
        asyncio.run(main._run_job("job1", "alias1", cands,
                                  lambda b, c: types.SimpleNamespace(slot_held=False)))

    @staticmethod
    def _comfy(name):
        return ({"name": name, "type": "comfyui"}, {"backend": name, "workflow_json": {}})

    @staticmethod
    def _key(name):
        return f"alias1|comfyui:{name}"

    def test_execution_error_fails_over_to_the_next_backend(self):
        """The 2026-09-03 case: candidate one runs the prompt and blows up, candidate
        two delivers. The job must be DONE, not failed."""
        bad, good = self._comfy("bad"), self._comfy("good")
        self._run([bad, good], {"comfyui:bad": _Adapter(RuntimeError("node 1 blew up")),
                                "comfyui:good": _Adapter()})
        self.assertIsNotNone(self.jobs.completed)
        self.assertIsNone(self.jobs.failed)
        self.assertEqual(self.jobs.backend, "good")     # row re-pointed at the real runner

    def test_the_failing_backend_is_charged_only_because_another_succeeded(self):
        bad, good = self._comfy("bad"), self._comfy("good")
        self._run([bad, good], {"comfyui:bad": _Adapter(RuntimeError("boom")),
                                "comfyui:good": _Adapter()})
        self.assertTrue(scheduler.exec_probed(main.gen_exec_faults, self._key("bad")))
        # the winner must stay clean, or a working backend drifts into quarantine
        self.assertFalse(scheduler.exec_probed(main.gen_exec_faults, self._key("good")))

    def test_two_proven_faults_quarantine_the_candidate(self):
        bad, good = self._comfy("bad"), self._comfy("good")
        for _ in range(2):
            self._run([bad, good], {"comfyui:bad": _Adapter(RuntimeError("boom")),
                                    "comfyui:good": _Adapter()})
        self.assertTrue(scheduler.exec_quarantined(main.gen_exec_faults,
                                                   self._key("bad"), time.time()))

    def test_when_every_candidate_fails_nobody_is_charged(self):
        """All three failed the same way → the REQUEST is the common factor. Charging
        them here would let one bad workflow quarantine an alias's whole fleet."""
        a, b = self._comfy("a"), self._comfy("b")
        self._run([a, b], {"comfyui:a": _Adapter(RuntimeError("bad model name")),
                           "comfyui:b": _Adapter(RuntimeError("bad model name"))})
        self.assertIsNotNone(self.jobs.failed)
        self.assertEqual(main.gen_exec_faults, {})

    def test_the_report_names_the_execution_error_not_the_network(self):
        a, b = self._comfy("a"), self._comfy("b")
        self._run([a, b], {"comfyui:a": _Adapter(RuntimeError("node 1 UNETLoader: nope")),
                           "comfyui:b": _Adapter(RuntimeError("node 1 UNETLoader: nope"))})
        msg = self.jobs.failed["msg"]
        self.assertIn("UNETLoader", msg)
        self.assertNotIn("unreachable", msg)
        self.assertIn("2 backends", msg)      # and it says the request is the suspect

    def test_execution_error_never_repeats_on_the_same_backend(self):
        """self_retries exists for sporadic driver faults on the connection path. An
        execution error reproduces, so re-running it just burns the user's time."""
        bad = ({"name": "bad", "type": "comfyui", "self_retries": 3},
               {"backend": "bad", "workflow_json": {}})
        ad = _Adapter(RuntimeError("boom"))
        self._run([bad], {"comfyui:bad": ad})
        self.assertEqual(ad.calls, 1)

    def test_a_cloud_candidate_never_fails_over(self):
        """A cloud task is BILLED. Whatever failed may have happened after the paid task
        was created, so re-running the job on the next candidate buys it twice."""
        cloud = ({"name": "meshy", "type": "meshy"},
                 {"backend": "meshy", "meshy": {"endpoint": "image-to-3d"}})
        good = self._comfy("good")
        good_ad = _Adapter()
        self._run([cloud, good], {"meshy:meshy": _Adapter(RuntimeError("task rejected")),
                                  "comfyui:good": good_ad})
        self.assertIsNotNone(self.jobs.failed)
        self.assertEqual(good_ad.calls, 0)           # the second candidate never ran
        self.assertIsNone(self.jobs.completed)


if __name__ == "__main__":
    unittest.main()

"""Chain hooks: what a stage-1 / stage-2 adapter contributes to a workflow chain.
run: /home/dev/projekte/llm-gateway/venv/bin/python -m unittest test_chain_hooks -v"""
import asyncio
import unittest

import adapters
from adapters import ComfyUIAdapter, GenBlob, GenOutput, NormalizedRequest

WF = {
    "47": {"inputs": {"value": "Kai"}, "class_type": "PrimitiveString", "_meta": {"title": "input_name"}},
    "100": {"inputs": {"filename_prefix": ["47", 0], "file_format": "glb", "trimesh": ["107", 0]},
            "class_type": "Trellis2ExportMesh", "_meta": {"title": "Output"}},
}


def _ctx():
    return adapters.AdapterContext(
        auth_headers=lambda b: {}, inflight_inc=lambda bid: None, inflight_dec=lambda bid: None,
        cost_usd=lambda *a: 0.0, source_of=lambda r: "test", record_call=lambda *a, **k: None,
        log_enabled=lambda: False)


def _comfy():
    return ComfyUIAdapter({"name": "gpu", "type": "comfyui", "url": "http://127.0.0.1:1",
                           "comfy_input_dir": "/srv/comfy/input"}, _ctx())


class ComfyChainExport(unittest.TestCase):
    def test_pins_export_node_and_names_mesh(self):
        ex = _comfy().chain_export({"workflow_json": WF}, {"export_node": "100"}, {}, "gwchain_j1")
        self.assertIsNone(ex.error)
        self.assertEqual(ex.mesh_name, "gwchain_j1_00001_.glb")
        self.assertEqual(ex.extra_fixed, [{"node": "100", "field": "filename_prefix", "value": "gwchain_j1"}])

    def test_mapped_file_format_overrides_ext(self):
        cand = {"workflow_json": WF, "mapping": {"fmt": {"node": "100", "field": "file_format", "label": "input_format"}}}
        ex = _comfy().chain_export(cand, {"export_node": "100"}, {"input_format": "obj"}, "gwchain_j1")
        self.assertEqual(ex.mesh_name, "gwchain_j1_00001_.obj")

    def test_pinned_file_format_beats_mapping(self):
        cand = {"workflow_json": WF,
                "mapping": {"fmt": {"node": "100", "field": "file_format"}},
                "fixed": [{"node": "100", "field": "file_format", "value": "fbx"}]}
        ex = _comfy().chain_export(cand, {"export_node": "100"}, {"fmt": "obj"}, "gwchain_j1")
        self.assertEqual(ex.mesh_name, "gwchain_j1_00001_.fbx")

    def test_bad_export_node_is_an_error_not_a_crash(self):
        ex = _comfy().chain_export({"workflow_json": WF}, {"export_node": "47"}, {}, "gwchain_j1")
        self.assertIsNotNone(ex.error)
        self.assertIn("filename_prefix", ex.error)

    def test_no_workflow_is_an_error(self):
        ex = _comfy().chain_export({}, {"export_node": "100"}, {}, "gwchain_j1")
        self.assertIsNotNone(ex.error)


class ComfyChainFeed(unittest.TestCase):
    def test_path_relay_returns_shared_disk_path(self):
        req2 = NormalizedRequest(alias="rig")
        ref = asyncio.run(_comfy().chain_feed_mesh(req2, {"name": "gpu"}, "input_mesh_path",
                                                   "gwchain_j1_00001_.glb", None, "/srv/comfy/output"))
        self.assertEqual(ref, "/srv/comfy/output/gwchain_j1_00001_.glb")
        self.assertEqual(req2.upload_files, {})


class BaseDefaults(unittest.TestCase):
    def test_base_adapter_refuses_chain_roles(self):
        base = adapters.OpenAIAdapter({"name": "llm", "type": "openai", "url": "http://x"}, _ctx())
        ex = base.chain_export({}, {}, {}, "p")
        self.assertIsNotNone(ex.error)
        self.assertIn("openai", ex.error)
        with self.assertRaises(RuntimeError):
            asyncio.run(base.chain_feed_mesh(NormalizedRequest(), {}, "m", "n", b"x", ""))
        self.assertIsNone(asyncio.run(base.chain_take_mesh(GenOutput(blobs=[]), ex, True)))


if __name__ == "__main__":
    unittest.main()

"""Unit tests for cloudtask.py — run: /home/dev/projekte/llm-gateway/venv/bin/python -m unittest test_cloudtask -v"""
import unittest

import cloudtask
import meshy

FIELDS = [
    {"key": "flag", "label": "flag", "type": "bool"},
    {"key": "mode", "label": "mode", "type": "select", "choices": [("a", "A"), ("b", "B")]},
    {"key": "tri", "label": "tri", "type": "tristate"},
    {"key": "n", "label": "n", "type": "int"},
    {"key": "txt", "label": "txt", "type": "text"},
    {"key": "lst", "label": "lst", "type": "list"},
]
DEFAULTS = {"flag": True, "mode": "a", "tri": None, "n": None, "txt": "", "lst": []}


class ParseOptions(unittest.TestCase):
    def test_reads_every_type(self):
        form = {"opt__flag": "on", "opt__mode": "b", "opt__tri": "false", "opt__n": "42",
                "opt__txt": " hi ", "opt__lst": "preset:walk, preset:run,,"}
        out = cloudtask.parse_options(FIELDS, form, DEFAULTS)
        self.assertEqual(out, {"flag": True, "mode": "b", "tri": False, "n": 42,
                               "txt": "hi", "lst": ["preset:walk", "preset:run"]})

    def test_missing_checkbox_is_false_and_blank_is_default_shape(self):
        out = cloudtask.parse_options(FIELDS, {"opt__mode": "zzz", "opt__n": "x", "opt__tri": ""}, DEFAULTS)
        self.assertFalse(out["flag"])                  # an unchecked box is not submitted
        self.assertEqual(out["mode"], "a")             # unknown choice → default
        self.assertIsNone(out["n"])                    # garbage int → None (module validates later)
        self.assertIsNone(out["tri"])
        self.assertEqual(out["lst"], [])

    def test_does_not_mutate_defaults(self):
        d = dict(DEFAULTS)
        cloudtask.parse_options(FIELDS, {"opt__lst": "x"}, d)
        self.assertEqual(d, DEFAULTS)

    def test_field_value_str(self):
        self.assertEqual(cloudtask.field_value_str({"type": "tristate"}, None), "")
        self.assertEqual(cloudtask.field_value_str({"type": "tristate"}, True), "true")
        self.assertEqual(cloudtask.field_value_str({"type": "int"}, None), "")
        self.assertEqual(cloudtask.field_value_str({"type": "int"}, 7), "7")
        self.assertEqual(cloudtask.field_value_str({"type": "list"}, ["a", "b"]), "a, b")


class MeshyFields(unittest.TestCase):
    def test_meshy_option_fields_roundtrip_defaults(self):
        """Rendering the defaults into a form and parsing them back yields the defaults —
        the editor and the request builder read the same table."""
        form = {}
        for fld in meshy.OPTION_FIELDS:
            v = meshy.OPTION_DEFAULTS[fld["key"]]
            if fld["type"] == "bool":
                if v:
                    form[f"opt__{fld['key']}"] = "on"
            else:
                form[f"opt__{fld['key']}"] = cloudtask.field_value_str(fld, v)
        out = cloudtask.parse_options(meshy.OPTION_FIELDS, form, meshy.OPTION_DEFAULTS)
        for fld in meshy.OPTION_FIELDS:
            self.assertEqual(out[fld["key"]], meshy.OPTION_DEFAULTS[fld["key"]], fld["key"])

    def test_meshy_fields_cover_every_option_except_formats(self):
        keys = {f["key"] for f in meshy.OPTION_FIELDS}
        self.assertEqual(keys, set(meshy.OPTION_DEFAULTS) - {"target_formats"})

    def test_meshy_module_constants(self):
        self.assertEqual((meshy.KIND, meshy.VENDOR, meshy.RIG_ENDPOINT), ("meshy", "Meshy", "rigging"))
        self.assertTrue(meshy.URL.startswith("https://"))
        self.assertIs(meshy.TaskState, cloudtask.TaskState)

    def test_parse_task_options_kwarg(self):
        task = {"status": "SUCCEEDED", "result": {"rigged_character_glb_url": "u",
                                                  "basic_animations": {"walking_glb_url": "w"}}}
        st = meshy.parse_task(task, ["glb"], "rigging", options={"animations": True})
        self.assertEqual([n for n, _ in st.downloads], ["rigged.glb", "walking.glb"])
        st = meshy.parse_task(task, ["glb"], "rigging", options={"animations": False})
        self.assertEqual([n for n, _ in st.downloads], ["rigged.glb"])


class AdapterHelpers(unittest.TestCase):
    """The kind seam in adapters.py — pure enough to check without a server. main and
    admin ask these four questions everywhere a candidate or backend has a kind."""

    def test_kinds(self):
        import adapters
        self.assertEqual(adapters.cloud_kind({"meshy": {}}), "meshy")
        self.assertEqual(adapters.cloud_kind({"tripo": {"endpoint": "rig"}}), "tripo")
        self.assertIsNone(adapters.cloud_kind({"workflow_json": {}}))
        self.assertEqual(adapters.cand_kind({}), "comfyui")
        self.assertEqual(adapters.backend_kind({"type": "meshy"}), "meshy")
        self.assertEqual(adapters.backend_kind({"type": "comfyui"}), "comfyui")
        self.assertEqual(adapters.backend_kind({"type": "openai"}), "comfyui")
        self.assertIn("meshy", adapters.CLOUD_TYPES)
        self.assertTrue(adapters.CLOUD_TYPES <= adapters.GEN_TYPES)
        self.assertIs(adapters.cloud_module("meshy"), meshy)
        self.assertEqual(adapters.cloud_block({"tripo": {"endpoint": "rig"}}), {"endpoint": "rig"})
        self.assertIsNone(adapters.cloud_block({}))

    def test_exception_aliases_and_vendor(self):
        import adapters
        self.assertIs(adapters.MeshyNoCredits, adapters.CloudNoCredits)
        self.assertIs(adapters.MeshyBusy, adapters.CloudBusy)
        e = adapters.CloudNoCredits("x", vendor="Tripo")
        self.assertIsInstance(e, ConnectionError)
        self.assertEqual(e.vendor, "Tripo")
        self.assertEqual(adapters.CloudBusy("y").vendor, "cloud")


if __name__ == "__main__":
    unittest.main()

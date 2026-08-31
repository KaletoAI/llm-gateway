"""Dead-branch pruning for `on_empty: disable` image slots (adapters._prune_branch).

The cascade rule is what ComfyUI itself enforces: a node whose REQUIRED input link
disappears cannot run, so it dies with the branch; a node that only lost an OPTIONAL
one keeps running without it. Getting that backwards is invisible until a real
generation aborts at /prompt, so it is worth a test.

Fixture mirrors the shape of img2mesh-trellis2_multiview_api.json (verified against
the live /object_info of a Trellis2 backend, 2026-08-30):
    loader → PreProcess(image required) → MultiView(front required, back/left/right optional)

    venv/bin/python -m unittest test_prune_branch -v
"""
import unittest

import adapters


TYPES = {
    "Loader":    {"out": ["IMAGE"], "in": {}, "req": ["image"]},
    "PreProcess": {"out": ["IMAGE"], "in": {"image": "IMAGE"},
                   "req": ["image", "padding", "remove_background"]},
    "MultiView": {"out": ["MESH"], "in": {"front_image": "IMAGE", "back_image": "IMAGE"},
                  "req": ["pipeline", "front_image"]},
    "Post":      {"out": ["MESH"], "in": {"mesh": "MESH"}, "req": ["mesh"]},
}


def _wf():
    """front + back through a PreProcess each, left wired straight into MultiView."""
    return {
        "58":  {"class_type": "Loader", "inputs": {"image": "front.png"}},
        "108": {"class_type": "Loader", "inputs": {"image": "back.png"}},
        "109": {"class_type": "Loader", "inputs": {"image": "left.png"}},
        "61":  {"class_type": "PrimitiveBoolean", "inputs": {"value": True}},
        "105": {"class_type": "PreProcess",
                "inputs": {"padding": 25, "remove_background": ["61", 0], "image": ["58", 2]}},
        "104": {"class_type": "PreProcess",
                "inputs": {"padding": 25, "remove_background": ["61", 0], "image": ["108", 2]}},
        "106": {"class_type": "MultiView",
                "inputs": {"seed": 1, "pipeline": ["82", 0], "front_image": ["105", 0],
                           "back_image": ["104", 0], "left_image": ["109", 2]}},
        "107": {"class_type": "Post", "inputs": {"mesh": ["106", 0]}},
        "82":  {"class_type": "LoadModel", "inputs": {"modelname": "x"}},
    }


class PruneBranch(unittest.TestCase):

    def test_required_consumer_dies_with_the_loader(self):
        """back loader gone → its PreProcess cannot run (image is required) → both go,
        and MultiView merely loses its OPTIONAL back_image."""
        wf = _wf()
        removed = adapters._prune_branch(wf, "108", TYPES)
        self.assertEqual(set(removed), {"108", "104"})
        self.assertNotIn("104", wf)
        self.assertIn("106", wf)
        self.assertNotIn("back_image", wf["106"]["inputs"])
        self.assertEqual(wf["106"]["inputs"]["front_image"], ["105", 0])

    def test_optional_consumer_survives(self):
        """left is wired straight into an OPTIONAL socket → only the loader goes."""
        wf = _wf()
        removed = adapters._prune_branch(wf, "109", TYPES)
        self.assertEqual(removed, ["109"])
        self.assertIn("106", wf)
        self.assertNotIn("left_image", wf["106"]["inputs"])

    def test_cascade_follows_a_required_chain_to_the_end(self):
        """front feeds a REQUIRED socket → the whole downstream chain is dead."""
        wf = _wf()
        removed = adapters._prune_branch(wf, "58", TYPES)
        self.assertEqual(set(removed), {"58", "105", "106", "107"})
        self.assertEqual(set(wf), {"108", "109", "61", "104", "82"})

    def test_sibling_link_from_a_surviving_node_is_kept(self):
        """The PreProcess also reads a boolean from 61 — pruning must not touch it."""
        wf = _wf()
        adapters._prune_branch(wf, "108", TYPES)
        self.assertIn("61", wf)
        self.assertEqual(wf["105"]["inputs"]["remove_background"], ["61", 0])

    def test_unknown_class_stops_the_cascade(self):
        """No /object_info for the consumer → fall back to removing just the node
        (the pre-cascade behaviour), never a guess about what it needs."""
        wf = _wf()
        removed = adapters._prune_branch(wf, "108", {})
        self.assertEqual(removed, ["108"])
        self.assertIn("104", wf)
        self.assertNotIn("image", wf["104"]["inputs"])   # link still dropped

    def test_absent_node_is_a_noop(self):
        wf = _wf()
        self.assertEqual(adapters._prune_branch(wf, "999", TYPES), [])
        self.assertEqual(len(wf), 9)

    def test_second_required_input_kills_a_shared_consumer(self):
        """A node with two REQUIRED image links dies when either one goes."""
        wf = {"1": {"class_type": "Loader", "inputs": {"image": "a.png"}},
              "2": {"class_type": "Loader", "inputs": {"image": "b.png"}},
              "3": {"class_type": "Batch", "inputs": {"image1": ["1", 0], "image2": ["2", 0]}}}
        types = {"Batch": {"out": ["IMAGE"], "in": {}, "req": ["image1", "image2"]}}
        removed = adapters._prune_branch(wf, "2", types)
        self.assertEqual(set(removed), {"2", "3"})
        self.assertEqual(set(wf), {"1"})


class NodeTypeEntry(unittest.TestCase):

    def test_required_field_names_are_captured(self):
        """`req` carries EVERY required field (a required COMBO is still required),
        while `in` keeps only the link-typed ones as before."""
        e = adapters._node_type_entry({
            "output": ["IMAGE"],
            "input": {"required": {"image": ["IMAGE"], "mode": [["a", "b"]], "n": ["INT", {}]},
                      "optional": {"mask": ["MASK"]}}})
        self.assertEqual(set(e["req"]), {"image", "mode", "n"})
        self.assertEqual(e["in"], {"image": "IMAGE", "n": "INT", "mask": "MASK"})
        self.assertEqual(e["out"], ["IMAGE"])


if __name__ == "__main__":
    unittest.main()

"""Skinning-weight repair for a rigged GLB, run in the gateway before delivery.

Auto-riggers mis-bind vertices two ways, both invisible in bind pose but ugly once
animated: a DRAG/SPIKE (a weight on a bone far from the vertex — a shin vertex partly
weighted to the foot bone shoots out when the ankle turns) and a RING/WEB (a crotch or
armpit vertex weighted to BOTH sibling limbs — it stretches into a web when the legs
spread). The error rate rises with mesh resolution (a 148k-vert Bianca had ~2700 both-
leg vertices; a 57k Trellis mesh a handful). Fixing it client-side would cost millions
of distance ops on every page load per character; doing it once here is cheap.

Pure numpy on the glTF data — no 3D engine. Only JOINTS_0/WEIGHTS_0 change.

Method (all local, per vertex — the skeleton hierarchy is the ground truth, NOT geometric
neighbours, which cross the left/right gap where two limbs touch in space):
  1. Bone segments: from each bone's world position (inverse bind matrix) to its first
     child bone; childless bones stay a point.
  2. Drop DRAG: any weight on a bone whose segment is >~2.5x farther than the nearest
     segment (a long-range pull). At-joint blends survive — there both bones are close.
  3. Drop RING: keep only a single ancestor chain of bones (Hips→UpLeg→Leg→Foot). Walking
     the remaining weights high→low, a bone is dropped if it is incompatible (neither an
     ancestor nor a descendant) of a heavier kept bone — i.e. a sibling limb. Renormalize;
     a vertex left with nothing is pinned rigidly to its single nearest bone.
  4. Seam unification (run BEFORE 2-3 so the chain enforcement has the last word): UV-seam
     twins at the same rounded position share one weight set, else the seam tears.
"""
import json
import struct

import numpy as np

_CT = {5120: "<i1", 5121: "<u1", 5122: "<i2", 5123: "<u2", 5125: "<u4", 5126: "<f4"}
_NC = {"SCALAR": 1, "VEC2": 2, "VEC3": 3, "VEC4": 4, "MAT2": 4, "MAT3": 9, "MAT4": 16}
_MISWEIGHT_RATIO = 2.5      # a weighted bone this much farther than the nearest → mis-bound
_WEIGHT_MIN = 0.15          # a weight below this doesn't count toward "reach" (noise)
_MIN_REACH_FRAC = 0.03      # ignore reaches under this fraction of the bbox diagonal
_SEAM_ROUND = 1e-4


def _chunks(data: bytes):
    """(json_offset, json_len, bin_offset, bin_len) of a GLB, or None."""
    if data[:4] != b"glTF" or len(data) < 20:
        return None
    off, jc, bc = 12, None, None
    while off + 8 <= len(data):
        clen, ctype = struct.unpack_from("<I4s", data, off)
        start = off + 8
        if ctype == b"JSON":
            jc = (start, clen)
        elif ctype == b"BIN\x00":
            bc = (start, clen)
        off = start + clen
    if jc is None or bc is None:
        return None
    return jc[0], jc[1], bc[0], bc[1]


class _Bin:
    """Strided read/write of glTF accessors over the (mutable) BIN chunk."""

    def __init__(self, gltf, bin_ba):
        self.gltf = gltf
        self.raw = np.frombuffer(bin_ba, dtype=np.uint8)   # writable (bytearray backing)

    def _meta(self, idx):
        acc = self.gltf["accessors"][idx]
        bv = self.gltf["bufferViews"][acc["bufferView"]]
        dt = np.dtype(_CT[acc["componentType"]])
        nc = _NC[acc["type"]]
        n = acc["count"]
        off = bv.get("byteOffset", 0) + acc.get("byteOffset", 0)
        stride = bv.get("byteStride") or dt.itemsize * nc
        return off, stride, dt, nc, n

    def read(self, idx):
        off, stride, dt, nc, n = self._meta(idx)
        view = np.lib.stride_tricks.as_strided(
            self.raw[off:], shape=(n, nc, dt.itemsize), strides=(stride, dt.itemsize, 1))
        return view.copy().reshape(n, nc * dt.itemsize).view(dt), idx

    def write(self, idx, arr):
        off, stride, dt, nc, n = self._meta(idx)
        b = np.ascontiguousarray(arr.astype(dt)).view(np.uint8).reshape(n, nc, dt.itemsize)
        w = np.lib.stride_tricks.as_strided(
            self.raw[off:], shape=(n, nc, dt.itemsize), strides=(stride, dt.itemsize, 1))
        w[...] = b


def _bone_geometry(gltf, io, skin):
    """(seg_a, seg_b) world-space bone segments for a skin, one row per joint."""
    joints = skin["joints"]
    J = len(joints)
    ibm, _ = io.read(skin["inverseBindMatrices"])            # (J, 16) column-major
    pos = np.empty((J, 3), dtype=np.float64)
    for j in range(J):
        world = np.linalg.inv(ibm[j].reshape(4, 4).T.astype(np.float64))  # col-major → M
        pos[j] = world[:3, 3]
    joint_of_node = {node: j for j, node in enumerate(joints)}
    seg_a = pos.copy()
    seg_b = pos.copy()
    for j, node in enumerate(joints):
        for ch in (gltf["nodes"][node].get("children") or []):
            if ch in joint_of_node:                          # first child that is a joint
                seg_b[j] = pos[joint_of_node[ch]]
                break
    return seg_a.astype(np.float32), seg_b.astype(np.float32)


def _seg_dist(pos, a, b, ab, ab_len2):
    """Distance from every vertex (V,3) to one segment a→b."""
    if ab_len2 < 1e-12:
        return np.linalg.norm(pos - a, axis=1)
    t = np.clip((pos - a) @ ab / ab_len2, 0.0, 1.0)
    return np.linalg.norm(pos - (a + t[:, None] * ab), axis=1)


def _bone_compat(gltf, skin):
    """(J,J) bool: bones a,b are 'compatible' iff one is an ancestor of the other in the
    skeleton — i.e. they lie on a single root→leaf chain (Hips→UpLeg→Leg→Foot). Sibling
    limbs (LeftUpLeg vs RightUpLeg) are INCOMPATIBLE: a vertex weighted to both stretches
    into a ring/web when they animate apart, which no geometric test near the crotch can
    see (both are close there). The tree is the ground truth for that."""
    joints = skin["joints"]
    J = len(joints)
    pos_in = {node: i for i, node in enumerate(joints)}
    parent = [-1] * J
    for i, node in enumerate(joints):
        for ch in (gltf["nodes"][node].get("children") or []):
            if ch in pos_in:
                parent[pos_in[ch]] = i
    anc = [set() for _ in range(J)]
    for i in range(J):
        x = i
        seen = 0
        while x != -1 and seen <= J:                 # seen guard: never loop on a malformed skeleton
            anc[i].add(x)
            x = parent[x]
            seen += 1
    compat = np.zeros((J, J), dtype=bool)
    for a in range(J):
        for b in range(J):
            compat[a, b] = (a in anc[b]) or (b in anc[a])
    return compat


def _repair_weights(jnt, wgt, dist, near, compat, scale):
    """Fix mis-bound vertices locally, in place. Two defects, one pass:
      • drag/spike — a weight on a bone far from the vertex (> ratio x nearest): dropped.
      • ring/web — weights spanning two incompatible bones (sibling limbs): the weaker
        branch is dropped, keeping a single ancestor chain (greedy, weight-descending).
    A vertex left empty (all its weight was on wrong-and-far bones) is pinned rigidly to
    the single nearest bone — safe, it cannot spike. Returns the changed-vertex mask."""
    V = jnt.shape[0]
    rows = np.arange(V)
    wj = dist[rows[:, None], jnt]                     # (V,4) segment distance to each carried bone
    far = ((wgt > 1e-6)                               # a real weight on a bone this vertex is nowhere near:
           & (wj > _MISWEIGHT_RATIO * np.maximum(near[:, None], 1e-9))   # a long-range drag → drop it (at-
           & (wj > _MIN_REACH_FRAC * scale))          # joint blends stay: there both bones are close, not far
    w = np.where(far, 0.0, wgt)                       # drop weights on far bones
    order = np.argsort(-w, axis=1)                    # process weights high→low
    js = np.take_along_axis(jnt, order, axis=1)
    ws = np.take_along_axis(w, order, axis=1)
    keep = np.zeros((V, 4), dtype=bool)
    keep[:, 0] = ws[:, 0] > 0
    for i in range(1, 4):                             # keep slot i iff compatible with every kept slot < i
        comp = ws[:, i] > 0
        for k in range(i):
            comp &= (~keep[:, k]) | compat[js[:, i], js[:, k]]
        keep[:, i] = comp
    dropped = (ws > 0) & ~keep
    ws = np.where(keep, ws, 0.0)
    tot = ws.sum(axis=1)
    empty = tot <= 1e-8                               # nothing left → rigid nearest bone
    if empty.any():
        nn = np.argmin(dist[empty], axis=1)
        js[empty] = 0; ws[empty] = 0.0
        js[empty, 0] = nn; ws[empty, 0] = 1.0
        tot = ws.sum(axis=1)
    ws = ws / np.maximum(tot[:, None], 1e-9)
    applied = far.any(axis=1) | dropped.any(axis=1) | empty      # any weight actually moved → write it
    jnt[applied] = js[applied]
    wgt[applied] = ws[applied].astype(np.float32)
    # "corrected" counts only MEANINGFUL repairs — a significant weight dropped for being
    # far, a cross-branch weight removed, or a vertex re-pinned — not the tiny long-range
    # falloff trims that also happen (they barely change the vertex).
    meaningful = (far & (wgt >= _WEIGHT_MIN)).any(axis=1) | dropped.any(axis=1) | empty
    return meaningful


def _unify_seams(pos, jnt, wgt):
    """Vertices at the same rounded position get one shared weight set (top-4,
    normalized). Returns the number of vertices whose weights actually changed."""
    key = np.round(pos / _SEAM_ROUND).astype(np.int64)
    _, inv, counts = np.unique(key, axis=0, return_inverse=True, return_counts=True)
    inv = inv.reshape(-1)
    changed = 0
    for gid in np.where(counts > 1)[0]:
        members = np.where(inv == gid)[0]
        acc = {}
        for v in members:
            for k in range(4):
                w = float(wgt[v, k])
                if w > 0:
                    acc[int(jnt[v, k])] = acc.get(int(jnt[v, k]), 0.0) + w
        top = sorted(acc.items(), key=lambda x: -x[1])[:4]
        s = sum(w for _, w in top) or 1.0
        nj = np.zeros(4, dtype=jnt.dtype)
        nw = np.zeros(4, dtype=np.float32)
        for i, (bj, w) in enumerate(top):
            nj[i], nw[i] = bj, w / s
        for v in members:
            if not (np.array_equal(jnt[v], nj) and np.allclose(wgt[v], nw, atol=1e-6)):
                changed += 1
            jnt[v] = nj
            wgt[v] = nw
    return changed


def repair(data: bytes):
    """Return (new_glb_bytes, stats). stats: {corrected, seams, vertices, skins}.
    On any parse problem returns the input unchanged with reason in stats."""
    ch = _chunks(data)
    if ch is None:
        return data, {"corrected": 0, "reason": "not a GLB"}
    jo, jl, bo, bl = ch
    try:
        gltf = json.loads(data[jo:jo + jl])
        bin_ba = bytearray(data[bo:bo + bl])
        io = _Bin(gltf, bin_ba)
        skins = gltf.get("skins") or []
        if not skins:
            return data, {"corrected": 0, "reason": "no skin"}
        corrected = seams = verts = 0
        # map: which skin does each skinned mesh use (node.mesh + node.skin)
        mesh_skin = {}
        for node in gltf.get("nodes", []):
            if node.get("mesh") is not None and node.get("skin") is not None:
                mesh_skin[node["mesh"]] = node["skin"]
        seg_cache = {}
        for mi, mesh in enumerate(gltf.get("meshes", [])):
            skin_idx = mesh_skin.get(mi)
            if skin_idx is None:
                continue
            if skin_idx not in seg_cache:
                seg_cache[skin_idx] = (_bone_geometry(gltf, io, skins[skin_idx]),
                                       _bone_compat(gltf, skins[skin_idx]))
            (seg_a, seg_b), compat = seg_cache[skin_idx]
            J = seg_a.shape[0]
            ab = seg_b - seg_a
            ab_len2 = np.einsum("ij,ij->i", ab, ab)
            for prim in mesh["primitives"]:
                at = prim.get("attributes") or {}
                if not ("POSITION" in at and "JOINTS_0" in at and "WEIGHTS_0" in at):
                    continue
                pos, _ = io.read(at["POSITION"])
                pos = pos.astype(np.float32)
                jnt, ji = io.read(at["JOINTS_0"])
                wgt, wi = io.read(at["WEIGHTS_0"])
                jnt = jnt.astype(np.int32)
                wgt = wgt.astype(np.float32)
                V = pos.shape[0]
                verts += V
                dist = np.empty((V, J), dtype=np.float32)
                for j in range(J):
                    dist[:, j] = _seg_dist(pos, seg_a[j], seg_b[j], ab[j], ab_len2[j])
                near_dist = dist.min(axis=1)
                scale = float(np.linalg.norm(pos.max(axis=0) - pos.min(axis=0)))
                # Unify UV-seam twins FIRST, then repair — so the chain/branch enforcement
                # is the LAST word and a crotch-midline seam can't merge the two legs back
                # together. Fix drag/spike (weight on a far bone) AND ring/web (weights
                # across two sibling limbs) in one local pass.
                seams += _unify_seams(pos, jnt, wgt)
                changed = _repair_weights(jnt, wgt, dist, near_dist, compat, scale)
                corrected += int(changed.sum())
                # write back (clip joint indices to the accessor's integer type)
                io.write(ji, jnt)
                io.write(wi, wgt)
        new = data[:bo] + bytes(bin_ba) + data[bo + bl:]
        return new, {"corrected": corrected, "seams": seams, "vertices": verts,
                     "skins": len(skins)}
    except Exception as e:
        return data, {"corrected": 0, "reason": f"skinfix error: {type(e).__name__}: {e}"}

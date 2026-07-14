"""Skinning-weight repair for a rigged GLB, run in the gateway before delivery.

Auto-riggers bind some vertices to wrong bones (a foot vertex to the head bone, a
sleeve vertex to the spine). It's invisible in bind pose but the mesh shatters when
animated. The error rate rises with mesh resolution (Trellis 56-59k verts: none;
Pixal3D Bianca 287k verts: >20k). Fixing it client-side would cost millions of
distance ops on every page load per character; doing it once here is cheap.

Pure numpy on the glTF data — no 3D engine. Only JOINTS_0/WEIGHTS_0 change; a healthy
model gets 0 corrections, so it is safe to always run.

Method (verified client-side):
  1. Bone segments: from each bone's world position (inverse bind matrix) to its
     first child bone; childless bones stay a point.
  2. Detect: per vertex, distance to every segment. The highest-weight bone is
     "dominant"; if its segment is >~2.5x farther than the nearest, the vertex is
     mis-bound.
  3. Re-weight the mis-bound: the 3 nearest segments, inverse-distance, normalized.
  4. Seam unification: vertices at the same position (rounded 1e-4) must share
     weights, else the UV seams tear — sum the group's weights, keep the top 4,
     normalize.
"""
import json
import struct

import numpy as np

_CT = {5120: "<i1", 5121: "<u1", 5122: "<i2", 5123: "<u2", 5125: "<u4", 5126: "<f4"}
_NC = {"SCALAR": 1, "VEC2": 2, "VEC3": 3, "VEC4": 4, "MAT2": 4, "MAT3": 9, "MAT4": 16}
_MISWEIGHT_RATIO = 2.5
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
                seg_cache[skin_idx] = _bone_geometry(gltf, io, skins[skin_idx])
            seg_a, seg_b = seg_cache[skin_idx]
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
                dom_local = np.argmax(wgt, axis=1)
                dom_joint = jnt[np.arange(V), dom_local]
                dom_dist = dist[np.arange(V), dom_joint]
                near_dist = dist.min(axis=1)
                mis = dom_dist > _MISWEIGHT_RATIO * np.maximum(near_dist, 1e-9)
                idx = np.where(mis)[0]
                if len(idx):
                    order = np.argsort(dist[idx], axis=1)[:, :3]        # 3 nearest joints
                    d3 = np.take_along_axis(dist[idx], order, axis=1)
                    inv = 1.0 / np.maximum(d3, 1e-9)
                    w3 = inv / inv.sum(axis=1, keepdims=True)
                    nj = np.zeros((len(idx), 4), dtype=jnt.dtype)
                    nw = np.zeros((len(idx), 4), dtype=np.float32)
                    nj[:, :3] = order
                    nw[:, :3] = w3
                    jnt[idx] = nj
                    wgt[idx] = nw
                corrected += int(len(idx))
                seams += _unify_seams(pos, jnt, wgt)
                # write back (clip joint indices to the accessor's integer type)
                io.write(ji, jnt)
                io.write(wi, wgt)
        new = data[:bo] + bytes(bin_ba) + data[bo + bl:]
        return new, {"corrected": corrected, "seams": seams, "vertices": verts,
                     "skins": len(skins)}
    except Exception as e:
        return data, {"corrected": 0, "reason": f"skinfix error: {type(e).__name__}: {e}"}

"""Generation job store — Phase 1 of the multimodal-gateway plan.

Self-contained and dependency-free (stdlib `sqlite3` WAL + plain disk blobs),
in the spirit of stats.py. Holds the lifecycle and results of image/video/audio
generation jobs so a result stays retrievable by id for a while (TTL) even after
a synchronous caller has its inline copy.

Layout:
- metadata  → SQLite `jobs` table (status, timing, owner, result manifest)
- artifacts → `<blob_dir>/<job_id>/<n><ext>` on disk (never base64 in the DB)

A job moves queued → running → done | failed, then is pruned once it is older
than its `ttl_s`. `owner` is carried now (default "default") so Phase-3 multi-user
job-ownership checks slot in without a migration.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
import time
import uuid
from contextlib import contextmanager
from typing import Optional

logger = logging.getLogger(__name__)

_DB_PATH = "jobs.db"
_BLOB_DIR = "jobs"
_DEFAULT_TTL = 86400
_active = False

_EXT_BY_MIME = {
    "image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp",
    "image/gif": ".gif", "video/mp4": ".mp4", "video/webm": ".webm",
    "audio/wav": ".wav", "audio/mpeg": ".mp3", "audio/flac": ".flac",
}


def init(db_path: str = "jobs.db", blob_dir: str = "jobs", default_ttl_s: int = 86400) -> None:
    global _DB_PATH, _BLOB_DIR, _DEFAULT_TTL, _active
    _DB_PATH, _BLOB_DIR, _DEFAULT_TTL, _active = db_path, blob_dir, default_ttl_s, True
    os.makedirs(_BLOB_DIR, exist_ok=True)
    with _conn() as c:
        c.execute("PRAGMA journal_mode=WAL")   # persistent DB property — set once here
        c.execute("""
            CREATE TABLE IF NOT EXISTS jobs (
                id           TEXT PRIMARY KEY,
                created      INTEGER NOT NULL,
                updated      INTEGER NOT NULL,
                status       TEXT NOT NULL,
                task         TEXT,
                alias        TEXT,
                backend      TEXT,
                owner        TEXT,
                ttl_s        INTEGER NOT NULL,
                error        TEXT,
                result_count INTEGER NOT NULL DEFAULT 0,
                results_json TEXT,
                meta_json    TEXT,
                stage        TEXT
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS jobs_created ON jobs(created)")
        cols = {r[1] for r in c.execute("PRAGMA table_info(jobs)").fetchall()}
        if "stage" not in cols:                # migrate existing DBs (multi-stage progress, e.g. "1/2")
            c.execute("ALTER TABLE jobs ADD COLUMN stage TEXT")
    logger.info(f"jobs: store at {_DB_PATH}, blobs in {_BLOB_DIR}/ (default ttl {_DEFAULT_TTL}s)")
    n = reconcile_orphans()
    if n:
        logger.info(f"jobs: reconciled {n} orphaned running/queued job(s) → failed (process restart)")


@contextmanager
def _conn():
    conn = sqlite3.connect(_DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def new_id() -> str:
    return uuid.uuid4().hex


def is_active() -> bool:
    return _active


# Task types that live in the job store but are NOT media generations: parked-chat
# results and background /v1/responses jobs. Media views filter them out.
_NON_MEDIA_TASKS = ("chat", "response")
_MEDIA_FLT = f" WHERE task NOT IN ({','.join('?' * len(_NON_MEDIA_TASKS))})"


def counts(media_only: bool = False) -> dict:
    """Job count per status, for the dashboard. `media_only` drops chat/response rows."""
    if not _active:
        return {}
    flt, args = (_MEDIA_FLT, _NON_MEDIA_TASKS) if media_only else ("", ())
    with _conn() as c:
        rows = c.execute(f"SELECT status, COUNT(*) FROM jobs{flt} GROUP BY status", args).fetchall()
    return {r[0]: r[1] for r in rows}


def count_by_backend_since(ts: int) -> dict:
    """Jobs created per backend since `ts` — the dashboard's per-backend request
    rate for image-generation backends. Empty if the job store is off."""
    if not _active:
        return {}
    with _conn() as c:
        rows = c.execute(
            "SELECT backend, COUNT(*) FROM jobs WHERE created > ? GROUP BY backend", (ts,)).fetchall()
    return {r[0]: r[1] for r in rows if r[0]}


def recent(limit: int = 20, media_only: bool = False, owner: Optional[str] = None) -> list:
    """Most recent jobs (metadata only), newest first. `media_only` drops the
    chat/response rows (those live under Statistic / the Responses API);
    `owner` narrows to one job owner (the Media Jobs user filter)."""
    if not _active:
        return []
    conds, args = [], []
    if media_only:
        conds.append(f"task NOT IN ({','.join('?' * len(_NON_MEDIA_TASKS))})")
        args += _NON_MEDIA_TASKS
    if owner is not None:
        conds.append("owner = ?")
        args.append(owner)
    flt = f" WHERE {' AND '.join(conds)}" if conds else ""
    with _conn() as c:
        rows = c.execute(
            f"SELECT id, created, updated, status, task, alias, backend, owner, result_count, error, stage "
            f"FROM jobs{flt} ORDER BY created DESC LIMIT ?", (*args, limit)).fetchall()
    return [dict(r) for r in rows]


def owners(media_only: bool = True) -> list:
    """Distinct job owners, alphabetical — the Media Jobs user-picker options."""
    if not _active:
        return []
    flt, args = (_MEDIA_FLT, _NON_MEDIA_TASKS) if media_only else ("", ())
    with _conn() as c:
        rows = c.execute(f"SELECT DISTINCT owner FROM jobs{flt} ORDER BY owner", args).fetchall()
    return [r[0] for r in rows if r[0]]


def median_duration(alias: str, backend: Optional[str] = None, limit: int = 10) -> Optional[float]:
    """Median runtime (s) of the last `limit` DONE jobs of an alias (optionally
    narrowed to one backend) — the basis for the job view's progress/ETA estimate."""
    if not _active:
        return None
    q = "SELECT updated - created FROM jobs WHERE status = 'done' AND alias = ?"
    args: list = [alias]
    if backend:
        q += " AND backend = ?"
        args.append(backend)
    q += " ORDER BY created DESC LIMIT ?"
    args.append(limit)
    with _conn() as c:
        vals = sorted(r[0] for r in c.execute(q, args) if r[0] is not None and r[0] >= 0)
    if not vals:
        return None
    n = len(vals)
    return float(vals[n // 2] if n % 2 else (vals[n // 2 - 1] + vals[n // 2]) / 2)


def neighbors(job_id: str, media_only: bool = True) -> tuple:
    """(newer_id, older_id) around a job in the media list's created-DESC order
    (rowid tiebreak) — drives the detail page's prev/next navigation. None at the
    list ends or for an unknown id."""
    if not _active:
        return None, None
    flt = f" AND task NOT IN ({','.join('?' * len(_NON_MEDIA_TASKS))})" if media_only else ""
    args = _NON_MEDIA_TASKS if media_only else ()
    with _conn() as c:
        row = c.execute("SELECT created, rowid AS rid FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if row is None:
            return None, None
        cr, rid = row["created"], row["rid"]
        newer = c.execute(
            f"SELECT id FROM jobs WHERE (created > ? OR (created = ? AND rowid > ?)){flt} "
            f"ORDER BY created ASC, rowid ASC LIMIT 1", (cr, cr, rid, *args)).fetchone()
        older = c.execute(
            f"SELECT id FROM jobs WHERE (created < ? OR (created = ? AND rowid < ?)){flt} "
            f"ORDER BY created DESC, rowid DESC LIMIT 1", (cr, cr, rid, *args)).fetchone()
    return (newer["id"] if newer else None, older["id"] if older else None)


def create(task: str, alias: str, backend: str, *,
           owner: str = "default", ttl_s: Optional[int] = None,
           job_id: Optional[str] = None) -> str:
    """Insert a queued job and return its id."""
    jid = job_id or new_id()
    now = int(time.time())
    ttl = ttl_s if (isinstance(ttl_s, int) and ttl_s > 0) else _DEFAULT_TTL
    with _conn() as c:
        c.execute(
            "INSERT INTO jobs (id, created, updated, status, task, alias, backend, owner, ttl_s) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (jid, now, now, "queued", task, alias, backend, owner, ttl),
        )
    return jid


def set_status(job_id: str, status: str) -> None:
    with _conn() as c:
        c.execute("UPDATE jobs SET status=?, updated=? WHERE id=?",
                  (status, int(time.time()), job_id))


def set_stage(job_id: str, stage: Optional[str]) -> None:
    """Set a job's sub-stage label (e.g. "1/2" for a multi-stage chain), shown next
    to `running` in the UI. Cleared (None) when the job leaves the running state."""
    with _conn() as c:
        c.execute("UPDATE jobs SET stage=?, updated=? WHERE id=?",
                  (stage, int(time.time()), job_id))


def fail(job_id: str, error: str) -> None:
    with _conn() as c:
        c.execute("UPDATE jobs SET status='failed', error=?, stage=NULL, updated=? WHERE id=?",
                  (str(error), int(time.time()), job_id))


def complete(job_id: str, blobs, meta: Optional[dict] = None) -> list[dict]:
    """Write artifact bytes to disk and mark the job done. Returns the result
    manifest (one entry per blob: n, mime, kind, filename).

    Re-points the job's `backend` column to where it actually ran (`meta["backend"]`)
    — after a failover that differs from the backend recorded at create() time."""
    job_dir = os.path.join(_BLOB_DIR, job_id)
    os.makedirs(job_dir, exist_ok=True)
    manifest = []
    for n, blob in enumerate(blobs):
        # On-disk name stays index-based (stable, path-safe) but takes the REAL
        # extension when the source named the artifact — so a Trellis .fbx lands
        # as 0.fbx, not 0.bin. `name` carries the original for display/download.
        orig = getattr(blob, "name", None)
        oext = os.path.splitext(orig)[1].lower() if orig else ""
        ext = oext or _EXT_BY_MIME.get(blob.mime, ".bin")
        fname = f"{n}{ext}"
        with open(os.path.join(job_dir, fname), "wb") as f:
            f.write(blob.data)
        entry = {"n": n, "mime": blob.mime, "kind": blob.kind, "filename": fname,
                 "sha256": hashlib.sha256(blob.data).hexdigest()}
        if orig:
            entry["name"] = orig
        manifest.append(entry)
    with _conn() as c:                              # one connection: read meta + update
        meta = {**_read_meta(c, job_id), **(meta or {})}   # keep inputs persisted at create time
        _mark_done(c, job_id, meta, manifest)
    return manifest


def complete_json(job_id: str, payload, meta: Optional[dict] = None) -> None:
    """Mark a job done with an inline JSON result (e.g. a parked chat completion) —
    no disk blob; the payload is retrievable via get()['results'][0]."""
    with _conn() as c:
        _mark_done(c, job_id, meta or {}, [payload])


def _mark_done(c, job_id: str, meta: dict, results: list) -> None:
    """The one done-UPDATE. Re-points `backend` to where the job actually ran
    (meta['backend']) — after a failover that differs from the create() value."""
    sets = "status='done', error=NULL, stage=NULL, updated=?, result_count=?, results_json=?, meta_json=?"
    args = [int(time.time()), len(results), json.dumps(results), json.dumps(meta)]
    if meta.get("backend"):
        sets += ", backend=?"
        args.append(meta["backend"])
    c.execute(f"UPDATE jobs SET {sets} WHERE id=?", (*args, job_id))


def _read_meta(c, job_id: str) -> dict:
    """Current meta_json of a job, read over the caller's connection."""
    row = c.execute("SELECT meta_json FROM jobs WHERE id=?", (job_id,)).fetchone()
    if row and row[0]:
        try:
            return json.loads(row[0])
        except Exception as e:                       # corrupt meta is worth knowing about
            logger.warning(f"jobs: unreadable meta_json for job {job_id}: {e}")
    return {}


def _img_mime(data: bytes) -> str:
    """Sniff an image's mime from its magic bytes (reference uploads carry no name)."""
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:2] == b"\xff\xd8":
        return "image/jpeg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[:4] == b"GIF8":
        return "image/gif"
    return "image/png"


def set_inputs(job_id: str, inputs: dict, ref_blobs: Optional[list] = None) -> None:
    """Persist a job's request inputs for later inspection (within TTL): the prompt /
    negative_prompt / params inline in `meta.inputs`, reference images as on-disk blobs
    (`in_<n><ext>`) listed in `meta.input_images`. Merges into existing meta so a later
    complete() keeps them."""
    job_dir = os.path.join(_BLOB_DIR, job_id)
    os.makedirs(job_dir, exist_ok=True)
    manifest = []
    for n, item in enumerate(ref_blobs or []):
        slot, data = item
        if not data:
            continue
        mime = _img_mime(bytes(data))
        fname = f"in_{n}{_EXT_BY_MIME.get(mime, '.bin')}"
        with open(os.path.join(job_dir, fname), "wb") as f:
            f.write(bytes(data))
        manifest.append({"n": n, "slot": slot, "mime": mime, "filename": fname})
    with _conn() as c:                              # one connection: read meta + update
        meta = _read_meta(c, job_id)
        meta["inputs"] = inputs
        meta["input_images"] = manifest
        c.execute("UPDATE jobs SET meta_json=? WHERE id=?", (json.dumps(meta), job_id))


def _manifest_path(job_id: str, n: int, column: str, key: Optional[str]) -> Optional[tuple[str, str]]:
    """(filesystem path, mime) for entry `n` of one manifest column — reads just that
    JSON column (artifact-serving hot path) instead of a full get() with both parses."""
    if not _active:
        return None
    with _conn() as c:
        row = c.execute(f"SELECT {column} FROM jobs WHERE id=?", (job_id,)).fetchone()
    if not row or not row[0]:
        return None
    try:
        entries = json.loads(row[0])
    except Exception:
        return None
    if key is not None:
        entries = (entries or {}).get(key)
    for r in entries or []:
        if isinstance(r, dict) and r.get("n") == n and r.get("filename"):
            path = os.path.join(_BLOB_DIR, job_id, r["filename"])
            if os.path.exists(path):
                return path, r.get("mime"), r.get("name")
    return None


def input_path(job_id: str, n: int) -> Optional[tuple[str, str]]:
    """(filesystem path, mime) for input reference image `n` of a job, or None."""
    r = _manifest_path(job_id, n, "meta_json", "input_images")
    return (r[0], r[1]) if r else None


def get(job_id: str) -> Optional[dict]:
    if not _active:            # store off → clean "not found" instead of a 500 on a missing table
        return None
    with _conn() as c:
        row = c.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    if row is None:
        return None
    d = dict(row)
    d["results"] = json.loads(d.pop("results_json") or "[]")
    d["meta"] = json.loads(d.pop("meta_json") or "{}")
    d["expired"] = (d["created"] + d["ttl_s"]) < int(time.time())
    return d


def result_path(job_id: str, n: int) -> Optional[tuple]:
    """(filesystem path, mime, name) for result `n` of a job, or None if absent
    (also for inline-JSON results, e.g. a background response — no file). `name`
    is the original artifact filename (None when the source didn't provide one)."""
    return _manifest_path(job_id, n, "results_json", None)


def _delete(job_id: str) -> None:
    job_dir = os.path.join(_BLOB_DIR, job_id)
    if os.path.isdir(job_dir):
        for f in os.listdir(job_dir):
            try:
                os.remove(os.path.join(job_dir, f))
            except OSError:
                pass
        try:
            os.rmdir(job_dir)
        except OSError:
            pass
    with _conn() as c:
        c.execute("DELETE FROM jobs WHERE id=?", (job_id,))


def reconcile_orphans() -> int:
    """Mark jobs still in `running`/`queued` as failed. Called at startup: the async
    tasks that owned them died with the previous process, so they can never finish —
    otherwise they linger as forever-'running' rows (ticking duration) until TTL."""
    with _conn() as c:
        cur = c.execute(
            "UPDATE jobs SET status='failed', error='interrupted by process restart', updated=? "
            "WHERE status IN ('running','queued')", (int(time.time()),))
        return cur.rowcount


def prune_once() -> int:
    """Delete jobs (rows + blobs) past their ttl. Returns how many were removed."""
    now = int(time.time())
    with _conn() as c:
        ids = [r["id"] for r in c.execute(
            "SELECT id FROM jobs WHERE (created + ttl_s) < ?", (now,)).fetchall()]
    for jid in ids:
        _delete(jid)
    if ids:
        logger.info(f"jobs: pruned {len(ids)} expired job(s)")
    return len(ids)


async def prune_loop(interval_s: int = 3600) -> None:
    import asyncio
    while True:
        try:
            await asyncio.to_thread(prune_once)
        except Exception as e:
            logger.warning(f"jobs: prune failed: {e}")
        await asyncio.sleep(interval_s)

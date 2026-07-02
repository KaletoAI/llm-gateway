"""Generation-alias store — the writable source of truth for the UI.

Per the multimodal-gateway plan (§13b): the store is authoritative, YAML is a
one-way bootstrap. On init we import the config's `image_models` into an empty
store; thereafter the UI reads/writes here and the router resolves from here.
Self-contained stdlib sqlite3 (WAL), in the spirit of stats.py / jobs.py.

An alias maps to an ordered list of candidates, each:
    {backend, task, workflow, model?, mapping?}
where `mapping` is the explicit {param: {node, field}} binding table.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import sqlite3
import struct
import time
from contextlib import contextmanager
from typing import Optional

logger = logging.getLogger(__name__)

_DB_PATH = "store.db"
_active = False


# ── Secret encryption (stdlib-only, zero-dep) ────────────────────────────────────
# Backend api_keys are stored encrypted, not plaintext. Construction: per-secret
# random nonce → HMAC-SHA256 keystream (CTR-style) XOR plaintext, authenticated with
# a separate-subkey HMAC-SHA256 (encrypt-then-MAC). Master key from $GATEWAY_SECRET_KEY
# or an auto-generated `secret.key` (chmod 600) next to the DB. Protects the DB file
# at rest (leak/backup/commit); not against an attacker who already has the key file.

_ENC_PREFIX = "enc:v1:"
_MASTER_KEY: Optional[bytes] = None


def _master_key() -> bytes:
    global _MASTER_KEY
    if _MASTER_KEY is not None:
        return _MASTER_KEY
    env = os.environ.get("GATEWAY_SECRET_KEY")
    if env:
        _MASTER_KEY = hashlib.sha256(env.encode()).digest()
        return _MASTER_KEY
    path = os.path.join(os.path.dirname(_DB_PATH) or ".", "secret.key")
    try:
        with open(path, "rb") as fh:
            _MASTER_KEY = fh.read()
    except FileNotFoundError:
        _MASTER_KEY = os.urandom(32)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fh.write(_MASTER_KEY)
        os.chmod(path, 0o600)
        logger.info(f"store: generated secret key at {path} (chmod 600)")
    return _MASTER_KEY


def _subkey(label: bytes) -> bytes:
    return hmac.new(_master_key(), label, hashlib.sha256).digest()


def _keystream(enc_key: bytes, nonce: bytes, n: int) -> bytes:
    out = bytearray()
    i = 0
    while len(out) < n:
        out += hmac.new(enc_key, nonce + struct.pack(">I", i), hashlib.sha256).digest()
        i += 1
    return bytes(out[:n])


def encrypt_secret(plaintext: str) -> str:
    """Encrypt a secret string → 'enc:v1:<b64>'. Idempotent (already-encrypted and
    empty values pass through)."""
    if not plaintext or plaintext.startswith(_ENC_PREFIX):
        return plaintext
    enc_key, mac_key = _subkey(b"gw-enc-v1"), _subkey(b"gw-mac-v1")
    nonce = os.urandom(16)
    pt = plaintext.encode("utf-8")
    ct = bytes(a ^ b for a, b in zip(pt, _keystream(enc_key, nonce, len(pt))))
    tag = hmac.new(mac_key, nonce + ct, hashlib.sha256).digest()
    return _ENC_PREFIX + base64.b64encode(nonce + ct + tag).decode()


def decrypt_secret(token: str) -> str:
    """Inverse of encrypt_secret. Legacy plaintext (no prefix) passes through, so old
    rows keep working until re-saved. Returns '' on tamper/wrong-key."""
    if not token or not token.startswith(_ENC_PREFIX):
        return token
    try:
        raw = base64.b64decode(token[len(_ENC_PREFIX):])
        nonce, ct, tag = raw[:16], raw[16:-32], raw[-32:]
        enc_key, mac_key = _subkey(b"gw-enc-v1"), _subkey(b"gw-mac-v1")
        if not hmac.compare_digest(hmac.new(mac_key, nonce + ct, hashlib.sha256).digest(), tag):
            logger.warning("store: secret MAC mismatch (wrong key / tampered)")
            return ""
        pt = bytes(a ^ b for a, b in zip(ct, _keystream(enc_key, nonce, len(ct))))
        return pt.decode("utf-8")
    except Exception:
        logger.warning("store: secret decrypt failed")
        return ""


def init(db_path: str = "store.db") -> None:
    global _DB_PATH, _active
    _DB_PATH = db_path
    with _conn() as c:
        c.execute("PRAGMA journal_mode=WAL")   # persistent DB property — set once here
        c.execute("""
            CREATE TABLE IF NOT EXISTS gen_aliases (
                alias          TEXT PRIMARY KEY,
                candidates_json TEXT NOT NULL,
                updated        INTEGER NOT NULL
            )
        """)
        # Backends are keyed by (name, type), so an LLM and a ComfyUI backend may share
        # a name. Migrate the old single-PK (name) table if present.
        bcols = {r[1] for r in c.execute("PRAGMA table_info(backends)").fetchall()}
        if bcols and "type" not in bcols:
            old = c.execute("SELECT name, json, updated FROM backends").fetchall()
            c.execute("DROP TABLE backends")
            bcols = set()
        if not bcols:
            c.execute("""
                CREATE TABLE backends (
                    name    TEXT NOT NULL,
                    type    TEXT NOT NULL DEFAULT 'openai',
                    json    TEXT NOT NULL,
                    updated INTEGER NOT NULL,
                    PRIMARY KEY (name, type)
                )
            """)
            for r in (locals().get("old") or []):
                b = json.loads(r["json"])
                c.execute("INSERT INTO backends (name, type, json, updated) VALUES (?,?,?,?)",
                          (r["name"], b.get("type", "openai"), r["json"], r["updated"]))
        c.execute("""
            CREATE TABLE IF NOT EXISTS chat_aliases (
                alias      TEXT PRIMARY KEY,
                value_json TEXT NOT NULL,
                updated    INTEGER NOT NULL
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key        TEXT PRIMARY KEY,
                value_json TEXT NOT NULL,
                updated    INTEGER NOT NULL
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS users (
                name    TEXT PRIMARY KEY,
                json    TEXT NOT NULL,
                updated INTEGER NOT NULL
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS backend_models (
                bid         TEXT PRIMARY KEY,
                models_json TEXT NOT NULL,
                updated     INTEGER NOT NULL
            )
        """)
    _active = True
    logger.info(f"store: generation aliases at {_DB_PATH}")


def is_active() -> bool:
    return _active


@contextmanager
def _conn():
    conn = sqlite3.connect(_DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def bootstrap(image_models_cfg: dict) -> None:
    """Seed the store from config `image_models` for aliases not already present
    (one-way import; the store wins once populated)."""
    existing = set(list_aliases().keys())
    seeded = 0
    for alias, candidates in (image_models_cfg or {}).items():
        if alias not in existing:
            upsert(alias, candidates)
            seeded += 1
    if seeded:
        logger.info(f"store: bootstrapped {seeded} generation alias(es) from config")


def list_aliases() -> dict:
    with _conn() as c:
        rows = c.execute("SELECT alias, candidates_json FROM gen_aliases ORDER BY alias").fetchall()
    return {r["alias"]: json.loads(r["candidates_json"]) for r in rows}


def get(alias: str) -> Optional[list]:
    with _conn() as c:
        row = c.execute("SELECT candidates_json FROM gen_aliases WHERE alias=?", (alias,)).fetchone()
    return json.loads(row["candidates_json"]) if row else None


def upsert(alias: str, candidates: list) -> None:
    with _conn() as c:
        c.execute(
            "INSERT INTO gen_aliases (alias, candidates_json, updated) VALUES (?,?,?) "
            "ON CONFLICT(alias) DO UPDATE SET candidates_json=excluded.candidates_json, updated=excluded.updated",
            (alias, json.dumps(candidates), int(time.time())),
        )


def delete(alias: str) -> None:
    with _conn() as c:
        c.execute("DELETE FROM gen_aliases WHERE alias=?", (alias,))


# ── Backends (UI-added, additive to config) ─────────────────────────────────────
# api_key is stored encrypted (encrypt_secret) and decrypted on read, so callers
# always see/store plaintext while the DB never holds it.

def _decode_backend(raw: str) -> dict:
    b = json.loads(raw)
    if b.get("api_key"):
        b["api_key"] = decrypt_secret(b["api_key"])
    return b


def list_backends() -> list:
    with _conn() as c:
        rows = c.execute("SELECT json FROM backends ORDER BY name").fetchall()
    return [_decode_backend(r["json"]) for r in rows]


def get_backend(name: str, type: str = "openai") -> Optional[dict]:
    with _conn() as c:
        row = c.execute("SELECT json FROM backends WHERE name=? AND type=?",
                        (name, type)).fetchone()
    return _decode_backend(row["json"]) if row else None


def upsert_backend(backend: dict) -> None:
    b = dict(backend)
    if b.get("api_key"):
        b["api_key"] = encrypt_secret(b["api_key"])      # never persist plaintext
    with _conn() as c:
        c.execute(
            "INSERT INTO backends (name, type, json, updated) VALUES (?,?,?,?) "
            "ON CONFLICT(name, type) DO UPDATE SET json=excluded.json, updated=excluded.updated",
            (b["name"], b.get("type", "openai"), json.dumps(b), int(time.time())),
        )


def delete_backend(name: str, type: str = "openai") -> None:
    with _conn() as c:
        c.execute("DELETE FROM backends WHERE name=? AND type=?", (name, type))


def rename_backend_references(old: str, new: str) -> int:
    """Re-point every alias that names `old` backend to `new` — generation-alias
    candidates and per-backend chat-alias entries. Returns how many aliases changed,
    so a UI rename doesn't leave dangling 'down' references."""
    changed = 0
    for alias, cands in list_aliases().items():
        hit = False
        for c in cands:
            if c.get("backend") == old:
                c["backend"], hit = new, True
        if hit:
            upsert(alias, cands)
            changed += 1
    for alias, value in list_chat_aliases().items():
        if isinstance(value, dict) and old in value:
            value[new] = value.pop(old)        # keep model/priority, swap the key
            upsert_chat_alias(alias, value)
            changed += 1
    return changed


# ── Server settings (UI-managed overrides of config.yaml) ────────────────────────
# Secret-valued keys (api_key) are encrypted at rest like backend keys.

_SECRET_SETTINGS = {"api_key"}


def save_backend_models(bid: str, models) -> None:
    """Persist a backend's discovered model ids so they survive a restart — lets the
    gateway still resolve a bare model id to its (now offline) backend, returning a
    truthful 503 instead of 403, even if the backend isn't reachable at startup."""
    with _conn() as c:
        c.execute(
            "INSERT INTO backend_models (bid, models_json, updated) VALUES (?,?,?) "
            "ON CONFLICT(bid) DO UPDATE SET models_json=excluded.models_json, updated=excluded.updated",
            (bid, json.dumps(sorted(models)), int(time.time())))


def load_backend_models() -> dict:
    """{bid: set(model_ids)} from each backend's last successful discovery."""
    if not _active:
        return {}
    with _conn() as c:
        rows = c.execute("SELECT bid, models_json FROM backend_models").fetchall()
    return {r["bid"]: set(json.loads(r["models_json"] or "[]")) for r in rows}


def get_settings() -> dict:
    with _conn() as c:
        rows = c.execute("SELECT key, value_json FROM settings").fetchall()
    out = {}
    for r in rows:
        v = json.loads(r["value_json"])
        if r["key"] in _SECRET_SETTINGS and isinstance(v, str):
            v = decrypt_secret(v)
        out[r["key"]] = v
    return out


def set_settings(values: dict) -> None:
    now = int(time.time())
    with _conn() as c:
        for k, v in values.items():
            if k in _SECRET_SETTINGS and isinstance(v, str) and v:
                v = encrypt_secret(v)
            c.execute(
                "INSERT INTO settings (key, value_json, updated) VALUES (?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json, updated=excluded.updated",
                (k, json.dumps(v), now),
            )


# ── IP aliases (friendly names for caller IPs in stats; auto reverse-DNS) ────────
# Stored as a single settings dict {ip: name}. An empty name means "auto-resolve
# was attempted but found no hostname" — kept so we don't retry every page load.

def get_ip_aliases() -> dict:
    return get_settings().get("ip_aliases") or {}


def save_ip_aliases(aliases: dict) -> None:
    set_settings({"ip_aliases": dict(aliases)})


def set_ip_alias(ip: str, name: str) -> None:
    a = get_ip_aliases()
    a[ip] = name
    save_ip_aliases(a)


def delete_ip_alias(ip: str) -> None:
    a = get_ip_aliases()
    if ip in a:
        del a[ip]
        save_ip_aliases(a)


# ── Per-alias sync-park time (seconds a call may wait for a free backend) ────────
# Stored as one settings dict {alias: seconds}. Absent → the global default; 0 → no
# parking for that alias (immediate 503 when all its backends are busy).

def get_alias_park() -> dict:
    return get_settings().get("alias_park") or {}


def set_alias_park(alias: str, park_s) -> None:
    """Set/clear an alias's park seconds. None/'' removes the override (→ global default)."""
    m = get_alias_park()
    if park_s in (None, ""):
        m.pop(alias, None)
    else:
        try:
            m[alias] = max(0, int(park_s))
        except (TypeError, ValueError):
            return
    set_settings({"alias_park": m})


def rename_alias_park(old: str, new: str) -> None:
    if old == new:
        return
    m = get_alias_park()
    if old in m:
        m[new] = m.pop(old)
        set_settings({"alias_park": m})


# ── Reasoning rules (normalized thinking toggle) ────────────────────────────────
# Ordered list of {match, backends[], adapter, param} — see reasoning.py. Stored as
# one settings entry; first matching rule (model-glob × backend-set) wins.

def get_reasoning_rules() -> list:
    v = get_settings().get("reasoning_rules")
    return v if isinstance(v, list) else []


def set_reasoning_rules(rules: list) -> None:
    set_settings({"reasoning_rules": list(rules or [])})


# ── Users (multi-user: api_key → identity, role, quota, model access) ────────────
# Each user: {name, api_key(enc), role, enabled, models[], quota_req_day, quota_cost_month}.
# api_key encrypted at rest; the empty `models` list means "all models allowed".

def _decode_user(raw: str) -> dict:
    u = json.loads(raw)
    if u.get("api_key"):
        u["api_key"] = decrypt_secret(u["api_key"])
    return u


def list_users() -> list:
    with _conn() as c:
        rows = c.execute("SELECT json FROM users ORDER BY name").fetchall()
    return [_decode_user(r["json"]) for r in rows]


def get_user(name: str) -> Optional[dict]:
    with _conn() as c:
        row = c.execute("SELECT json FROM users WHERE name=?", (name,)).fetchone()
    return _decode_user(row["json"]) if row else None


def upsert_user(user: dict) -> None:
    u = dict(user)
    if u.get("api_key"):
        u["api_key"] = encrypt_secret(u["api_key"])
    with _conn() as c:
        c.execute(
            "INSERT INTO users (name, json, updated) VALUES (?,?,?) "
            "ON CONFLICT(name) DO UPDATE SET json=excluded.json, updated=excluded.updated",
            (u["name"], json.dumps(u), int(time.time())),
        )


def delete_user(name: str) -> None:
    with _conn() as c:
        c.execute("DELETE FROM users WHERE name=?", (name,))


# ── Chat aliases (UI-added/overridden, merged over config `virtual_models`) ──────
# A value is either a model-id string (same model on every backend) or a per-backend
# dict {backend: model-id | {model, priority}} — exactly the config `virtual_models`
# shapes, so the merged result drops straight into the LLM router.

def list_chat_aliases() -> dict:
    with _conn() as c:
        rows = c.execute("SELECT alias, value_json FROM chat_aliases ORDER BY alias").fetchall()
    return {r["alias"]: json.loads(r["value_json"]) for r in rows}


def get_chat_alias(alias: str) -> Optional[object]:
    with _conn() as c:
        row = c.execute("SELECT value_json FROM chat_aliases WHERE alias=?", (alias,)).fetchone()
    return json.loads(row["value_json"]) if row else None


def upsert_chat_alias(alias: str, value) -> None:
    with _conn() as c:
        c.execute(
            "INSERT INTO chat_aliases (alias, value_json, updated) VALUES (?,?,?) "
            "ON CONFLICT(alias) DO UPDATE SET value_json=excluded.value_json, updated=excluded.updated",
            (alias, json.dumps(value), int(time.time())),
        )


def delete_chat_alias(alias: str) -> None:
    with _conn() as c:
        c.execute("DELETE FROM chat_aliases WHERE alias=?", (alias,))

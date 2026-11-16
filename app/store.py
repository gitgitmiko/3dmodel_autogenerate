"""Penyimpanan aset & job di SQLite (stdlib)."""

import json
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager

from .config import DB_PATH

_lock = threading.RLock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS assets (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  created REAL NOT NULL,
  status TEXT NOT NULL,          -- queued | processing | ready | error
  params TEXT NOT NULL DEFAULT '{}',
  stats TEXT NOT NULL DEFAULT '{}',
  rig_status TEXT NOT NULL DEFAULT 'none'  -- none | preparing | prepared | rigging | rigged | error
);
CREATE TABLE IF NOT EXISTS jobs (
  id TEXT PRIMARY KEY,
  asset_id TEXT NOT NULL,
  kind TEXT NOT NULL,            -- image_to_3d | rig_prepare | rig
  status TEXT NOT NULL,          -- queued | running | done | error
  progress REAL NOT NULL DEFAULT 0,
  message TEXT NOT NULL DEFAULT '',
  params TEXT NOT NULL DEFAULT '{}',
  result TEXT NOT NULL DEFAULT '{}',
  created REAL NOT NULL,
  started REAL,
  finished REAL
);
CREATE INDEX IF NOT EXISTS jobs_status ON jobs(status, created);
CREATE INDEX IF NOT EXISTS jobs_asset ON jobs(asset_id, created);
"""


@contextmanager
def db():
    with _lock:
        con = sqlite3.connect(DB_PATH)
        con.row_factory = sqlite3.Row
        try:
            yield con
            con.commit()
        finally:
            con.close()


def init():
    with db() as con:
        con.executescript(SCHEMA)
        # Job yang terputus karena restart dianggap gagal.
        con.execute(
            "UPDATE jobs SET status='error', message='Terhenti karena server restart', finished=? "
            "WHERE status='running'", (time.time(),))
        con.execute("UPDATE assets SET status='error' WHERE status='processing'")
        con.execute("UPDATE assets SET rig_status='error' WHERE rig_status IN ('preparing','rigging')")


def _row(r, json_fields=("params", "stats", "result")):
    if r is None:
        return None
    d = dict(r)
    for k in json_fields:
        if k in d and isinstance(d[k], str):
            d[k] = json.loads(d[k] or "{}")
    return d


def new_id() -> str:
    return uuid.uuid4().hex[:12]


def create_asset(name: str, params: dict) -> dict:
    aid = new_id()
    with db() as con:
        con.execute("INSERT INTO assets (id, name, created, status, params) VALUES (?,?,?,?,?)",
                    (aid, name, time.time(), "queued", json.dumps(params)))
    return get_asset(aid)


def get_asset(aid: str):
    with db() as con:
        return _row(con.execute("SELECT * FROM assets WHERE id=?", (aid,)).fetchone())


def list_assets():
    with db() as con:
        return [_row(r) for r in con.execute("SELECT * FROM assets ORDER BY created DESC").fetchall()]


def update_asset(aid: str, **fields):
    if not fields:
        return
    for k in ("params", "stats"):
        if k in fields and not isinstance(fields[k], str):
            fields[k] = json.dumps(fields[k])
    sets = ", ".join(f"{k}=?" for k in fields)
    with db() as con:
        con.execute(f"UPDATE assets SET {sets} WHERE id=?", (*fields.values(), aid))


def delete_asset(aid: str):
    with db() as con:
        con.execute("DELETE FROM jobs WHERE asset_id=?", (aid,))
        con.execute("DELETE FROM assets WHERE id=?", (aid,))


def create_job(asset_id: str, kind: str, params: dict) -> dict:
    jid = new_id()
    with db() as con:
        con.execute("INSERT INTO jobs (id, asset_id, kind, status, params, created, message) VALUES (?,?,?,?,?,?,?)",
                    (jid, asset_id, kind, "queued", json.dumps(params), time.time(), "Menunggu antrean"))
    return get_job(jid)


def get_job(jid: str):
    with db() as con:
        return _row(con.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone())


def jobs_for(asset_id: str):
    with db() as con:
        return [_row(r) for r in con.execute(
            "SELECT * FROM jobs WHERE asset_id=? ORDER BY created DESC", (asset_id,)).fetchall()]


def next_queued():
    with db() as con:
        return _row(con.execute("SELECT * FROM jobs WHERE status='queued' ORDER BY created LIMIT 1").fetchone())


def queue_position(jid: str) -> int:
    with db() as con:
        r = con.execute("SELECT created FROM jobs WHERE id=?", (jid,)).fetchone()
        if not r:
            return 0
        return con.execute("SELECT COUNT(*) FROM jobs WHERE status IN ('queued','running') AND created < ?",
                           (r["created"],)).fetchone()[0]


def update_job(jid: str, **fields):
    for k in ("params", "result"):
        if k in fields and not isinstance(fields[k], str):
            fields[k] = json.dumps(fields[k])
    sets = ", ".join(f"{k}=?" for k in fields)
    with db() as con:
        con.execute(f"UPDATE jobs SET {sets} WHERE id=?", (*fields.values(), jid))


def active_jobs():
    with db() as con:
        return [_row(r) for r in con.execute(
            "SELECT * FROM jobs WHERE status IN ('queued','running') ORDER BY created").fetchall()]

"""SQLite (WAL) is the controller's only state. Embedded on purpose: see SUBMISSION section on storage.

Every chunk's cursor and aggregate deltas commit in one transaction, so a kill at any point
leaves either all of a chunk or none of it. That is what gives exactly-once ingest.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS kv(k TEXT PRIMARY KEY, v TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS cursor(id INTEGER PRIMARY KEY CHECK(id=1), last_chunk INTEGER NOT NULL, events INTEGER NOT NULL);
INSERT OR IGNORE INTO cursor VALUES(1,-1,0);
CREATE TABLE IF NOT EXISTS segments(seg INTEGER PRIMARY KEY, lang TEXT, city_tier TEXT, age_band TEXT, value_band TEXT,
    UNIQUE(lang,city_tier,age_band,value_band));
CREATE TABLE IF NOT EXISTS leads(lead_id TEXT PRIMARY KEY, seg INTEGER NOT NULL) WITHOUT ROWID;
-- one row per arm x population cell x day x config version: the experiment evidence
CREATE TABLE IF NOT EXISTS arm_day(wf TEXT, day INTEGER, version INTEGER, cohort TEXT, seg INTEGER,
    welcome TEXT, voice TEXT, did TEXT, prompt TEXT,
    dials INTEGER, connected INTEGER, engaged INTEGER, halluc INTEGER,
    PRIMARY KEY(wf,day,version,cohort,seg,welcome,voice,did,prompt)) WITHOUT ROWID;
-- hourly view for incident detectors
CREATE TABLE IF NOT EXISTS hourly(wf TEXT, minute INTEGER, did TEXT, prompt TEXT, version INTEGER,
    dials INTEGER, connected INTEGER, engaged INTEGER, halluc INTEGER,
    PRIMARY KEY(wf,minute,did,prompt,version)) WITHOUT ROWID;
-- hour of day x population cell, for scheduling
CREATE TABLE IF NOT EXISTS hour_seg(wf TEXT, hour INTEGER, seg INTEGER, dials INTEGER, connected INTEGER,
    PRIMARY KEY(wf,hour,seg)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS inbox(event_id INTEGER PRIMARY KEY, body TEXT NOT NULL, done INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS wf_state(wf TEXT PRIMARY KEY, state TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS proposals(proposal_id TEXT PRIMARY KEY, wf TEXT NOT NULL, kind TEXT NOT NULL, route TEXT NOT NULL,
    status TEXT NOT NULL, base_version INTEGER NOT NULL, config_hash TEXT NOT NULL, payload TEXT NOT NULL,
    approval_id TEXT, applied_version INTEGER, reasons TEXT, created_minute INTEGER NOT NULL, updated_minute INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS proposals_wf ON proposals(wf,status);
-- the audit trail: everything needed to answer "why did this change?" from this store alone
CREATE TABLE IF NOT EXISTS decisions(id INTEGER PRIMARY KEY AUTOINCREMENT, minute INTEGER NOT NULL, ts TEXT NOT NULL, wf TEXT,
    kind TEXT NOT NULL, summary TEXT NOT NULL, evidence TEXT, proposal_id TEXT);
CREATE INDEX IF NOT EXISTS decisions_wf ON decisions(wf,minute);
CREATE INDEX IF NOT EXISTS decisions_pid ON decisions(proposal_id);
CREATE TABLE IF NOT EXISTS alerts(id INTEGER PRIMARY KEY AUTOINCREMENT, minute INTEGER, ts TEXT, severity TEXT, wf TEXT,
    message TEXT, action TEXT);
CREATE TABLE IF NOT EXISTS metrics(id INTEGER PRIMARY KEY AUTOINCREMENT, minute INTEGER, tick_ms INTEGER, ingest_ms INTEGER,
    decide_ms INTEGER, advance_ms INTEGER, events INTEGER, credits REAL);
"""
FEATURES = ("lang", "city_tier", "age_band", "value_band")


class Store:
    def __init__(self, path: str | Path):
        self.path = str(path)
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, isolation_level=None, timeout=60)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.executescript(SCHEMA)

    def kv_get(self, k: str, default=None):
        row = self.db.execute("SELECT v FROM kv WHERE k=?", (k,)).fetchone()
        return json.loads(row[0]) if row else default

    def kv_set(self, k: str, v) -> None:
        self.db.execute("INSERT INTO kv VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, json.dumps(v)))

    def wf_state(self, wf: str) -> dict | None:
        row = self.db.execute("SELECT state FROM wf_state WHERE wf=?", (wf,)).fetchone()
        return json.loads(row[0]) if row else None

    def save_wf_state(self, wf: str, state: dict) -> None:
        self.db.execute("INSERT INTO wf_state VALUES(?,?) ON CONFLICT(wf) DO UPDATE SET state=excluded.state",
                        (wf, json.dumps(state, sort_keys=True)))

    def decide(self, minute: int, ts: str, wf: str | None, kind: str, summary: str, evidence=None, proposal_id=None):
        self.db.execute("INSERT INTO decisions(minute,ts,wf,kind,summary,evidence,proposal_id) VALUES(?,?,?,?,?,?,?)",
                        (minute, ts, wf, kind, summary, json.dumps(evidence, sort_keys=True, default=str), proposal_id))

    def alert(self, minute: int, ts: str, severity: str, wf: str | None, message: str, action: str):
        self.db.execute("INSERT INTO alerts(minute,ts,severity,wf,message,action) VALUES(?,?,?,?,?,?)",
                        (minute, ts, severity, wf, message, action))
        print(f"ALERT[{severity}] {ts} {wf or '-'}: {message} -> {action}", flush=True)

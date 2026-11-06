"""C1: chunk ingest. Cursor and deltas commit together, so a replayed chunk cannot double count."""
from __future__ import annotations

import json
from collections import defaultdict
from datetime import date

from .store import FEATURES, Store


class Ingestor:
    def __init__(self, store: Store, client, origin_ts: str):
        self.s, self.c = store, client
        self._load_segments()
        self.lead_seg = dict(store.db.execute("SELECT lead_id,seg FROM leads"))
        # Day 0 is the day the scenario starts. History days are therefore negative.
        self.origin = date.fromisoformat(origin_ts[:10]).toordinal()
        self.day_cache: dict[str, int] = {}

    def _load_segments(self):
        self.seg_ids = {tuple(r[1:]): r[0] for r in self.s.db.execute(
            "SELECT seg,lang,city_tier,age_band,value_band FROM segments")}

    def _day(self, ts: str) -> int:
        d = ts[:10]
        v = self.day_cache.get(d)
        if v is None:
            v = self.day_cache[d] = date.fromisoformat(d).toordinal() - self.origin
        return v

    def _seg(self, f: dict) -> int:
        key = tuple(f[k] for k in FEATURES)
        seg = self.seg_ids.get(key)
        if seg is None:
            seg = self.seg_ids[key] = len(self.seg_ids) + 1
            self.s.db.execute("INSERT INTO segments VALUES(?,?,?,?,?)", (seg, *key))
        return seg

    def run(self) -> tuple[int, int]:
        """Ingest every chunk after the cursor, across history and live kinds, in chunk order."""
        last = self.s.db.execute("SELECT last_chunk FROM cursor").fetchone()[0]
        pending = sorted(self.c.chunks("history", last) + self.c.chunks("events", last), key=lambda c: c["chunk"])
        total = 0
        for ch in pending:
            total += self._one(ch["chunk"])
        return len(pending), total

    def _one(self, n: int) -> int:
        text = self.c.chunk_text(n)  # fetch before opening the write transaction
        arm = defaultdict(lambda: [0, 0, 0, 0])
        hourly = defaultdict(lambda: [0, 0, 0, 0])
        hseg = defaultdict(lambda: [0, 0])
        new_leads, inbox, calls = [], [], []
        count = 0
        db = self.s.db
        db.execute("BEGIN IMMEDIATE")
        try:
            for line in text.splitlines():
                if not line:
                    continue
                e = json.loads(line)
                count += 1
                t = e["type"]
                if t == "call":
                    seg = self.lead_seg.get(e["lead_id"], 0)
                    v = e["variant"]
                    conn = 1 if e["connected"] else 0
                    eng = 1 if e["engaged"] else 0
                    hal = 1 if (e.get("qa") or {}).get("hallucination_flag") else 0
                    ts = e["ts"]
                    day = self._day(ts)
                    hh = int(ts[11:13])
                    wf = e["workflow_id"]
                    a = arm[(wf, day, e["config_version"], e["cohort_id"], seg,
                             v["welcome_message"], v["voice"], v["caller_id"], v["prompt_version"])]
                    a[0] += 1; a[1] += conn; a[2] += eng; a[3] += hal
                    h = hourly[(wf, day * 1440 + hh * 60, v["caller_id"], v["prompt_version"], e["config_version"])]
                    h[0] += 1; h[1] += conn; h[2] += eng; h[3] += hal
                    g = hseg[(wf, hh, seg)]
                    g[0] += 1; g[1] += conn
                    if day >= 0:
                        calls.append((e["person_id"], ts, wf, e["config_version"], e["cohort_id"], v["welcome_message"],
                                      v["voice"], v["caller_id"], v["prompt_version"], e.get("disposition")))
                elif t == "lead":
                    seg = self._seg(e["features"])
                    self.lead_seg[e["lead_id"]] = seg
                    new_leads.append((e["lead_id"], seg))
                elif t in ("config_changed", "approval_decided"):
                    inbox.append((e["event_id"], line))
            db.executemany("INSERT OR IGNORE INTO leads VALUES(?,?)", new_leads)
            db.executemany(
                "INSERT INTO arm_day VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT DO UPDATE SET "
                "dials=dials+excluded.dials,connected=connected+excluded.connected,"
                "engaged=engaged+excluded.engaged,halluc=halluc+excluded.halluc",
                [(*k, *v) for k, v in arm.items()])
            db.executemany(
                "INSERT INTO hourly VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT DO UPDATE SET "
                "dials=dials+excluded.dials,connected=connected+excluded.connected,"
                "engaged=engaged+excluded.engaged,halluc=halluc+excluded.halluc",
                [(*k, *v) for k, v in hourly.items()])
            db.executemany(
                "INSERT INTO hour_seg VALUES(?,?,?,?,?) ON CONFLICT DO UPDATE SET "
                "dials=dials+excluded.dials,connected=connected+excluded.connected",
                [(*k, *v) for k, v in hseg.items()])
            db.executemany("INSERT INTO call_log VALUES(?,?,?,?,?,?,?,?,?,?)", calls)
            db.executemany("INSERT OR IGNORE INTO inbox(event_id,body) VALUES(?,?)", inbox)
            db.execute("UPDATE cursor SET last_chunk=?,events=events+?", (n, count))
            db.execute("COMMIT")
        except BaseException:
            db.execute("ROLLBACK")
            self._load_segments()
            raise
        return count

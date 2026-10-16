"""Interchangeable transactional research-state repositories."""
from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path
import sqlite3
from threading import RLock

from quant_rsi.domain.research import check
from quant_rsi.serialization.research import encode, decode


class InMemoryResearchRepository:
    def __init__(self):
        self._values, self._lock = {}, RLock()

    def create(self, campaign):
        with self._lock:
            check(campaign.campaign_id not in self._values, "conflict", "Campaign already exists.")
            self._values[campaign.campaign_id] = deepcopy(campaign)

    @contextmanager
    def transaction(self, campaign_id):
        with self._lock:
            check(campaign_id in self._values, "not_found", "Campaign not found.")
            campaign = deepcopy(self._values[campaign_id])
            yield campaign
            self._values[campaign_id] = deepcopy(campaign)


class SQLiteResearchRepository:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS research_campaigns (id TEXT PRIMARY KEY, document TEXT NOT NULL)")

    @contextmanager
    def _connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        try:
            db.execute("PRAGMA synchronous=FULL")
            with db:
                yield db
        finally:
            db.close()

    def create(self, campaign):
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            check(db.execute("SELECT id FROM research_campaigns WHERE id=?", (campaign.campaign_id,)).fetchone() is None,
                  "conflict", "Campaign already exists.")
            db.execute("INSERT INTO research_campaigns VALUES (?,?)", (campaign.campaign_id, encode(campaign)))

    @contextmanager
    def transaction(self, campaign_id):
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT document FROM research_campaigns WHERE id=?", (campaign_id,)).fetchone()
            check(row is not None, "not_found", "Campaign not found.")
            campaign = decode(row[0])
            yield campaign
            db.execute("UPDATE research_campaigns SET document=? WHERE id=?", (encode(campaign), campaign_id))

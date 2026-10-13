"""Stockage SQLite : dédoublonnage, historique, statuts du tracker."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .models import Lead, Status

SCHEMA = """
CREATE TABLE IF NOT EXISTS leads (
    id            TEXT PRIMARY KEY,
    first_seen    TEXT NOT NULL,
    source        TEXT NOT NULL,
    segment       TEXT NOT NULL,
    company       TEXT,
    company_key   TEXT,
    title         TEXT NOT NULL,
    url           TEXT,
    score         INTEGER,
    status        TEXT NOT NULL DEFAULT 'nouveau',
    status_note   TEXT DEFAULT '',
    status_at     TEXT,
    payload       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_leads_company ON leads(company_key);
CREATE INDEX IF NOT EXISTS idx_leads_status ON leads(status);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Store:
    def __init__(self, path: str | Path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)

    def close(self) -> None:
        self.conn.close()

    # --- dédoublonnage -------------------------------------------------
    def is_known(self, lead: Lead) -> bool:
        row = self.conn.execute("SELECT 1 FROM leads WHERE id = ?", (lead.fingerprint,)).fetchone()
        return row is not None

    def recently_contacted(self, company_key: str, cooldown_days: int) -> bool:
        """Vrai si l'entreprise a été contactée (ou a répondu) il y a moins de cooldown_days."""
        if not company_key:
            return False
        since = (datetime.now(timezone.utc) - timedelta(days=cooldown_days)).isoformat()
        row = self.conn.execute(
            "SELECT 1 FROM leads WHERE company_key = ? AND status IN (?, ?) AND status_at >= ?",
            (company_key, Status.CONTACTE.value, Status.REPONDU.value, since),
        ).fetchone()
        return row is not None

    # --- écriture ------------------------------------------------------
    def save(self, lead: Lead) -> None:
        self.conn.execute(
            """INSERT INTO leads (id, first_seen, source, segment, company, company_key, title, url,
                                  score, payload)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET score = excluded.score, payload = excluded.payload,
                                             company_key = excluded.company_key""",
            (
                lead.fingerprint, _now(), lead.source, lead.segment.value, lead.company,
                lead.company_key, lead.title, lead.url, lead.score,
                lead.model_dump_json(),
            ),
        )
        self.conn.commit()

    def set_status(self, lead_id: str, status: Status, note: str = "") -> bool:
        cur = self.conn.execute(
            "UPDATE leads SET status = ?, status_note = ?, status_at = ? WHERE id LIKE ?",
            (status.value, note, _now(), f"{lead_id}%"),
        )
        self.conn.commit()
        return cur.rowcount == 1

    # --- lecture -------------------------------------------------------
    def leads_since(self, since_iso: str, min_score: int = 0) -> list[dict]:
        rows = self.conn.execute(
            """SELECT * FROM leads WHERE first_seen >= ? AND COALESCE(score, 0) >= ?
               ORDER BY score DESC""",
            (since_iso, min_score),
        ).fetchall()
        return [dict(r) for r in rows]

    def all_leads(self) -> list[dict]:
        return [dict(r) for r in self.conn.execute("SELECT * FROM leads ORDER BY first_seen DESC")]

    def feedback_examples(self, limit: int = 12) -> list[dict]:
        """Décisions récentes d'Axel (rejets motivés, leads retenus) pour calibrer le scoring."""
        rows = self.conn.execute(
            """SELECT title, company, score, status, status_note FROM leads
               WHERE status IN (?, ?, ?, ?) ORDER BY status_at DESC LIMIT ?""",
            (Status.REJETE.value, Status.A_CONTACTER.value, Status.CONTACTE.value,
             Status.REPONDU.value, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    @staticmethod
    def lead_from_row(row: dict) -> Lead:
        return Lead.model_validate(json.loads(row["payload"]))

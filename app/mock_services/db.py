"""Histórico de eventos (claim_events) en SQLite. Lo ESCRIBE el servicio simulado; el agente solo lo LEE."""
import sqlite3
from datetime import datetime
from pathlib import Path

from app.mock_services.data import SEED_EVENTS

SCHEMA = """
CREATE TABLE IF NOT EXISTS claim_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    claim_id    TEXT NOT NULL,
    event_type  TEXT NOT NULL,
    event_date  TEXT NOT NULL,
    description TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_claim_events_claim ON claim_events(claim_id);
"""


def init_db(path: Path) -> None:
    """Crea la tabla y la rellena con datos de ejemplo si está vacía."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as conn:
        conn.executescript(SCHEMA)
        if conn.execute("SELECT COUNT(*) FROM claim_events").fetchone()[0] == 0:
            conn.executemany(
                "INSERT INTO claim_events (claim_id, event_type, event_date, description) VALUES (?, ?, ?, ?)", SEED_EVENTS
            )


def reset_db(path: Path) -> None:
    with sqlite3.connect(path) as conn:
        conn.executescript(SCHEMA)
        conn.execute("DELETE FROM claim_events")
    init_db(path)


def add_event(path: Path, claim_id: str, event_type: str, description: str) -> None:
    with sqlite3.connect(path) as conn:
        conn.execute(
            "INSERT INTO claim_events (claim_id, event_type, event_date, description) VALUES (?, ?, ?, ?)",
            (claim_id, event_type, datetime.now().strftime("%Y-%m-%d"), description),
        )

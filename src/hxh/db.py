from __future__ import annotations
import sqlite3
from pathlib import Path
from .config import DEFAULT_DB

SCHEMA = Path(__file__).with_name("schema.sql")
KEEP = {"review_decision", "sqlite_sequence"}   # human decisions survive pipeline re-runs


def connect(path: Path | str = DEFAULT_DB) -> sqlite3.Connection:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(path))
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON")
    return con


def reset_and_init(con: sqlite3.Connection) -> None:
    con.execute("PRAGMA foreign_keys=OFF")
    for (v,) in con.execute("SELECT name FROM sqlite_master WHERE type='view'").fetchall():
        con.execute(f"DROP VIEW IF EXISTS {v}")
    for (t,) in con.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall():
        if t not in KEEP:
            con.execute(f"DROP TABLE IF EXISTS {t}")
    con.executescript(SCHEMA.read_text(encoding="utf-8"))
    con.execute("PRAGMA foreign_keys=ON")
    con.commit()

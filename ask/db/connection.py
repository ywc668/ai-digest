"""Database connections for the Ask module.

Every connection auto-loads the sqlite-vec extension, so callers can use `vec0`
virtual tables and `vec_*` functions without any per-call ceremony.

The default database is the **shared** AI Digest SQLite file — Ask adds its own
(additive) tables to that same file and never opens a second database (see
ask/DESIGN.md §1, principle 1). The path is read from the existing project's
`config.yaml` (`state.db_file`), not hardcoded, so Ask always follows whatever
the AI Digest pipeline uses.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import sqlite_vec
import yaml

# ask/db/connection.py -> ask/db -> ask -> <repo root>
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_CONFIG_PATH = _PROJECT_ROOT / "config.yaml"
_DEFAULT_DB_FILE = "digest.db"


def _default_db_path() -> str:
    """Resolve AI Digest's configured DB path (config.yaml -> state.db_file).

    Mirrors how the existing code reads it (server.py / store.py). Relative paths
    resolve against the repo root so the cwd doesn't matter.
    """
    db_file = _DEFAULT_DB_FILE
    try:
        cfg = yaml.safe_load(_CONFIG_PATH.read_text()) or {}
        db_file = (cfg.get("state") or {}).get("db_file", _DEFAULT_DB_FILE)
    except FileNotFoundError:
        pass  # fall back to the default name, resolved below
    path = Path(db_file)
    return str(path if path.is_absolute() else _PROJECT_ROOT / path)


def get_db(db_path: str | None = None) -> sqlite3.Connection:
    """Open a sqlite3 connection with the sqlite-vec extension loaded.

    Args:
        db_path: Override the database file (e.g. a temp DB in tests, or
            ``":memory:"``). Defaults to AI Digest's shared database.

    Returns:
        An open ``sqlite3.Connection`` with ``row_factory = sqlite3.Row``,
        sqlite-vec available, and foreign keys enabled (the schema in
        ask/DESIGN.md §3 relies on ``ON DELETE CASCADE``).
    """
    path = db_path or _default_db_path()
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row

    # Load sqlite-vec, then re-disable extension loading — we only need this one,
    # and leaving it off is the safer default.
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)

    conn.execute("PRAGMA foreign_keys = ON")
    return conn

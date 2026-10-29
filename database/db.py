"""SQLite connection helper. Database files are not created until requested."""

import os
import sqlite3
from pathlib import Path
from contextlib import contextmanager
from typing import Iterator

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = Path(os.getenv("ABB_DATA_ROOT", PROJECT_ROOT / "data")).resolve()
GLOBAL_DB_PATH = Path(os.getenv("ABB_GLOBAL_DB", DATA_DIR / "global.db")).resolve()
ZPP7_DB_PATH = Path(os.getenv("ABB_ZPP7_DB", DATA_DIR / "zpp7_breakdown.db")).resolve()


@contextmanager
def connect(db_path: Path = GLOBAL_DB_PATH) -> Iterator[sqlite3.Connection]:
    """Yield a SQLite connection and commit/rollback its transaction safely."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()

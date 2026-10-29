"""
Processing lock for ABB Shortage Management System.

Ensures that concurrent report-processing jobs cannot run simultaneously
and corrupt shared staging or parser database state.
"""

from __future__ import annotations

import json
import logging
import os
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Generator

logger = logging.getLogger("abb_shortage.lock")

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_LOCK_FILE = ROOT / "data" / ".processing.lock"
STALE_LOCK_SECONDS = 900  # 15 minutes


def _is_pid_running(pid: int) -> bool:
    """Check if process with given PID is still running on Windows or Unix."""
    if pid <= 0:
        return False
    try:
        if os.name == "nt":
            import ctypes
            kernel32 = ctypes.windll.kernel32
            SYNCHRONIZE = 0x00100000
            process = kernel32.OpenProcess(SYNCHRONIZE, False, pid)
            if process != 0:
                kernel32.CloseHandle(process)
                return True
            return False
        else:
            os.kill(pid, 0)
            return True
    except Exception:
        return False


@contextmanager
def processing_lock(
    project_id: str,
    lock_file: Path = DEFAULT_LOCK_FILE,
) -> Generator[None, None, None]:
    """
    Context manager to acquire an exclusive processing lock for a project.
    Raises RuntimeError if a non-stale lock is already held.
    """
    lock_file.parent.mkdir(parents=True, exist_ok=True)

    if lock_file.exists():
        try:
            with open(lock_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            locked_project = data.get("project_id", "unknown")
            locked_pid = data.get("pid", 0)
            locked_time = data.get("timestamp", 0)
            elapsed = time.time() - locked_time

            # Check if stale or dead process
            if elapsed > STALE_LOCK_SECONDS or not _is_pid_running(locked_pid):
                logger.warning(
                    "Breaking stale processing lock held by project '%s' (PID %s, %.1f seconds ago).",
                    locked_project, locked_pid, elapsed,
                )
                try:
                    lock_file.unlink(missing_ok=True)
                except Exception:
                    pass
            else:
                raise RuntimeError(
                    f"Processing is already in progress for project '{locked_project}' "
                    f"(PID {locked_pid}, started {int(elapsed)}s ago). "
                    "Please wait for it to complete."
                )
        except (json.JSONDecodeError, KeyError):
            # Corrupted lock file - clear it
            lock_file.unlink(missing_ok=True)

    # Acquire lock
    lock_data = {
        "project_id": project_id,
        "pid": os.getpid(),
        "timestamp": time.time(),
        "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    with open(lock_file, "w", encoding="utf-8") as f:
        json.dump(lock_data, f, indent=2)

    try:
        yield
    finally:
        try:
            lock_file.unlink(missing_ok=True)
        except Exception as exc:
            logger.warning("Failed to remove processing lock file: %s", exc)

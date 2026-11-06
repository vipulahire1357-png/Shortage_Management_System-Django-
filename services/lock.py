"""
Processing lock for ABB Shortage Management System.

Ensures that concurrent report-processing jobs cannot run simultaneously
and corrupt shared staging or parser database state.

Lock acquisition uses OS-supported exclusive file creation (O_CREAT | O_EXCL)
to prevent the race where two processes simultaneously observe no lock and
both write one.

Stale-lock detection:
  - A lock is stale ONLY if the owner process (PID) is no longer running.
  - A fixed timeout is used as a secondary heuristic: if the lock is older
    than STALE_LOCK_SECONDS AND the process is dead, it is removed.
  - A long-running but live process is NEVER evicted by timeout alone.
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
STALE_LOCK_SECONDS = 900  # 15 minutes — secondary heuristic only


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


def _try_acquire_lock_file(lock_file: Path, lock_data: dict) -> bool:
    """Attempt atomic lock file creation using O_CREAT | O_EXCL.

    Returns True if the lock was successfully created (we own it).
    Returns False if the file already exists (someone else owns it).
    Raises OSError on unexpected filesystem errors.
    """
    try:
        # O_CREAT | O_EXCL: create file only if it does NOT exist (atomic)
        # O_WRONLY: write only
        fd = os.open(
            str(lock_file),
            os.O_CREAT | os.O_EXCL | os.O_WRONLY,
            0o644,
        )
        try:
            content = json.dumps(lock_data, indent=2).encode("utf-8")
            os.write(fd, content)
        finally:
            os.close(fd)
        return True
    except FileExistsError:
        return False


@contextmanager
def processing_lock(
    project_id: str,
    lock_file: Path = DEFAULT_LOCK_FILE,
) -> Generator[None, None, None]:
    """
    Context manager to acquire an exclusive processing lock for a project.

    Uses OS-level atomic file creation to prevent race conditions.
    Raises RuntimeError if a non-stale lock is already held.
    """
    lock_file.parent.mkdir(parents=True, exist_ok=True)

    lock_data = {
        "project_id": project_id,
        "pid": os.getpid(),
        "timestamp": time.time(),
        "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }

    # Attempt atomic acquisition
    if not _try_acquire_lock_file(lock_file, lock_data):
        # Lock file exists — check if it's stale
        try:
            with open(lock_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            locked_project = data.get("project_id", "unknown")
            locked_pid = data.get("pid", 0)
            locked_time = data.get("timestamp", 0)
            elapsed = time.time() - locked_time

            pid_alive = _is_pid_running(locked_pid)

            # A lock is stale if the owner process is dead.
            # The timeout is a secondary safety net — we still check PID.
            if not pid_alive:
                logger.warning(
                    "Breaking stale processing lock held by project '%s' "
                    "(PID %s is no longer running, %.1f seconds ago).",
                    locked_project, locked_pid, elapsed,
                )
                try:
                    lock_file.unlink(missing_ok=True)
                except Exception:
                    pass
            elif elapsed > STALE_LOCK_SECONDS:
                # Process is alive AND lock is old — it's a long-running job.
                # Do NOT break the lock. Report it.
                raise RuntimeError(
                    f"Processing is already in progress for project '{locked_project}' "
                    f"(PID {locked_pid}, started {int(elapsed)}s ago). "
                    "Please wait for it to complete."
                )
            else:
                raise RuntimeError(
                    f"Processing is already in progress for project '{locked_project}' "
                    f"(PID {locked_pid}, started {int(elapsed)}s ago). "
                    "Please wait for it to complete."
                )
        except (json.JSONDecodeError, KeyError):
            # Corrupted lock file - clear it
            logger.warning("Removing corrupted lock file")
            try:
                lock_file.unlink(missing_ok=True)
            except Exception:
                pass

        # Retry atomic acquisition after clearing stale/corrupted lock
        if not _try_acquire_lock_file(lock_file, lock_data):
            # Another process grabbed it between our check and retry
            raise RuntimeError(
                "Could not acquire processing lock — another process claimed it. "
                "Please try again."
            )

    try:
        yield
    finally:
        try:
            # Only remove if we still own it (check PID)
            if lock_file.exists():
                try:
                    with open(lock_file, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    if data.get("pid") == os.getpid():
                        lock_file.unlink(missing_ok=True)
                    else:
                        logger.warning(
                            "Lock file owned by PID %s, not removing (we are PID %s)",
                            data.get("pid"), os.getpid(),
                        )
                except (json.JSONDecodeError, KeyError):
                    lock_file.unlink(missing_ok=True)
        except Exception as exc:
            logger.warning("Failed to remove processing lock file: %s", exc)

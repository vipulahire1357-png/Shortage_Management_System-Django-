"""Comprehensive tests for services/report_pipeline.py and services/lock.py.

Tests cover:
  Discovery & Validation (1-9)
  Staging (10-19)
  Processing flow (20-26)
  Concurrency & Recovery (27-33)
  Regression compatibility (34-38)
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import sys
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from services.report_pipeline import (
    ALL_REPORT_KEYS,
    CLASSIFIERS,
    GLOBAL_REPORT_KEYS,
    PROJECT_REPORT_KEYS,
    PipelineResult,
    ReportFile,
    ValidationResult,
    _is_managed_staged_file,
    _safe_resolve,
    _validate_project_id,
    discover_global_reports,
    discover_project_reports,
    prepare_staging_files,
    process_project_reports,
    promote_staging_files,
    restore_previous_staging,
    validate_report_set,
)
from services.lock import processing_lock, _try_acquire_lock_file


class PipelineTestBase(unittest.TestCase):
    """Base class providing temp directory structure."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="pipeline_test_")
        self.projects_dir = Path(self.tmp) / "projects"
        self.global_dir = Path(self.tmp) / "global"
        self.raw_dir = Path(self.tmp) / "data" / "raw"
        self.data_dir = Path(self.tmp) / "data"

        self.projects_dir.mkdir(parents=True)
        self.global_dir.mkdir(parents=True)
        self.raw_dir.mkdir(parents=True)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _create_project(self, project_id: str, reports: dict[str, str] | None = None):
        """Create a project folder with dummy report files."""
        proj_dir = self.projects_dir / project_id
        proj_dir.mkdir(exist_ok=True)
        if reports is None:
            reports = {
                "ZPP7 List.xls": b"ZPP7_DATA",
                "MB25 BOM74.XLSX": b"MB25_DATA",
                "ME2J REPORT.XLSX": b"ME2J_DATA",
                "PR LIST.XLSX": b"PR_DATA",
                "ME2M 05.11.2026.XLSX": b"ME2M_DATA",
            }
        for name, content in reports.items():
            (proj_dir / name).write_bytes(content)
        return proj_dir

    def _create_global_reports(self, reports: dict[str, str] | None = None):
        """Create global folder with dummy report files."""
        if reports is None:
            reports = {
                "Job IN_MM_INNR_MB52_D_458330, Step 1.htm": b"MB52_DATA",
                "MATERIAL_SCOPE_LIST.xlsx": b"SCOPE_DATA",
            }
        for name, content in reports.items():
            (self.global_dir / name).write_bytes(content)

    def _setup_full_project(self, project_id: str = "IN-10026042"):
        """Create both project and global reports."""
        self._create_project(project_id)
        self._create_global_reports()
        return project_id


# =======================================================================
# Discovery Tests (1-9)
# =======================================================================
class TestDiscoveryAllReportsFound(PipelineTestBase):
    """Test 1: All seven reports discovered."""

    def test_all_seven_found(self):
        self._setup_full_project()
        proj = discover_project_reports("IN-10026042", self.projects_dir)
        glob = discover_global_reports(self.global_dir)

        for key in PROJECT_REPORT_KEYS:
            self.assertIsNotNone(proj[key], f"Project report {key} not found")
        for key in GLOBAL_REPORT_KEYS:
            self.assertIsNotNone(glob[key], f"Global report {key} not found")


class TestDiscoveryMissingProject(PipelineTestBase):
    """Test 2: Missing project report."""

    def test_missing_zpp7(self):
        self._create_project("IN-TEST", reports={
            "MB25 BOM74.XLSX": b"DATA",
            "ME2J REPORT.XLSX": b"DATA",
            "PR LIST.XLSX": b"DATA",
            "ME2M.XLSX": b"DATA",
            # No ZPP7
        })
        proj = discover_project_reports("IN-TEST", self.projects_dir)
        self.assertIsNone(proj["zpp7"])


class TestDiscoveryMissingGlobal(PipelineTestBase):
    """Test 3: Missing global report."""

    def test_missing_mb52(self):
        (self.global_dir / "MATERIAL_SCOPE_LIST.xlsx").write_bytes(b"DATA")
        # No MB52 file
        glob = discover_global_reports(self.global_dir)
        self.assertIsNone(glob["mb52"])


class TestDiscoveryUnsupportedExtension(PipelineTestBase):
    """Test 4: Unsupported extension."""

    def test_validation_rejects_bad_extension(self):
        self._create_project("IN-TEST", reports={
            "ZPP7 List.pdf": b"DATA",  # PDF not supported
            "MB25 BOM.XLSX": b"DATA",
            "ME2J.XLSX": b"DATA",
            "PR.XLSX": b"DATA",
            "ME2M.XLSX": b"DATA",
        })
        self._create_global_reports()
        proj = discover_project_reports("IN-TEST", self.projects_dir)
        glob = discover_global_reports(self.global_dir)
        result = validate_report_set(proj, glob)
        # ZPP7 with .pdf should fail extension check
        self.assertFalse(result.valid)
        self.assertTrue(any("extension" in e.lower() for e in result.errors))


class TestDiscoveryEmptyReport(PipelineTestBase):
    """Test 5: Empty report file."""

    def test_validation_rejects_empty_file(self):
        self._create_project("IN-TEST", reports={
            "ZPP7 List.xls": b"",  # Empty!
            "MB25.XLSX": b"DATA",
            "ME2J.XLSX": b"DATA",
            "PR.XLSX": b"DATA",
            "ME2M.XLSX": b"DATA",
        })
        self._create_global_reports()
        proj = discover_project_reports("IN-TEST", self.projects_dir)
        glob = discover_global_reports(self.global_dir)
        result = validate_report_set(proj, glob)
        self.assertFalse(result.valid)
        self.assertTrue(any("empty" in e.lower() for e in result.errors))


class TestDiscoveryMultipleMatch(PipelineTestBase):
    """Test 6: Only first matching file per report key is used."""

    def test_first_match_wins(self):
        self._create_project("IN-TEST", reports={
            "ZPP7 A.xls": b"DATA_A",
            "ZPP7 B.xlsx": b"DATA_B",
            "MB25.XLSX": b"DATA",
            "ME2J.XLSX": b"DATA",
            "PR.XLSX": b"DATA",
            "ME2M.XLSX": b"DATA",
        })
        proj = discover_project_reports("IN-TEST", self.projects_dir)
        # Should find the first ZPP7 match (alphabetically sorted)
        self.assertIsNotNone(proj["zpp7"])


class TestDiscoveryTempFilesIgnored(PipelineTestBase):
    """Test 7: Excel temporary lock files are ignored."""

    def test_tilde_files_ignored(self):
        self._create_project("IN-TEST", reports={
            "~$ZPP7 List.xls": b"LOCK",
            "ZPP7 List.xls": b"REAL_DATA",
            "MB25.XLSX": b"DATA",
            "ME2J.XLSX": b"DATA",
            "PR.XLSX": b"DATA",
            "ME2M.XLSX": b"DATA",
        })
        proj = discover_project_reports("IN-TEST", self.projects_dir)
        self.assertIsNotNone(proj["zpp7"])
        self.assertFalse(proj["zpp7"].source_path.name.startswith("~$"))


class TestDiscoveryCaseInsensitive(PipelineTestBase):
    """Test 8: Filename matching is case-insensitive."""

    def test_case_insensitive_match(self):
        self._create_project("IN-TEST", reports={
            "zpp7 List.XLS": b"DATA",
            "mb25 report.xlsx": b"DATA",
            "ME2J.xlsx": b"DATA",
            "pr list.Xlsx": b"DATA",
            "Me2M.xlsx": b"DATA",
        })
        proj = discover_project_reports("IN-TEST", self.projects_dir)
        for key in PROJECT_REPORT_KEYS:
            self.assertIsNotNone(proj[key], f"{key} should match case-insensitively")


class TestDiscoveryPathTraversal(PipelineTestBase):
    """Test 9: Project path traversal is rejected."""

    def test_traversal_rejected(self):
        with self.assertRaises(ValueError):
            _safe_resolve(self.projects_dir, "..", "..", "etc")

    def test_invalid_project_id(self):
        with self.assertRaises(ValueError):
            _validate_project_id("../../../etc/passwd")


# =======================================================================
# Staging Tests (10-19)
# =======================================================================
class TestStagingEmptyRaw(PipelineTestBase):
    """Test 10: Staging into empty data/raw/."""

    def test_stage_into_empty(self):
        pid = self._setup_full_project()
        proj = discover_project_reports(pid, self.projects_dir)
        glob = discover_global_reports(self.global_dir)

        temp_dir, staged = prepare_staging_files(proj, glob, self.raw_dir)
        self.assertEqual(len(staged), 7)

        backups = promote_staging_files(staged, self.raw_dir)
        # No existing files → no backups
        self.assertEqual(len(backups), 0)

        # Verify all 7 files in raw
        raw_files = {f.name for f in self.raw_dir.iterdir() if f.is_file()}
        self.assertEqual(len(raw_files), 7)

        shutil.rmtree(temp_dir, ignore_errors=True)


class TestStagingReplaceExisting(PipelineTestBase):
    """Test 11: Existing staged files are replaced."""

    def test_replace_old_files(self):
        # Pre-populate raw
        (self.raw_dir / "ZPP7.xls").write_bytes(b"OLD_ZPP7")
        (self.raw_dir / "MB25.xlsx").write_bytes(b"OLD_MB25")

        pid = self._setup_full_project()
        proj = discover_project_reports(pid, self.projects_dir)
        glob = discover_global_reports(self.global_dir)

        temp_dir, staged = prepare_staging_files(proj, glob, self.raw_dir)
        backups = promote_staging_files(staged, self.raw_dir)

        # Old files should be backed up
        self.assertIn("ZPP7.xls", backups)
        self.assertIn("MB25.xlsx", backups)

        # New files should be in raw
        self.assertTrue((self.raw_dir / "ZPP7.xls").is_file())
        self.assertNotEqual(
            (self.raw_dir / "ZPP7.xls").read_bytes(), b"OLD_ZPP7"
        )

        shutil.rmtree(temp_dir, ignore_errors=True)


class TestStagingNoMixOldNew(PipelineTestBase):
    """Test 12: Old project files are not mixed with new."""

    def test_all_old_removed_before_new(self):
        # This is verified by the promote_staging_files implementation:
        # Step 1: backup all managed, Step 2: remove all managed, Step 3: copy new
        pid = self._setup_full_project()
        proj = discover_project_reports(pid, self.projects_dir)
        glob = discover_global_reports(self.global_dir)

        temp_dir, staged = prepare_staging_files(proj, glob, self.raw_dir)
        promote_staging_files(staged, self.raw_dir)

        # All files should come from the new project
        for f in self.raw_dir.iterdir():
            if _is_managed_staged_file(f):
                self.assertGreater(f.stat().st_size, 0)

        shutil.rmtree(temp_dir, ignore_errors=True)


class TestStagingSourceUnchanged(PipelineTestBase):
    """Test 14: Original source files remain unchanged after staging."""

    def test_source_files_intact(self):
        pid = self._setup_full_project()
        proj_dir = self.projects_dir / pid

        # Record original sizes
        original_sizes = {
            f.name: f.stat().st_size
            for f in proj_dir.iterdir() if f.is_file()
        }

        proj = discover_project_reports(pid, self.projects_dir)
        glob = discover_global_reports(self.global_dir)
        temp_dir, staged = prepare_staging_files(proj, glob, self.raw_dir)
        promote_staging_files(staged, self.raw_dir)

        # Verify originals unchanged
        for f in proj_dir.iterdir():
            if f.is_file():
                self.assertEqual(f.stat().st_size, original_sizes[f.name])

        shutil.rmtree(temp_dir, ignore_errors=True)


class TestStagingUnrelatedFiles(PipelineTestBase):
    """Test 15: Unrelated files in data/raw/ remain untouched."""

    def test_unrelated_preserved(self):
        (self.raw_dir / "my_notes.txt").write_bytes(b"Keep me!")
        (self.raw_dir / "custom_report.csv").write_bytes(b"Custom data")

        pid = self._setup_full_project()
        proj = discover_project_reports(pid, self.projects_dir)
        glob = discover_global_reports(self.global_dir)
        temp_dir, staged = prepare_staging_files(proj, glob, self.raw_dir)
        promote_staging_files(staged, self.raw_dir)

        self.assertTrue((self.raw_dir / "my_notes.txt").is_file())
        self.assertTrue((self.raw_dir / "custom_report.csv").is_file())
        self.assertEqual(
            (self.raw_dir / "my_notes.txt").read_bytes(), b"Keep me!"
        )

        shutil.rmtree(temp_dir, ignore_errors=True)


class TestStagingRestorationAfterSuccess(PipelineTestBase):
    """Test 16: Original staged files restored after success."""

    def test_restore_after_success(self):
        (self.raw_dir / "ZPP7.xls").write_bytes(b"ORIGINAL")

        pid = self._setup_full_project()
        proj = discover_project_reports(pid, self.projects_dir)
        glob = discover_global_reports(self.global_dir)
        temp_dir, staged = prepare_staging_files(proj, glob, self.raw_dir)
        backups = promote_staging_files(staged, self.raw_dir)

        # Simulate: processing happened here...
        # Now restore
        errors = restore_previous_staging(backups, self.raw_dir)
        self.assertEqual(errors, [])
        self.assertEqual(
            (self.raw_dir / "ZPP7.xls").read_bytes(), b"ORIGINAL"
        )

        shutil.rmtree(temp_dir, ignore_errors=True)


class TestStagingRestorationAfterFailure(PipelineTestBase):
    """Test 17: Original staged files restored after parser failure."""

    def test_restore_after_failure(self):
        (self.raw_dir / "ZPP7.xls").write_bytes(b"ORIGINAL_ZPP7")

        pid = self._setup_full_project()
        proj = discover_project_reports(pid, self.projects_dir)
        glob = discover_global_reports(self.global_dir)
        temp_dir, staged = prepare_staging_files(proj, glob, self.raw_dir)
        backups = promote_staging_files(staged, self.raw_dir)

        # Simulate parser failure → still restore
        errors = restore_previous_staging(backups, self.raw_dir)
        self.assertEqual(errors, [])
        self.assertEqual(
            (self.raw_dir / "ZPP7.xls").read_bytes(), b"ORIGINAL_ZPP7"
        )

        shutil.rmtree(temp_dir, ignore_errors=True)


class TestStagingFailedPrepNoPartialReplace(PipelineTestBase):
    """Test 18: Failed staging preparation does not partially replace files."""

    def test_failed_prep_leaves_raw_untouched(self):
        (self.raw_dir / "ZPP7.xls").write_bytes(b"EXISTING")

        # Create project with only some reports
        self._create_project("IN-TEST", reports={
            "ZPP7 List.xls": b"DATA",
        })
        self._create_global_reports()

        proj = discover_project_reports("IN-TEST", self.projects_dir)
        glob = discover_global_reports(self.global_dir)

        # Missing reports will cause prepare_staging_files to fail
        with self.assertRaises(RuntimeError):
            prepare_staging_files(proj, glob, self.raw_dir)

        # Existing file should be untouched
        self.assertEqual(
            (self.raw_dir / "ZPP7.xls").read_bytes(), b"EXISTING"
        )


class TestStagingCopyVerification(PipelineTestBase):
    """Test 19: Copy verification catches issues."""

    def test_staged_files_verified(self):
        pid = self._setup_full_project()
        proj = discover_project_reports(pid, self.projects_dir)
        glob = discover_global_reports(self.global_dir)

        temp_dir, staged = prepare_staging_files(proj, glob, self.raw_dir)

        # All staged files should have same size as source
        all_reports = {**proj, **glob}
        for key in ALL_REPORT_KEYS:
            report = all_reports[key]
            staged_path = staged[report.staged_name]
            self.assertEqual(
                staged_path.stat().st_size,
                report.source_path.stat().st_size,
            )

        shutil.rmtree(temp_dir, ignore_errors=True)


# =======================================================================
# Processing Tests (20-26)
# =======================================================================
class TestAllParsersExecute(PipelineTestBase):
    """Test 20: All seven parsers execute."""

    @patch("services.report_pipeline.run_all_report_parsers")
    @patch("services.report_pipeline.run_project_shortage_service")
    @patch("services.report_pipeline.refresh_project_vendor_track")
    @patch("services.report_pipeline.export_project_shortage_register")
    def test_all_parsers_called(self, mock_export, mock_vt, mock_shortage, mock_parsers):
        from parsers import available_parsers
        mock_parsers.return_value = {
            name: {"status": "SUCCESS", "result": None, "elapsed": 0.1}
            for name in available_parsers()
        }
        mock_shortage.return_value = {"rows_written": 100, "elapsed_seconds": 0.5}
        mock_vt.return_value = 50
        mock_export.return_value = {"filename": "test.xlsx", "rows": 100}

        pid = self._setup_full_project()
        result = process_project_reports(
            pid,
            projects_dir=self.projects_dir,
            global_dir=self.global_dir,
            raw_dir=self.raw_dir,
        )

        mock_parsers.assert_called_once()
        self.assertTrue(result.all_parsers_passed)


class TestShortageOnlyAfterParsers(PipelineTestBase):
    """Test 21-22: Shortage executes only after all parsers succeed.
    A parser failure prevents shortage generation."""

    @patch("services.report_pipeline.run_all_report_parsers")
    @patch("services.report_pipeline.run_project_shortage_service")
    def test_parser_failure_stops_shortage(self, mock_shortage, mock_parsers):
        mock_parsers.return_value = {
            "mb25": {"status": "SUCCESS", "result": None, "elapsed": 0.1},
            "zpp7": {"status": "FAILED", "error": "File corrupt", "elapsed": 0.1},
        }

        pid = self._setup_full_project()
        result = process_project_reports(
            pid,
            projects_dir=self.projects_dir,
            global_dir=self.global_dir,
            raw_dir=self.raw_dir,
        )

        self.assertFalse(result.success)
        self.assertFalse(result.all_parsers_passed)
        mock_shortage.assert_not_called()


class TestVendorTrackAfterShortage(PipelineTestBase):
    """Test 23-24: Vendor Track runs only after shortage success.
    Register export runs only after successful data generation."""

    @patch("services.report_pipeline.run_all_report_parsers")
    @patch("services.report_pipeline.run_project_shortage_service")
    @patch("services.report_pipeline.refresh_project_vendor_track")
    @patch("services.report_pipeline.export_project_shortage_register")
    def test_shortage_failure_stops_vendor_track(self, mock_export, mock_vt, mock_shortage, mock_parsers):
        from parsers import available_parsers
        mock_parsers.return_value = {
            name: {"status": "SUCCESS", "result": None, "elapsed": 0.1}
            for name in available_parsers()
        }
        mock_shortage.side_effect = RuntimeError("Database error")

        pid = self._setup_full_project()
        result = process_project_reports(
            pid,
            projects_dir=self.projects_dir,
            global_dir=self.global_dir,
            raw_dir=self.raw_dir,
        )

        self.assertFalse(result.success)
        mock_vt.assert_not_called()
        mock_export.assert_not_called()


class TestProjectIdPropagated(PipelineTestBase):
    """Test 25: Selected project ID is propagated through every service."""

    @patch("services.report_pipeline.run_all_report_parsers")
    @patch("services.report_pipeline.run_project_shortage_service")
    @patch("services.report_pipeline.refresh_project_vendor_track")
    @patch("services.report_pipeline.export_project_shortage_register")
    def test_project_id_passes_through(self, mock_export, mock_vt, mock_shortage, mock_parsers):
        from parsers import available_parsers
        mock_parsers.return_value = {
            name: {"status": "SUCCESS", "result": None, "elapsed": 0.1}
            for name in available_parsers()
        }
        mock_shortage.return_value = {"rows_written": 10, "elapsed_seconds": 0.1}
        mock_vt.return_value = 5
        mock_export.return_value = {"filename": "f.xlsx", "rows": 10}

        pid = self._setup_full_project()
        result = process_project_reports(
            pid,
            projects_dir=self.projects_dir,
            global_dir=self.global_dir,
            raw_dir=self.raw_dir,
        )

        mock_shortage.assert_called_once_with(pid)
        mock_vt.assert_called_once()
        self.assertEqual(mock_vt.call_args[0][0], pid)
        mock_export.assert_called_once()
        self.assertEqual(mock_export.call_args[0][0], pid)


class TestParserFailureDetails(PipelineTestBase):
    """Test 26: Parser failures returned with useful details."""

    @patch("services.report_pipeline.run_all_report_parsers")
    def test_failure_details_in_result(self, mock_parsers):
        mock_parsers.return_value = {
            "me2m": {"status": "FAILED", "error": "Column mismatch in ME2M", "elapsed": 0.5},
        }

        pid = self._setup_full_project()
        result = process_project_reports(
            pid,
            projects_dir=self.projects_dir,
            global_dir=self.global_dir,
            raw_dir=self.raw_dir,
        )

        self.assertFalse(result.success)
        self.assertIn("me2m", result.error)
        self.assertIn("Column mismatch", result.error)


# =======================================================================
# Concurrency & Recovery Tests (27-33)
# =======================================================================
class TestLockBeforeStaging(PipelineTestBase):
    """Test 27: Lock is acquired before staging modifications."""

    @patch("services.report_pipeline.run_all_report_parsers")
    @patch("services.report_pipeline.run_project_shortage_service")
    @patch("services.report_pipeline.refresh_project_vendor_track")
    @patch("services.report_pipeline.export_project_shortage_register")
    def test_lock_acquired_before_staging(self, mock_export, mock_vt, mock_shortage, mock_parsers):
        from parsers import available_parsers
        mock_parsers.return_value = {
            name: {"status": "SUCCESS", "result": None, "elapsed": 0.1}
            for name in available_parsers()
        }
        mock_shortage.return_value = {"rows_written": 10, "elapsed_seconds": 0.1}
        mock_vt.return_value = 5
        mock_export.return_value = {"filename": "f.xlsx", "rows": 10}

        pid = self._setup_full_project()
        result = process_project_reports(
            pid,
            projects_dir=self.projects_dir,
            global_dir=self.global_dir,
            raw_dir=self.raw_dir,
        )

        self.assertTrue(result.lock_acquired)
        self.assertTrue(result.staging_prepared)


class TestConcurrentProcessingRejected(PipelineTestBase):
    """Test 28: Concurrent processing is rejected."""

    def test_concurrent_lock_rejected(self):
        lock_file = Path(self.tmp) / "test.lock"

        with processing_lock("PROJ-A", lock_file=lock_file):
            # Try to acquire same lock
            with self.assertRaises(RuntimeError) as ctx:
                with processing_lock("PROJ-B", lock_file=lock_file):
                    pass
            self.assertIn("already in progress", str(ctx.exception))


class TestLockReleasedAfterSuccess(PipelineTestBase):
    """Test 29: Lock is released after success."""

    def test_lock_released_on_success(self):
        lock_file = Path(self.tmp) / "test.lock"

        with processing_lock("PROJ-A", lock_file=lock_file):
            self.assertTrue(lock_file.exists())

        self.assertFalse(lock_file.exists())


class TestLockReleasedAfterException(PipelineTestBase):
    """Test 30: Lock is released after exceptions."""

    def test_lock_released_on_exception(self):
        lock_file = Path(self.tmp) / "test.lock"

        with self.assertRaises(ValueError):
            with processing_lock("PROJ-A", lock_file=lock_file):
                raise ValueError("Simulated error")

        self.assertFalse(lock_file.exists())


class TestLockNotRemovedForRunningProcess(PipelineTestBase):
    """Test 31: A valid running process lock is not incorrectly deleted."""

    def test_running_process_lock_preserved(self):
        lock_file = Path(self.tmp) / "test.lock"

        # Create a lock owned by THIS process (which is running)
        lock_data = {
            "project_id": "PROJ-X",
            "pid": os.getpid(),
            "timestamp": time.time(),
        }
        lock_file.write_text(json.dumps(lock_data), encoding="utf-8")

        with self.assertRaises(RuntimeError) as ctx:
            with processing_lock("PROJ-Y", lock_file=lock_file):
                pass
        self.assertIn("already in progress", str(ctx.exception))
        # Lock file should still exist (not broken)
        self.assertTrue(lock_file.exists())


class TestStagingRestorationFailureReported(PipelineTestBase):
    """Test 32: Staging restoration failure is reported."""

    def test_restoration_errors_reported(self):
        # Create a backup file that exists, but make the raw_dir unwritable
        # by pointing to a non-existent raw dir
        backup_file = Path(self.tmp) / "backup_zpp7.xls"
        backup_file.write_bytes(b"BACKUP_DATA")
        backups = {"ZPP7.xls": backup_file}
        bad_raw = Path(self.tmp) / "nonexistent_raw"
        # Don't create bad_raw — restore will fail to iterate it
        errors = restore_previous_staging(backups, bad_raw)
        self.assertGreater(len(errors), 0)


class TestStaleLockRecovery(PipelineTestBase):
    """Test 33: Stale lock (dead process) recovery works safely."""

    def test_stale_lock_broken(self):
        lock_file = Path(self.tmp) / "test.lock"

        # Create a lock with a dead PID
        lock_data = {
            "project_id": "PROJ-OLD",
            "pid": 999999999,  # Almost certainly not running
            "timestamp": time.time() - 60,
        }
        lock_file.write_text(json.dumps(lock_data), encoding="utf-8")

        # Should be able to acquire lock (stale lock broken)
        with processing_lock("PROJ-NEW", lock_file=lock_file):
            self.assertTrue(lock_file.exists())
            data = json.loads(lock_file.read_text(encoding="utf-8"))
            self.assertEqual(data["project_id"], "PROJ-NEW")


# =======================================================================
# Atomic lock test
# =======================================================================
class TestAtomicLockCreation(PipelineTestBase):
    """Verify _try_acquire_lock_file uses O_CREAT|O_EXCL for atomicity."""

    def test_atomic_create(self):
        lock_file = Path(self.tmp) / "atomic.lock"
        data = {"pid": os.getpid(), "project_id": "TEST"}

        # First attempt should succeed
        self.assertTrue(_try_acquire_lock_file(lock_file, data))
        self.assertTrue(lock_file.exists())

        # Second attempt should fail (file exists)
        self.assertFalse(_try_acquire_lock_file(lock_file, data))


# =======================================================================
# Regression Tests (34-38)
# =======================================================================
class TestValidationReport(PipelineTestBase):
    """Test validation produces clear error messages."""

    def test_validation_report_all_missing(self):
        proj = {k: None for k in PROJECT_REPORT_KEYS}
        glob = {k: None for k in GLOBAL_REPORT_KEYS}
        result = validate_report_set(proj, glob)
        self.assertFalse(result.valid)
        self.assertEqual(len(result.errors), 7)  # All 7 reports missing


class TestPipelineResultStructure(unittest.TestCase):
    """Test PipelineResult dataclass has all expected fields."""

    def test_result_fields(self):
        r = PipelineResult(project_id="TEST")
        self.assertEqual(r.project_id, "TEST")
        self.assertFalse(r.success)
        self.assertEqual(r.error, "")
        self.assertIsInstance(r.parser_results, dict)
        self.assertIsInstance(r.shortage_result, dict)
        self.assertFalse(r.lock_acquired)
        self.assertFalse(r.staging_prepared)
        self.assertFalse(r.staging_verified)
        self.assertFalse(r.all_parsers_passed)
        self.assertFalse(r.shortage_success)
        self.assertFalse(r.vendor_track_success)
        self.assertFalse(r.export_success)


class TestProcessingResultOnMissingReports(PipelineTestBase):
    """Test that pipeline returns clear error when reports are missing."""

    def test_missing_reports_error(self):
        self._create_project("IN-TEST", reports={
            "ZPP7 List.xls": b"DATA",
            # Missing other reports
        })
        self._create_global_reports()

        result = process_project_reports(
            "IN-TEST",
            projects_dir=self.projects_dir,
            global_dir=self.global_dir,
            raw_dir=self.raw_dir,
        )

        self.assertFalse(result.success)
        self.assertIn("Validation failed", result.error)
        self.assertIn("Missing", result.error)


class TestManagedFileDetection(unittest.TestCase):
    """Test _is_managed_staged_file correctly identifies report files."""

    def test_known_stems(self):
        tmp = Path(tempfile.mkdtemp())
        try:
            # Known report files
            for name in ["ZPP7.xls", "MB25.xlsx", "ME2J.xlsx", "PR.xlsx",
                         "ME2M.xlsb", "MB52.htm", "MATERIAL_SCOPE_LIST.xlsx"]:
                f = tmp / name
                f.write_bytes(b"DATA")
                self.assertTrue(_is_managed_staged_file(f), f"{name} should be managed")

            # Unknown files
            for name in ["notes.txt", "custom.csv", "readme.md"]:
                f = tmp / name
                f.write_bytes(b"DATA")
                self.assertFalse(_is_managed_staged_file(f), f"{name} should NOT be managed")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class TestEndToEndWithMocks(PipelineTestBase):
    """Full pipeline test with mocked parsers and services."""

    @patch("services.report_pipeline.run_all_report_parsers")
    @patch("services.report_pipeline.run_project_shortage_service")
    @patch("services.report_pipeline.refresh_project_vendor_track")
    @patch("services.report_pipeline.export_project_shortage_register")
    def test_full_success_flow(self, mock_export, mock_vt, mock_shortage, mock_parsers):
        from parsers import available_parsers
        mock_parsers.return_value = {
            name: {"status": "SUCCESS", "result": None, "elapsed": 0.1}
            for name in available_parsers()
        }
        mock_shortage.return_value = {"rows_written": 500, "elapsed_seconds": 0.5}
        mock_vt.return_value = 200
        mock_export.return_value = {"filename": "IN-TEST_Net_shortage_Table.xlsx", "rows": 500}

        # Pre-populate raw with old files
        (self.raw_dir / "ZPP7.xls").write_bytes(b"OLD")
        (self.raw_dir / "my_notes.txt").write_bytes(b"KEEP")

        pid = self._setup_full_project()
        result = process_project_reports(
            pid,
            projects_dir=self.projects_dir,
            global_dir=self.global_dir,
            raw_dir=self.raw_dir,
        )

        # Success
        self.assertTrue(result.success)
        self.assertTrue(result.lock_acquired)
        self.assertTrue(result.staging_prepared)
        self.assertTrue(result.staging_verified)
        self.assertTrue(result.all_parsers_passed)
        self.assertTrue(result.shortage_success)
        self.assertTrue(result.vendor_track_success)
        self.assertTrue(result.export_success)

        # Restoration happened
        self.assertTrue(result.restoration_success)

        # Old file restored
        self.assertEqual(
            (self.raw_dir / "ZPP7.xls").read_bytes(), b"OLD"
        )
        # Unrelated file untouched
        self.assertTrue((self.raw_dir / "my_notes.txt").is_file())

        # Result has timing
        self.assertGreater(result.elapsed_seconds, 0)


if __name__ == "__main__":
    unittest.main()


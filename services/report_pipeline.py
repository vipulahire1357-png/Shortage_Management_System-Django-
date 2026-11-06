"""
Reliable SAP Report Staging and Processing Pipeline.

Orchestrates the complete workflow for project-specific shortage register generation:
  1. Discover project-specific and global SAP reports
  2. Validate all source files
  3. Safely stage reports into data/raw/
  4. Execute all seven SAP parsers
  5. Run optimized net_shortage engine
  6. Refresh Vendor Track
  7. Export Material Shortage Register
  8. Restore previous staging files

All operations run inside the existing processing_lock to prevent concurrent
modification of shared staging and database state.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import shutil
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger("abb_shortage.report_pipeline")

# ---------------------------------------------------------------------------
# Path configuration — uses existing Django settings / environment variables
# ---------------------------------------------------------------------------
_PROJECT_ROOT = Path(__file__).resolve().parents[1]

DATA_ROOT = Path(os.getenv("ABB_DATA_ROOT", _PROJECT_ROOT / "data")).resolve()
RAW_DIR = Path(os.getenv("ABB_RAW_DIR", DATA_ROOT / "raw")).resolve()
PROJECTS_DIR = Path(os.getenv("ABB_PROJECTS_DIR", _PROJECT_ROOT / "projects")).resolve()
GLOBAL_DIR = Path(os.getenv("ABB_GLOBAL_DIR", _PROJECT_ROOT / "global")).resolve()
SERVICES_DB = Path(os.getenv("ABB_SERVICES_DB", DATA_ROOT / "services.db")).resolve()
GLOBAL_DB = Path(os.getenv("ABB_GLOBAL_DB", DATA_ROOT / "global.db")).resolve()
ZPP7_DB = Path(os.getenv("ABB_ZPP7_DB", DATA_ROOT / "zpp7_breakdown.db")).resolve()
SHORTAGE_REGISTERS_DIR = Path(
    os.getenv("SHORTAGE_REGISTERS_DIR", str(_PROJECT_ROOT / "Material Shortage Registers"))
).resolve()
VENDOR_REGISTERS_DIR = Path(
    os.getenv("VENDOR_REGISTERS_DIR", str(_PROJECT_ROOT / "Vendor Registers"))
).resolve()

# ---------------------------------------------------------------------------
# Report definitions — imported from existing shortage/data.py
# ---------------------------------------------------------------------------
# Re-use the existing report metadata to avoid duplication
from shortage.data import REPORTS

# Report classification order — which reports come from projects vs global
PROJECT_REPORT_KEYS = ("zpp7", "mb25", "me2j", "me2m", "pr")
GLOBAL_REPORT_KEYS = ("mb52", "scopelist")
ALL_REPORT_KEYS = PROJECT_REPORT_KEYS + GLOBAL_REPORT_KEYS

# Classifiers: each function returns True if a filename matches a given report
CLASSIFIERS = {
    "zpp7": lambda p: "zpp7" in p.name.casefold(),
    "mb25": lambda p: "mb25" in p.name.casefold(),
    "me2j": lambda p: "me2j" in p.stem.casefold(),
    "pr": lambda p: (
        p.stem.casefold() == "pr"
        or p.stem.casefold().startswith(("pr_", "pr ", "purchase requisition"))
        or p.stem.casefold() == "pr list"
    ),
    "me2m": lambda p: "me2m" in p.name.casefold(),
    "mb52": lambda p: "mb52" in p.name.casefold(),
    "scopelist": lambda p: "material_scope_list" in p.name.casefold(),
}

# Safe project ID pattern
SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,63}$")


# ---------------------------------------------------------------------------
# Result data classes
# ---------------------------------------------------------------------------
@dataclass
class ReportFile:
    """A discovered source report file."""
    key: str             # Report key (e.g. "zpp7", "mb52")
    source_path: Path    # Original file path
    staged_name: str     # Name used in data/raw/
    label: str           # Human-readable label


@dataclass
class PipelineResult:
    """Complete result from a processing pipeline run."""
    project_id: str
    success: bool = False
    error: str = ""

    # Discovery
    project_reports: dict[str, ReportFile] = field(default_factory=dict)
    global_reports: dict[str, ReportFile] = field(default_factory=dict)

    # Staging
    staging_prepared: bool = False
    staging_verified: bool = False

    # Parser results
    parser_results: dict[str, dict[str, Any]] = field(default_factory=dict)
    all_parsers_passed: bool = False

    # Shortage
    shortage_result: dict[str, Any] = field(default_factory=dict)
    shortage_success: bool = False

    # Vendor track
    vendor_track_rows: int = 0
    vendor_track_success: bool = False

    # Export
    export_result: dict[str, Any] = field(default_factory=dict)
    export_success: bool = False

    # Restoration
    restoration_success: bool = False
    restoration_errors: list[str] = field(default_factory=list)

    # Timing
    elapsed_seconds: float = 0.0

    # Lock
    lock_acquired: bool = False


# ---------------------------------------------------------------------------
# 1. Report Discovery
# ---------------------------------------------------------------------------
def discover_project_reports(
    project_id: str,
    projects_dir: Path = PROJECTS_DIR,
) -> dict[str, ReportFile | None]:
    """Discover and classify reports in the selected project folder.

    Returns a dict mapping report keys to ReportFile or None (not found).
    """
    _validate_project_id(project_id)
    project_dir = _safe_resolve(projects_dir, project_id)

    if not project_dir.is_dir():
        raise FileNotFoundError(
            f"Project folder not found: {project_dir}"
        )

    return _discover_reports(project_dir, PROJECT_REPORT_KEYS)


def discover_global_reports(
    global_dir: Path = GLOBAL_DIR,
) -> dict[str, ReportFile | None]:
    """Discover and classify shared global reports."""
    if not global_dir.is_dir():
        raise FileNotFoundError(
            f"Global report folder not found: {global_dir}"
        )

    return _discover_reports(global_dir, GLOBAL_REPORT_KEYS)


def _discover_reports(
    folder: Path,
    keys: tuple[str, ...],
) -> dict[str, ReportFile | None]:
    """Discover reports in a folder using classifiers."""
    found: dict[str, ReportFile | None] = {key: None for key in keys}

    if not folder.is_dir():
        return found

    for path in sorted(folder.iterdir()):
        if not path.is_file():
            continue
        # Skip Excel temp/lock files
        if path.name.startswith("~$"):
            continue
        for key in keys:
            if found[key] is None and key in CLASSIFIERS and CLASSIFIERS[key](path):
                stem = REPORTS[key]["stem"]
                staged_name = f"{stem}{path.suffix}"
                found[key] = ReportFile(
                    key=key,
                    source_path=path,
                    staged_name=staged_name,
                    label=REPORTS[key]["label"],
                )
                break  # Each file matches at most one report

    return found


# ---------------------------------------------------------------------------
# 2. Validation
# ---------------------------------------------------------------------------
@dataclass
class ValidationResult:
    valid: bool = True
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def validate_report_set(
    project_reports: dict[str, ReportFile | None],
    global_reports: dict[str, ReportFile | None],
) -> ValidationResult:
    """Validate presence, extensions, readability, and uniqueness of all reports."""
    result = ValidationResult()
    all_reports = {**project_reports, **global_reports}

    # 1. Check all required reports present
    for key in ALL_REPORT_KEYS:
        report = all_reports.get(key)
        if report is None:
            source = "project" if key in PROJECT_REPORT_KEYS else "global"
            result.valid = False
            result.errors.append(
                f"Missing required {source} report: {REPORTS[key]['label']} ({key})"
            )
            continue

        path = report.source_path

        # 2. Verify file exists and is a regular file
        if not path.is_file():
            result.valid = False
            result.errors.append(f"{report.label}: file does not exist: {path.name}")
            continue

        # 3. Check not an Excel temp/lock file
        if path.name.startswith("~$"):
            result.valid = False
            result.errors.append(
                f"{report.label}: file is an Excel temporary lock file: {path.name}"
            )
            continue

        # 4. Check file extension
        allowed_exts = REPORTS[key].get("extensions", set())
        if allowed_exts and path.suffix.lower() not in allowed_exts:
            result.valid = False
            result.errors.append(
                f"{report.label}: unsupported extension '{path.suffix}'. "
                f"Expected one of: {', '.join(sorted(allowed_exts))}"
            )

        # 5. Check non-empty
        try:
            size = path.stat().st_size
            if size == 0:
                result.valid = False
                result.errors.append(f"{report.label}: file is empty: {path.name}")
        except OSError as exc:
            result.valid = False
            result.errors.append(f"{report.label}: cannot read file: {exc}")
            continue

        # 6. Verify readable
        try:
            with open(path, "rb") as f:
                f.read(1)
        except OSError as exc:
            result.valid = False
            result.errors.append(f"{report.label}: file is not readable: {exc}")

    # 7. Check no two sources classified as same report (shouldn't happen
    #    with current classifier design, but verify)
    staged_names: dict[str, str] = {}
    for key, report in all_reports.items():
        if report is None:
            continue
        if report.staged_name in staged_names:
            result.valid = False
            result.errors.append(
                f"Staging conflict: '{report.label}' and '{staged_names[report.staged_name]}' "
                f"would both produce '{report.staged_name}'"
            )
        staged_names[report.staged_name] = report.label

    return result


# ---------------------------------------------------------------------------
# 3. Safe Staging
# ---------------------------------------------------------------------------
def _known_staged_stems() -> set[str]:
    """Return the set of known report file stems (lowered)."""
    stems = {item["stem"].casefold() for item in REPORTS.values()}
    # ME2M filenames sometimes include dates; match "me2m" anywhere
    return stems


def _is_managed_staged_file(path: Path) -> bool:
    """Check if a file in data/raw/ is a managed staged report file."""
    if not path.is_file():
        return False
    stems = _known_staged_stems()
    stem_lower = path.stem.casefold()
    return stem_lower in stems or "me2m" in stem_lower


def prepare_staging_files(
    project_reports: dict[str, ReportFile | None],
    global_reports: dict[str, ReportFile | None],
    raw_dir: Path = RAW_DIR,
) -> tuple[Path, dict[str, Path]]:
    """Create a verified temporary set of seven report files.

    Returns (temp_dir, staged_files_map) where staged_files_map maps
    staged_name → temp_path. The caller is responsible for cleaning
    up the temp directory.
    """
    all_reports = {**project_reports, **global_reports}

    temp_dir = Path(tempfile.mkdtemp(prefix="abb-staging-"))
    staged_files: dict[str, Path] = {}

    try:
        for key in ALL_REPORT_KEYS:
            report = all_reports[key]
            if report is None:
                raise RuntimeError(f"Missing report during staging: {key}")

            dest = temp_dir / report.staged_name
            shutil.copy2(report.source_path, dest)

            # Verify copy
            if not dest.is_file():
                raise RuntimeError(
                    f"Failed to copy {report.label}: destination file not created"
                )

            src_size = report.source_path.stat().st_size
            dst_size = dest.stat().st_size
            if src_size != dst_size:
                raise RuntimeError(
                    f"Copy verification failed for {report.label}: "
                    f"source={src_size} bytes, destination={dst_size} bytes"
                )

            staged_files[report.staged_name] = dest
            logger.info("[STAGING] Copied %s → %s (%d bytes)",
                        report.source_path.name, report.staged_name, dst_size)

    except Exception:
        # Cleanup on failure
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise

    logger.info("[STAGING] All %d reports prepared in temp dir: %s",
                len(staged_files), temp_dir)
    return temp_dir, staged_files


def promote_staging_files(
    staged_files: dict[str, Path],
    raw_dir: Path = RAW_DIR,
) -> dict[str, Path]:
    """Safely replace managed files in data/raw/ with new staged files.

    Returns backup map: {original_name: backup_path} for restoration.
    """
    raw_dir.mkdir(parents=True, exist_ok=True)

    backup_dir = Path(tempfile.mkdtemp(prefix="abb-raw-backup-"))
    backups: dict[str, Path] = {}

    try:
        # Step 1: Backup existing managed files
        for existing in raw_dir.iterdir():
            if _is_managed_staged_file(existing):
                backup_path = backup_dir / existing.name
                shutil.copy2(existing, backup_path)
                backups[existing.name] = backup_path
                logger.debug("[BACKUP] Backed up %s", existing.name)

        # Step 2: Remove existing managed files
        for existing in raw_dir.iterdir():
            if _is_managed_staged_file(existing):
                existing.unlink()

        # Step 3: Copy new files into raw
        for staged_name, temp_path in staged_files.items():
            dest = raw_dir / staged_name
            shutil.copy2(temp_path, dest)

            # Verify
            if not dest.is_file():
                raise RuntimeError(f"Staged file not created: {dest}")
            if dest.stat().st_size != temp_path.stat().st_size:
                raise RuntimeError(f"Size mismatch for staged file: {staged_name}")

        logger.info("[STAGING] Promoted %d files into %s", len(staged_files), raw_dir)

    except Exception:
        # Restore from backup on failure
        logger.error("[STAGING] Promotion failed, restoring backups")
        _restore_from_backup(backups, raw_dir)
        shutil.rmtree(backup_dir, ignore_errors=True)
        raise

    return backups


def restore_previous_staging(
    backups: dict[str, Path],
    raw_dir: Path = RAW_DIR,
) -> list[str]:
    """Restore previous managed report files from backup.

    Returns a list of error messages (empty if all successful).
    """
    errors: list[str] = []

    # Remove current managed files
    try:
        for candidate in raw_dir.iterdir():
            if _is_managed_staged_file(candidate):
                try:
                    candidate.unlink()
                except OSError as exc:
                    errors.append(f"Failed to remove {candidate.name}: {exc}")
    except OSError as exc:
        errors.append(f"Failed to iterate raw dir: {exc}")

    # Restore backups
    for name, backup_path in backups.items():
        try:
            if backup_path.is_file():
                shutil.copy2(backup_path, raw_dir / name)
        except OSError as exc:
            errors.append(f"Failed to restore {name}: {exc}")

    if errors:
        logger.error("[RESTORATION] Errors during restoration: %s", errors)
    else:
        logger.info("[RESTORATION] Previous %d managed files restored", len(backups))

    return errors


def _restore_from_backup(
    backups: dict[str, Path],
    raw_dir: Path,
) -> None:
    """Emergency restore from backup — used during failed promotion."""
    for name, backup_path in backups.items():
        try:
            if backup_path.is_file():
                shutil.copy2(backup_path, raw_dir / name)
        except OSError:
            pass  # Best effort during error recovery


# ---------------------------------------------------------------------------
# 4. Parser Execution
# ---------------------------------------------------------------------------
def run_all_report_parsers() -> dict[str, dict[str, Any]]:
    """Run all registered parsers and return results.

    Uses the existing parser registry from parsers/__init__.py.
    If ANY parser fails, the result dict marks it as FAILED with error details.

    Returns: {parser_name: {"status": "SUCCESS"|"FAILED", "result": ..., "error": ...}}
    """
    from parsers import available_parsers, run_parser

    results: dict[str, dict[str, Any]] = {}

    for name in available_parsers():
        logger.info("[PARSER] Starting %s", name.upper())
        t = time.time()
        try:
            result = run_parser(name)
            results[name] = {
                "status": "SUCCESS",
                "result": result,
                "elapsed": round(time.time() - t, 2),
            }
            logger.info("[PARSER] %s: SUCCESS (%.2fs)", name.upper(), time.time() - t)
        except Exception as exc:
            results[name] = {
                "status": "FAILED",
                "error": str(exc),
                "elapsed": round(time.time() - t, 2),
            }
            logger.error("[PARSER] %s: FAILED: %s", name.upper(), exc, exc_info=True)

    return results


# ---------------------------------------------------------------------------
# 5. Shortage Service
# ---------------------------------------------------------------------------
def run_project_shortage_service(project_id: str) -> dict[str, Any]:
    """Run optimized Material Shortage generation for a project.

    Uses the existing services/net_shortage.py engine.
    """
    from services.net_shortage import generate_material_shortage_register

    logger.info("[NET SHORTAGE] Starting for project %s", project_id)
    result = generate_material_shortage_register(project_id)
    logger.info("[NET SHORTAGE] Project %s: %d rows in %.2fs",
                project_id, result["rows_written"], result["elapsed_seconds"])
    return result


# ---------------------------------------------------------------------------
# 6. Vendor Track Refresh
# ---------------------------------------------------------------------------
def refresh_project_vendor_track(
    project_id: str,
    db_path: Path = SERVICES_DB,
) -> int:
    """Refresh vendor tracking for the selected project.

    Uses the existing services/vendortrack_service.py.
    """
    from services.vendortrack_service import refresh_vendor_track_for_project

    logger.info("[VENDOR TRACK] Refreshing for project %s", project_id)
    rows = refresh_vendor_track_for_project(project_id, db_path=db_path)
    logger.info("[VENDOR TRACK] Project %s: %d rows", project_id, rows)
    return rows


# ---------------------------------------------------------------------------
# 7. Register Export
# ---------------------------------------------------------------------------
def export_project_shortage_register(
    project_id: str,
    db_path: Path = SERVICES_DB,
    output_dir: Path = SHORTAGE_REGISTERS_DIR,
) -> dict[str, Any]:
    """Generate the project Material Shortage Register Excel export.

    Uses the existing services/shortage_export.py.
    """
    from services.shortage_export import export_material_shortage_register

    logger.info("[REGISTER EXPORT] Exporting for project %s", project_id)
    result = export_material_shortage_register(
        project_id, db_path=db_path, output_dir=output_dir
    )
    logger.info("[REGISTER EXPORT] Exported %s: %d rows → %s",
                project_id, result["rows"], result["filename"])
    return result


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _validate_project_id(project_id: str) -> None:
    """Validate the project ID format."""
    if not project_id or not SAFE_ID.fullmatch(project_id):
        raise ValueError(
            f"Invalid project ID: {project_id!r}. "
            "Must be 1-64 characters: alphanumeric and hyphens."
        )


def _safe_resolve(base_dir: Path, *parts: str) -> Path:
    """Resolve a path safely, preventing traversal outside base_dir."""
    target = (base_dir / Path(*parts)).resolve()
    base = base_dir.resolve()
    try:
        target.relative_to(base)
    except ValueError:
        raise ValueError(
            f"Path traversal rejected: '{'/'.join(parts)}' escapes '{base}'"
        )
    return target


# ---------------------------------------------------------------------------
# MAIN ORCHESTRATOR
# ---------------------------------------------------------------------------
def process_project_reports(
    project_id: str,
    *,
    projects_dir: Path | None = None,
    global_dir: Path | None = None,
    raw_dir: Path | None = None,
    db_path: Path | None = None,
    registers_dir: Path | None = None,
) -> PipelineResult:
    """Orchestrate the complete protected project processing workflow.

    This is the single entry point for processing a project. It:
      1. Discovers project-specific and global reports
      2. Validates all source files
      3. Acquires the processing lock
      4. Safely stages reports into data/raw/
      5. Runs all seven parsers
      6. Runs the shortage engine
      7. Refreshes Vendor Track
      8. Exports the register
      9. Restores previous staging files
      10. Releases the lock

    Returns a PipelineResult with detailed status information.
    """
    from services.lock import processing_lock

    projects_dir = projects_dir or PROJECTS_DIR
    global_dir = global_dir or GLOBAL_DIR
    raw_dir = raw_dir or RAW_DIR
    db_path = db_path or SERVICES_DB
    registers_dir = registers_dir or SHORTAGE_REGISTERS_DIR

    result = PipelineResult(project_id=project_id)
    start_time = time.time()

    logger.info("=" * 60)
    logger.info("[PROCESS] Starting pipeline for project: %s", project_id)
    logger.info("=" * 60)

    # ----- Phase 1: Discovery -----
    try:
        _validate_project_id(project_id)
        result.project_reports = discover_project_reports(project_id, projects_dir)
        result.global_reports = discover_global_reports(global_dir)
    except Exception as exc:
        result.error = f"Discovery failed: {exc}"
        result.elapsed_seconds = round(time.time() - start_time, 2)
        logger.error("[DISCOVERY] %s", result.error)
        return result

    _log_discovery(result)

    # ----- Phase 2: Validation -----
    validation = validate_report_set(result.project_reports, result.global_reports)
    if not validation.valid:
        result.error = "Validation failed: " + "; ".join(validation.errors)
        result.elapsed_seconds = round(time.time() - start_time, 2)
        logger.error("[VALIDATION] %s", result.error)
        return result
    logger.info("[VALIDATION] All reports valid")

    # ----- Phase 3+: Lock-protected processing -----
    backups: dict[str, Path] = {}
    backup_dir_to_cleanup: Path | None = None
    staging_temp_dir: Path | None = None

    try:
        with processing_lock(project_id):
            result.lock_acquired = True
            logger.info("[LOCK] Processing lock acquired")

            # ----- Phase 3: Prepare staging -----
            try:
                staging_temp_dir, staged_files = prepare_staging_files(
                    result.project_reports, result.global_reports, raw_dir
                )
                result.staging_prepared = True
            except Exception as exc:
                result.error = f"Staging preparation failed: {exc}"
                logger.error("[STAGING] %s", result.error)
                return result

            # ----- Phase 4: Promote to data/raw/ -----
            try:
                backups = promote_staging_files(staged_files, raw_dir)
                # Track the backup dir for cleanup
                if backups:
                    backup_dir_to_cleanup = next(iter(backups.values())).parent
                result.staging_verified = True
                logger.info("[STAGING] All %d reports staged and verified",
                            len(staged_files))
            except Exception as exc:
                result.error = f"Staging promotion failed: {exc}"
                logger.error("[STAGING] %s", result.error)
                return result

            # ----- Phase 5: Run parsers -----
            try:
                result.parser_results = run_all_report_parsers()
                failed = [
                    name for name, r in result.parser_results.items()
                    if r["status"] != "SUCCESS"
                ]
                if failed:
                    result.all_parsers_passed = False
                    errors = "; ".join(
                        f"{n}: {result.parser_results[n].get('error', 'unknown')}"
                        for n in failed
                    )
                    result.error = f"Parser(s) failed: {errors}"
                    logger.error("[PARSERS] %s", result.error)
                    return result
                result.all_parsers_passed = True
                logger.info("[PARSERS] All %d parsers succeeded",
                            len(result.parser_results))
            except Exception as exc:
                result.error = f"Parser execution failed: {exc}"
                logger.error("[PARSERS] %s", result.error, exc_info=True)
                return result

            # ----- Phase 6: Run shortage engine -----
            try:
                result.shortage_result = run_project_shortage_service(project_id)
                result.shortage_success = True
            except Exception as exc:
                result.error = f"Shortage engine failed: {exc}"
                logger.error("[NET SHORTAGE] %s", result.error, exc_info=True)
                return result

            # ----- Phase 7: Refresh Vendor Track -----
            try:
                result.vendor_track_rows = refresh_project_vendor_track(
                    project_id, db_path=db_path
                )
                result.vendor_track_success = True
            except Exception as exc:
                result.error = f"Vendor track refresh failed: {exc}"
                logger.error("[VENDOR TRACK] %s", result.error, exc_info=True)
                return result

            # ----- Phase 8: Export register -----
            try:
                result.export_result = export_project_shortage_register(
                    project_id, db_path=db_path, output_dir=registers_dir
                )
                result.export_success = True
            except Exception as exc:
                # Export failure is non-fatal — data is in DB
                result.export_result = {"error": str(exc)}
                logger.warning("[REGISTER EXPORT] Export failed (non-fatal): %s", exc)

            result.success = True
            logger.info("[PROCESS] Pipeline completed successfully for %s",
                        project_id)

    except RuntimeError as exc:
        # Lock contention or other runtime error
        if "already in progress" in str(exc):
            result.error = str(exc)
            logger.warning("[LOCK] %s", result.error)
        else:
            result.error = f"Processing error: {exc}"
            logger.error("[PROCESS] %s", result.error, exc_info=True)
    except Exception as exc:
        result.error = f"Unexpected error: {exc}"
        logger.error("[PROCESS] %s", result.error, exc_info=True)
    finally:
        # ----- Phase 9: Restore staging -----
        if backups:
            try:
                restoration_errors = restore_previous_staging(backups, raw_dir)
                result.restoration_errors = restoration_errors
                result.restoration_success = not restoration_errors
            except Exception as exc:
                result.restoration_errors.append(f"Restoration failed: {exc}")
                result.restoration_success = False
                logger.error("[RESTORATION] %s", exc)

        # Cleanup temp dirs
        if staging_temp_dir and staging_temp_dir.exists():
            shutil.rmtree(staging_temp_dir, ignore_errors=True)
        if backup_dir_to_cleanup and backup_dir_to_cleanup.exists():
            shutil.rmtree(backup_dir_to_cleanup, ignore_errors=True)

        result.elapsed_seconds = round(time.time() - start_time, 2)
        logger.info("[PROCESS] Total time: %.2fs | Lock released", result.elapsed_seconds)

    return result


def _log_discovery(result: PipelineResult) -> None:
    """Log discovery results."""
    proj_found = sum(1 for r in result.project_reports.values() if r is not None)
    glob_found = sum(1 for r in result.global_reports.values() if r is not None)
    logger.info("[DISCOVERY] Project reports: %d/%d found",
                proj_found, len(PROJECT_REPORT_KEYS))
    logger.info("[DISCOVERY] Global reports: %d/%d found",
                glob_found, len(GLOBAL_REPORT_KEYS))
    for key, report in {**result.project_reports, **result.global_reports}.items():
        if report:
            logger.info("[DISCOVERY]   %s → %s", key, report.source_path.name)
        else:
            logger.warning("[DISCOVERY]   %s → NOT FOUND", key)


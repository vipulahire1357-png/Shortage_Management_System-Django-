"""
Optimized Material Shortage Register Engine.

Builds the project-wise Material Shortage Register by combining:
    data/zpp7_breakdown.db  — ZPP7 BOM requirements
    data/global.db          — MB25, MB52, ME2J, ME2M, PR, ScopeList
    data/services.db        — net_shortage (output), vendor_track (read for compat)

Processing steps:
    1. Build base MaterialShortage from ZPP7 + MB25
    2. Enrich with MB52 (Project Stock + Free Stock)
    3. Enrich with ME2J (PO Quantities)
    4. Enrich with PR (Requisition Quantities)
    5. Enrich with ScopeList (Final Scope)
    6. Enrich with ME2M (Vendor Detail + Proposed Date)
    7. Calculate Net Shortage = max(ZPP7 Quantity - Project Stock Quantity, 0)
    8. Preserve existing Final Date + Remarks
    9. Validate and atomically save to services.db
"""

from __future__ import annotations

import logging
import os
import sqlite3
import time
from collections import defaultdict
from contextlib import closing, contextmanager
from pathlib import Path
from typing import Any, Generator

logger = logging.getLogger("abb_shortage.net_shortage")

# ---------------------------------------------------------------------------
# Path configuration
# ---------------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = Path(os.getenv("ABB_DATA_ROOT", PROJECT_ROOT / "data")).resolve()

GLOBAL_DB_PATH = Path(os.getenv("ABB_GLOBAL_DB", DATA_DIR / "global.db")).resolve()
ZPP7_DB_PATH = Path(os.getenv("ABB_ZPP7_DB", DATA_DIR / "zpp7_breakdown.db")).resolve()
SERVICES_DB_PATH = Path(os.getenv("ABB_SERVICES_DB", DATA_DIR / "services.db")).resolve()

OUTPUT_TABLE = "net_shortage"

# ---------------------------------------------------------------------------
# Output column contract — ordered for the final net_shortage table
# ---------------------------------------------------------------------------
# The physical column order matches the business specification.
# "PO Quantity" in the DB stores "Total PO Quantity" from ME2J.
OUTPUT_COLUMNS_ORDERED = [
    "Material Ref Code",
    "Material Code",
    "WBS Element",
    "Material Description",
    "ZPP7 Quantity",
    "Project Stock Quantity",
    "Net Shortage",
    "PO Quantity",       # mapped from ME2J "Total PO Quantity"
    "Open PO Quantity",
    "Final Date",
    "Free Stock",
    "Vendor Detail",
    "Scope",             # mapped from ScopeList "Final Scope"
    "Remarks",
]

# Additional technical columns
ALL_DB_COLUMNS = ["project_id"] + OUTPUT_COLUMNS_ORDERED

# Column type mapping for CREATE TABLE
COLUMN_TYPES = {
    "project_id": "TEXT",
    "Material Ref Code": "TEXT",
    "Material Code": "TEXT",
    "WBS Element": "TEXT",
    "Material Description": "TEXT",
    "ZPP7 Quantity": "REAL",
    "Project Stock Quantity": "REAL",
    "Net Shortage": "REAL",
    "PO Quantity": "REAL",
    "Open PO Quantity": "REAL",
    "Final Date": "TEXT",
    "Free Stock": "REAL",
    "Vendor Detail": "TEXT",
    "Scope": "TEXT",
    "Remarks": "TEXT",
    "PR Quantity": "REAL",
}

# We keep PR Quantity in the schema for backward compatibility with existing
# dashboard/views queries, even though the specification doesn't list it as
# a primary output column. It goes after Remarks.
FULL_DB_COLUMNS = ALL_DB_COLUMNS + ["PR Quantity"]

# Identity key for matching existing records (preserving Final Date, Remarks)
IDENTITY_COLUMNS = ("project_id", "Material Ref Code", "Material Code", "WBS Element")


# ---------------------------------------------------------------------------
# SQLite helpers
# ---------------------------------------------------------------------------
def _q(name: str) -> str:
    """Quote an SQLite identifier."""
    return '"' + name.replace('"', '""') + '"'


def _list_tables(conn: sqlite3.Connection) -> dict[str, str]:
    """Return {lowered_name: actual_name} for all user tables."""
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    ).fetchall()
    return {str(r[0]).casefold(): str(r[0]) for r in rows}


def _resolve_table(conn: sqlite3.Connection, name: str) -> str:
    """Resolve a table name case-insensitively or raise."""
    tables = _list_tables(conn)
    actual = tables.get(name.casefold())
    if actual is None:
        available = ", ".join(sorted(tables.values())) or "(no tables)"
        raise RuntimeError(
            f"Required table {name!r} not found. Available: {available}"
        )
    return actual


def _table_columns(conn: sqlite3.Connection, table: str) -> dict[str, str]:
    """Return {lowered_col: actual_col} for a table."""
    rows = conn.execute(f"PRAGMA table_info({_q(table)})").fetchall()
    return {str(r[1]).strip().casefold(): str(r[1]) for r in rows}


def _resolve_columns(
    conn: sqlite3.Connection, table: str, required: list[str]
) -> dict[str, str]:
    """Map requested column names to their actual names."""
    available = _table_columns(conn, table)
    missing = [c for c in required if c.casefold() not in available]
    if missing:
        raise RuntimeError(
            f"Table {table!r} missing columns: {missing}. "
            f"Found: {list(available.values())}"
        )
    return {c: available[c.casefold()] for c in required}


def _clean(value: Any) -> str:
    """Normalize text for join keys."""
    if value is None:
        return ""
    s = str(value).strip()
    # MB52 stores Python None as the literal string "None"
    if s == "None":
        return ""
    return s


def _to_float(value: Any, *, context: str = "") -> float:
    """Convert a report value to float, treating blanks as zero."""
    if value is None:
        return 0.0
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    text = str(value).strip().replace(",", "").replace(" ", "")
    if not text or text == "None":
        return 0.0
    try:
        return float(text)
    except ValueError as exc:
        raise ValueError(
            f"Non-numeric value {value!r} in {context}"
        ) from exc


# ---------------------------------------------------------------------------
# STEP 1: Build base MaterialShortage from ZPP7 + MB25
# ---------------------------------------------------------------------------
def _read_zpp7_records(
    zpp7_db: Path,
) -> list[dict[str, Any]]:
    """Read all material records from the ZPP7 breakdown tables."""
    if not zpp7_db.is_file():
        raise FileNotFoundError(f"ZPP7 database not found: {zpp7_db}")

    records: list[dict[str, Any]] = []
    with closing(sqlite3.connect(zpp7_db.as_uri() + "?mode=ro", uri=True)) as conn:
        tables = _list_tables(conn)
        for folded, actual in sorted(tables.items()):
            if folded in ("parser_metadata",):
                continue
            cols = _table_columns(conn, actual)
            # Validate required columns
            if "material ref code" not in cols or "material code" not in cols:
                logger.warning("Skipping ZPP7 table %r: missing required columns", actual)
                continue
            desc_col = cols.get("description") or cols.get("material description")
            qty_col = cols.get("zpp7 quantity") or cols.get("req")
            if not desc_col or not qty_col:
                logger.warning("Skipping ZPP7 table %r: missing description or quantity column", actual)
                continue

            ref_col = cols["material ref code"]
            mat_col = cols["material code"]
            sql = (
                f"SELECT {_q(ref_col)}, {_q(mat_col)}, {_q(desc_col)}, {_q(qty_col)} "
                f"FROM {_q(actual)} ORDER BY rowid"
            )
            for row in conn.execute(sql).fetchall():
                ref_code = _clean(row[0])
                material = _clean(row[1])
                if not ref_code or not material:
                    continue
                records.append({
                    "Material Ref Code": ref_code,
                    "Material Code": material,
                    "Material Description": _clean(row[2]),
                    "ZPP7 Quantity": _to_float(
                        row[3],
                        context=f"ZPP7 {actual!r}, material {material!r}",
                    ),
                    "_zpp7_table": actual,
                })

    if not records:
        raise RuntimeError(
            f"No ZPP7 material records found in {zpp7_db}. "
            "Ensure ZPP7 parser has been run."
        )
    logger.info("[ZPP7] Read %d raw material records from %d tables", len(records), len(tables))
    return records


def _read_mb25_mappings(conn: sqlite3.Connection) -> dict[str, set[str]]:
    """Read MB25 Material Ref Code → WBS Element mappings."""
    table = _resolve_table(conn, "MB25")
    col_map = _resolve_columns(conn, table, ["Material Ref Code", "WBS Element"])

    ref_col = col_map["Material Ref Code"]
    wbs_col = col_map["WBS Element"]

    wbs_by_ref: dict[str, set[str]] = defaultdict(set)
    for row in conn.execute(
        f"SELECT {_q(ref_col)}, {_q(wbs_col)} FROM {_q(table)}"
    ).fetchall():
        ref = _clean(row[0])
        wbs = _clean(row[1])
        if ref and wbs:
            wbs_by_ref[ref].add(wbs)

    logger.info("[MB25] Read %d WBS mappings for %d Material Ref Codes",
                sum(len(v) for v in wbs_by_ref.values()), len(wbs_by_ref))
    return dict(wbs_by_ref)


def build_material_shortage(
    zpp7_db: Path,
    global_conn: sqlite3.Connection,
) -> list[dict[str, Any]]:
    """STEP 1: Join ZPP7 records with MB25 to create the base dataset.

    Returns one record per (Material Ref Code, Material Code, WBS Element) triple.
    Preserves unmatched ZPP7 records with empty WBS for diagnostics.
    Aggregates ZPP7 Quantity for duplicate (ref, material, wbs) combinations.
    """
    zpp7_records = _read_zpp7_records(zpp7_db)
    wbs_by_ref = _read_mb25_mappings(global_conn)

    # Aggregate by (ref_code, material_code, wbs) — handles duplicate BOM entries
    bom: dict[tuple[str, str, str], dict[str, Any]] = {}
    unmatched_refs: set[str] = set()

    for rec in zpp7_records:
        ref = rec["Material Ref Code"]
        mat = rec["Material Code"]
        desc = rec["Material Description"]
        qty = rec["ZPP7 Quantity"]

        mapped_wbs = wbs_by_ref.get(ref)
        if not mapped_wbs:
            # No MB25 match — retain with empty WBS for diagnostics
            unmatched_refs.add(ref)
            key = (ref, mat, "")
            if key not in bom:
                bom[key] = {
                    "Material Ref Code": ref,
                    "Material Code": mat,
                    "WBS Element": "",
                    "Material Description": desc,
                    "ZPP7 Quantity": 0.0,
                    "_mb25_matched": False,
                }
            bom[key]["ZPP7 Quantity"] += qty
            if not bom[key]["Material Description"] and desc:
                bom[key]["Material Description"] = desc
            continue

        for wbs in sorted(mapped_wbs):
            key = (ref, mat, wbs)
            if key not in bom:
                bom[key] = {
                    "Material Ref Code": ref,
                    "Material Code": mat,
                    "WBS Element": wbs,
                    "Material Description": desc,
                    "ZPP7 Quantity": 0.0,
                    "_mb25_matched": True,
                }
            bom[key]["ZPP7 Quantity"] += qty
            if not bom[key]["Material Description"] and desc:
                bom[key]["Material Description"] = desc

    if unmatched_refs:
        logger.warning(
            "[ZPP7+MB25] %d Material Ref Code(s) had no MB25 WBS mapping: %s",
            len(unmatched_refs), ", ".join(sorted(unmatched_refs)[:10]),
        )

    result = [bom[k] for k in sorted(bom.keys())]
    matched = sum(1 for r in result if r.get("_mb25_matched"))
    logger.info(
        "[ZPP7+MB25] Base dataset: %d records (%d matched, %d unmatched)",
        len(result), matched, len(result) - matched,
    )
    return result


# ---------------------------------------------------------------------------
# STEP 2: Enrich with MB52 — Project Stock + Free Stock
# ---------------------------------------------------------------------------
def enrich_with_mb52(
    material_shortage: list[dict[str, Any]],
    global_conn: sqlite3.Connection,
) -> None:
    """Add Project Stock Quantity and Free Stock from MB52.

    MB52 schema: Material Code, Description, Project Stock Quantity (TEXT), WBS Element
    - Rows with a WBS Element → Project Stock (matched by Material Code + WBS)
    - Rows without WBS (NULL or 'None') → Free Stock (matched by Material Code only)
    """
    table = _resolve_table(global_conn, "MB52")
    col_map = _resolve_columns(
        global_conn, table,
        ["Material Code", "Project Stock Quantity", "WBS Element"],
    )
    mat_col = col_map["Material Code"]
    qty_col = col_map["Project Stock Quantity"]
    wbs_col = col_map["WBS Element"]

    # Aggregate MB52 quantities
    project_stock: dict[tuple[str, str], float] = defaultdict(float)
    free_stock: dict[str, float] = defaultdict(float)
    seen_project: set[tuple[str, str]] = set()
    seen_free: set[tuple[str, str]] = set()  # Track (material, rowid) to avoid duplicates

    for row in global_conn.execute(
        f"SELECT rowid, {_q(mat_col)}, {_q(qty_col)}, {_q(wbs_col)} "
        f"FROM {_q(table)} ORDER BY rowid"
    ).fetchall():
        rowid = row[0]
        material = _clean(row[1])
        qty = _to_float(row[2], context=f"MB52 rowid {rowid}, material {material!r}")
        wbs = _clean(row[3])

        if not material:
            continue

        if wbs:
            # Project-specific stock
            key = (material, wbs)
            if key not in seen_project:
                seen_project.add(key)
                project_stock[key] += qty
            else:
                project_stock[key] += qty
        else:
            # Free stock (no WBS)
            free_stock[material] += qty

    # Apply to material shortage records
    ps_match = fs_match = 0
    for rec in material_shortage:
        mat = rec["Material Code"]
        wbs = rec["WBS Element"]

        # Project Stock
        ps_key = (mat, wbs)
        if wbs and ps_key in project_stock:
            rec["Project Stock Quantity"] = project_stock[ps_key]
            ps_match += 1
        else:
            rec["Project Stock Quantity"] = 0.0

        # Free Stock — only if material has no project-specific stock match
        if mat in free_stock:
            rec["Free Stock"] = free_stock[mat]
            fs_match += 1
        else:
            rec["Free Stock"] = 0.0

    logger.info(
        "[MB52] Project stock matches: %d, Free stock matches: %d, "
        "Unmatched: %d",
        ps_match, fs_match,
        len(material_shortage) - ps_match,
    )


# ---------------------------------------------------------------------------
# STEP 3: Enrich with ME2J — Purchase Order quantities
# ---------------------------------------------------------------------------
def enrich_with_me2j(
    material_shortage: list[dict[str, Any]],
    global_conn: sqlite3.Connection,
) -> None:
    """Add PO Quantity (Total PO Quantity) and Open PO Quantity from ME2J.

    ME2J schema: WBS Element, Material Code, Description,
                 Total PO Quantity (TEXT), Open PO Quantity (TEXT)
    """
    table = _resolve_table(global_conn, "ME2J")
    col_map = _resolve_columns(
        global_conn, table,
        ["Material Code", "WBS Element", "Total PO Quantity", "Open PO Quantity"],
    )
    mat_col = col_map["Material Code"]
    wbs_col = col_map["WBS Element"]
    po_col = col_map["Total PO Quantity"]
    open_col = col_map["Open PO Quantity"]

    # Aggregate ME2J at (Material Code, WBS Element) level
    po_totals: dict[tuple[str, str], float] = defaultdict(float)
    open_totals: dict[tuple[str, str], float] = defaultdict(float)
    lines_read = 0

    for row in global_conn.execute(
        f"SELECT {_q(mat_col)}, {_q(wbs_col)}, {_q(po_col)}, {_q(open_col)} "
        f"FROM {_q(table)}"
    ).fetchall():
        material = _clean(row[0])
        wbs = _clean(row[1])
        if not material or not wbs:
            continue
        lines_read += 1
        key = (material, wbs)
        po_totals[key] += _to_float(
            row[2], context=f"ME2J Total PO, material {material!r}, WBS {wbs!r}"
        )
        open_totals[key] += _to_float(
            row[3], context=f"ME2J Open PO, material {material!r}, WBS {wbs!r}"
        )

    # Apply
    matched = 0
    for rec in material_shortage:
        key = (rec["Material Code"], rec["WBS Element"])
        if key in po_totals:
            rec["PO Quantity"] = po_totals[key]
            rec["Open PO Quantity"] = open_totals[key]
            matched += 1
        else:
            rec["PO Quantity"] = 0.0
            rec["Open PO Quantity"] = 0.0

    logger.info(
        "[ME2J] PO lines aggregated: %d → %d groups, matched: %d records",
        lines_read, len(po_totals), matched,
    )


# ---------------------------------------------------------------------------
# STEP 4: Enrich with PR — Purchase Requisition quantities
# ---------------------------------------------------------------------------
def enrich_with_pr(
    material_shortage: list[dict[str, Any]],
    global_conn: sqlite3.Connection,
) -> None:
    """Add PR Quantity from the PR table.

    PR schema: WBS Element, Material Code, Description, PR Quantity (TEXT)
    """
    table = _resolve_table(global_conn, "PR")
    col_map = _resolve_columns(
        global_conn, table,
        ["Material Code", "WBS Element", "PR Quantity"],
    )
    mat_col = col_map["Material Code"]
    wbs_col = col_map["WBS Element"]
    pr_col = col_map["PR Quantity"]

    # Aggregate PR at (Material Code, WBS Element) level
    pr_totals: dict[tuple[str, str], float] = defaultdict(float)
    lines_read = 0

    for row in global_conn.execute(
        f"SELECT {_q(mat_col)}, {_q(wbs_col)}, {_q(pr_col)} FROM {_q(table)}"
    ).fetchall():
        material = _clean(row[0])
        wbs = _clean(row[1])
        if not material or not wbs:
            continue
        lines_read += 1
        pr_totals[(material, wbs)] += _to_float(
            row[2], context=f"PR, material {material!r}, WBS {wbs!r}"
        )

    matched = 0
    for rec in material_shortage:
        key = (rec["Material Code"], rec["WBS Element"])
        if key in pr_totals:
            rec["PR Quantity"] = pr_totals[key]
            matched += 1
        else:
            rec["PR Quantity"] = 0.0

    logger.info(
        "[PR] PR lines aggregated: %d → %d groups, matched: %d records",
        lines_read, len(pr_totals), matched,
    )


# ---------------------------------------------------------------------------
# STEP 5: Enrich with ScopeList — Final Scope
# ---------------------------------------------------------------------------
def enrich_with_scope(
    material_shortage: list[dict[str, Any]],
    global_conn: sqlite3.Connection,
) -> None:
    """Add Final Scope from ScopeList.

    ScopeList schema: Material Code, Description, Final Scope (TEXT)
    Priority: 'ABB' > 'Vendor Scope' > others.
    """
    table = _resolve_table(global_conn, "ScopeList")
    col_map = _resolve_columns(
        global_conn, table, ["Material Code", "Final Scope"],
    )
    mat_col = col_map["Material Code"]
    scope_col = col_map["Final Scope"]

    # Scope priority: lower number = higher priority
    SCOPE_PRIORITY = {"ABB": 0, "VENDOR SCOPE": 1}

    material_scope: dict[str, str] = {}
    for row in global_conn.execute(
        f"SELECT {_q(mat_col)}, {_q(scope_col)} FROM {_q(table)}"
    ).fetchall():
        material = _clean(row[0])
        scope = _clean(row[1])
        if not material or not scope:
            continue
        scope_upper = scope.upper()
        if scope_upper not in SCOPE_PRIORITY:
            continue
        existing = material_scope.get(material)
        if existing is None or SCOPE_PRIORITY[scope_upper] < SCOPE_PRIORITY.get(
            existing.upper(), 99
        ):
            material_scope[material] = scope

    matched = conflicting = 0
    for rec in material_shortage:
        mat = rec["Material Code"]
        if mat in material_scope:
            rec["Scope"] = material_scope[mat]
            matched += 1
        else:
            rec["Scope"] = ""

    logger.info(
        "[SCOPE] Scope records matched: %d of %d, unique materials with scope: %d",
        matched, len(material_shortage), len(material_scope),
    )


# ---------------------------------------------------------------------------
# STEP 6: Enrich with ME2M — Vendor Detail + Proposed Date
# ---------------------------------------------------------------------------
class VendorResolution:
    """Result of vendor resolution for a material+WBS combination.

    Attributes:
        vendor: Selected vendor string if uniquely resolved, else None.
        has_conflict: True if multiple distinct vendors exist.
        status: 'SINGLE_VENDOR', 'MULTIPLE_VENDORS' (WBS-resolved),
                'ALLOCATION_UNRESOLVED', or 'VENDOR_UNAVAILABLE'.
        vendors: All distinct vendor names found.
        proposed_date: Latest proposed date if available.
    """
    __slots__ = ("vendor", "has_conflict", "status", "vendors", "proposed_date")

    def __init__(
        self,
        vendor: str | None,
        has_conflict: bool,
        status: str,
        vendors: list[str],
        proposed_date: str = "",
    ):
        self.vendor = vendor
        self.has_conflict = has_conflict
        self.status = status
        self.vendors = vendors
        self.proposed_date = proposed_date


def _resolve_vendor(
    records: list[dict[str, Any]],
    wbs: str,
) -> VendorResolution:
    """Resolve vendor for a material+WBS from ME2M records.

    Never arbitrarily selects the first vendor.
    """
    # Filter to records matching the WBS
    wbs_lower = wbs.casefold()
    wbs_records = [
        r for r in records
        if _clean(r.get("WBS Element", "")).casefold() == wbs_lower
    ] if wbs else []

    # If WBS-specific records exist, use those; otherwise fall back to all
    source = wbs_records if wbs_records else records

    # Collect unique vendors
    seen: set[str] = set()
    unique_vendors: list[str] = []
    latest_date = ""

    for rec in source:
        vendor = " ".join(_clean(rec.get("Vendor Detail", "")).split())
        v_id = vendor.casefold()
        if vendor and v_id not in seen:
            seen.add(v_id)
            unique_vendors.append(vendor)
        # Track latest proposed date
        pd = _clean(rec.get("Proposed Date", ""))
        if pd and pd > latest_date:
            latest_date = pd

    if not unique_vendors:
        return VendorResolution(None, False, "VENDOR_UNAVAILABLE", [], "")

    if len(unique_vendors) == 1:
        return VendorResolution(
            unique_vendors[0], False, "SINGLE_VENDOR",
            unique_vendors, latest_date,
        )

    # Multiple vendors — if we narrowed by WBS and got one, that's resolved
    if wbs_records:
        wbs_vendors: list[str] = []
        wbs_seen: set[str] = set()
        for rec in wbs_records:
            v = " ".join(_clean(rec.get("Vendor Detail", "")).split())
            v_id = v.casefold()
            if v and v_id not in wbs_seen:
                wbs_seen.add(v_id)
                wbs_vendors.append(v)
        if len(wbs_vendors) == 1:
            return VendorResolution(
                wbs_vendors[0], False, "MULTIPLE_VENDORS",
                unique_vendors, latest_date,
            )

    # Cannot determine — do NOT guess
    return VendorResolution(
        None, True, "ALLOCATION_UNRESOLVED",
        unique_vendors, latest_date,
    )


def enrich_with_me2m(
    material_shortage: list[dict[str, Any]],
    global_conn: sqlite3.Connection,
) -> None:
    """Add Vendor Detail from ME2M. Stores Proposed Date for vendor track.

    ME2M schema: Material Code, Description, Vendor Detail, WBS Element, Proposed Date
    """
    table = _resolve_table(global_conn, "ME2M")
    cols = _table_columns(global_conn, table)

    # Read only the columns we need
    read_cols = ["Material Code", "Vendor Detail"]
    if "wbs element" in cols:
        read_cols.append("WBS Element")
    if "proposed date" in cols:
        read_cols.append("Proposed Date")

    col_map = _resolve_columns(global_conn, table, read_cols)
    select = ", ".join(_q(col_map[c]) for c in read_cols)

    # Group ME2M records by Material Code
    me2m_by_material: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in global_conn.execute(
        f"SELECT {select} FROM {_q(table)}"
    ).fetchall():
        material = _clean(row[0])
        if not material:
            continue
        rec: dict[str, Any] = {"Material Code": material, "Vendor Detail": _clean(row[1])}
        idx = 2
        if "WBS Element" in read_cols:
            rec["WBS Element"] = _clean(row[idx])
            idx += 1
        if "Proposed Date" in read_cols:
            rec["Proposed Date"] = _clean(row[idx])
        me2m_by_material[material].append(rec)

    logger.info("[ME2M] Read %d records for %d materials",
                sum(len(v) for v in me2m_by_material.values()),
                len(me2m_by_material))

    matched = multi_vendor = 0
    for rec in material_shortage:
        mat = rec["Material Code"]
        wbs = rec["WBS Element"]

        me2m_records = me2m_by_material.get(mat)
        if not me2m_records:
            rec["Vendor Detail"] = ""
            rec["_proposed_date"] = ""
            continue

        resolution = _resolve_vendor(me2m_records, wbs)
        matched += 1

        if resolution.status == "SINGLE_VENDOR":
            rec["Vendor Detail"] = resolution.vendor or ""
        elif resolution.status == "MULTIPLE_VENDORS":
            rec["Vendor Detail"] = resolution.vendor or ""
        elif resolution.status == "ALLOCATION_UNRESOLVED":
            # Preserve all vendor names for visibility
            rec["Vendor Detail"] = f"[Unresolved] {' | '.join(resolution.vendors)}"
            multi_vendor += 1
        else:
            rec["Vendor Detail"] = ""

        rec["_proposed_date"] = resolution.proposed_date
        rec["_vendor_resolution"] = resolution

    logger.info(
        "[ME2M] Vendor matches: %d, Multi-vendor unresolved: %d",
        matched, multi_vendor,
    )


# ---------------------------------------------------------------------------
# STEP 7: Calculate Net Shortage
# ---------------------------------------------------------------------------
def calculate_net_shortage(material_shortage: list[dict[str, Any]]) -> None:
    """Net Shortage = max(ZPP7 Quantity - Project Stock Quantity, 0).

    Rules:
    - Negative results become zero.
    - Free Stock is NOT subtracted.
    - PO and Open PO quantities are NOT subtracted.
    """
    for rec in material_shortage:
        zpp7 = rec.get("ZPP7 Quantity", 0.0) or 0.0
        stock = rec.get("Project Stock Quantity", 0.0) or 0.0
        rec["Net Shortage"] = max(zpp7 - stock, 0.0)


# ---------------------------------------------------------------------------
# STEP 8: Preserve existing Final Date + Remarks
# ---------------------------------------------------------------------------
def preserve_existing_fields(
    material_shortage: list[dict[str, Any]],
    project_id: str,
    services_conn: sqlite3.Connection,
) -> None:
    """Read existing Final Date and Remarks from net_shortage and apply to
    matching records. Identity key: (project_id, Material Ref Code, Material Code, WBS Element).

    Existing Remarks are preserved. System-generated warning remarks (from
    vendor resolution) are only applied to records that don't have user remarks.
    """
    tables = _list_tables(services_conn)
    if OUTPUT_TABLE.casefold() not in tables:
        # No existing data
        for rec in material_shortage:
            rec.setdefault("Final Date", "")
            rec.setdefault("Remarks", "")
        return

    # Read existing records
    existing: dict[tuple[str, str, str], dict[str, str]] = {}
    try:
        rows = services_conn.execute(
            f'SELECT "Material Ref Code", "Material Code", "WBS Element", '
            f'"Final Date", "Remarks" FROM {_q(OUTPUT_TABLE)} '
            f'WHERE "project_id" = ?',
            (project_id,),
        ).fetchall()
        for row in rows:
            ref = _clean(row[0])
            mat = _clean(row[1])
            wbs = _clean(row[2])
            fdate = _clean(row[3])
            remarks = _clean(row[4])
            # Skip "None" strings stored as literal values
            if fdate == "None":
                fdate = ""
            if remarks == "None":
                remarks = ""
            key = (ref, mat, wbs)
            existing[key] = {"Final Date": fdate, "Remarks": remarks}
    except Exception:
        logger.warning("Could not read existing Final Date/Remarks", exc_info=True)

    preserved_dates = preserved_remarks = 0
    for rec in material_shortage:
        key = (rec["Material Ref Code"], rec["Material Code"], rec["WBS Element"])
        old = existing.get(key)

        if old:
            # Preserve Final Date — never overwritten by processing
            if old["Final Date"]:
                rec["Final Date"] = old["Final Date"]
                preserved_dates += 1
            else:
                rec.setdefault("Final Date", "")

            # Preserve user Remarks — only overwrite if old was a system warning
            old_remarks = old["Remarks"]
            is_system_remark = old_remarks.startswith("[WARNING:")
            if old_remarks and not is_system_remark:
                rec["Remarks"] = old_remarks
                preserved_remarks += 1
            elif not rec.get("Remarks"):
                # Generate system remarks for unresolved vendors
                resolution = rec.get("_vendor_resolution")
                if resolution and resolution.status == "ALLOCATION_UNRESOLVED":
                    rec["Remarks"] = (
                        f"[WARNING: Allocation Unresolved — Multiple ME2M vendors: "
                        f"{', '.join(resolution.vendors)}]"
                    )
                else:
                    rec["Remarks"] = ""
        else:
            rec.setdefault("Final Date", "")
            # Generate system remarks for new unresolved vendor records
            if not rec.get("Remarks"):
                resolution = rec.get("_vendor_resolution")
                if resolution and resolution.status == "ALLOCATION_UNRESOLVED":
                    rec["Remarks"] = (
                        f"[WARNING: Allocation Unresolved — Multiple ME2M vendors: "
                        f"{', '.join(resolution.vendors)}]"
                    )
                else:
                    rec["Remarks"] = ""

    logger.info(
        "[PRESERVE] Final Dates preserved: %d, User Remarks preserved: %d",
        preserved_dates, preserved_remarks,
    )


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------
def validate_material_shortage(
    material_shortage: list[dict[str, Any]],
    project_id: str,
) -> dict[str, Any]:
    """Validate the final dataset before writing. Returns diagnostics dict."""
    diagnostics: dict[str, Any] = {
        "total_records": len(material_shortage),
        "matched_records": 0,
        "unmatched_records": 0,
        "duplicate_keys": [],
        "null_quantities": 0,
        "negative_net_shortage": 0,
        "valid": True,
        "errors": [],
    }

    seen_keys: set[tuple[str, str, str]] = set()
    for rec in material_shortage:
        ref = rec.get("Material Ref Code", "")
        mat = rec.get("Material Code", "")
        wbs = rec.get("WBS Element", "")

        if rec.get("_mb25_matched"):
            diagnostics["matched_records"] += 1
        else:
            diagnostics["unmatched_records"] += 1

        # Check for duplicates
        key = (ref, mat, wbs)
        if key in seen_keys:
            diagnostics["duplicate_keys"].append(key)
        seen_keys.add(key)

        # Check quantities
        if rec.get("ZPP7 Quantity") is None:
            diagnostics["null_quantities"] += 1

        ns = rec.get("Net Shortage", 0)
        if ns is not None and ns < 0:
            diagnostics["negative_net_shortage"] += 1

    if diagnostics["duplicate_keys"]:
        diagnostics["valid"] = False
        diagnostics["errors"].append(
            f"{len(diagnostics['duplicate_keys'])} duplicate (ref, material, wbs) keys found"
        )

    if diagnostics["negative_net_shortage"] > 0:
        diagnostics["errors"].append(
            f"{diagnostics['negative_net_shortage']} records with negative Net Shortage"
        )

    if not material_shortage:
        diagnostics["valid"] = False
        diagnostics["errors"].append("No records to write")

    return diagnostics


# ---------------------------------------------------------------------------
# Schema management
# ---------------------------------------------------------------------------
def _ensure_net_shortage_schema(conn: sqlite3.Connection) -> None:
    """Create or migrate net_shortage table to the required schema.

    Preserves existing data during migration. Handles column ordering changes.
    """
    tables = _list_tables(conn)

    # Build expected schema
    ordered_types: dict[str, str] = {}
    for col in FULL_DB_COLUMNS:
        ordered_types[col] = COLUMN_TYPES.get(col, "TEXT")

    if OUTPUT_TABLE.casefold() not in tables:
        # Create new table
        defs = ", ".join(
            f"{_q(col)} {kind}" for col, kind in ordered_types.items()
        )
        conn.execute(f"CREATE TABLE {_q(OUTPUT_TABLE)} ({defs})")
        # Create indexes for common query patterns
        conn.execute(
            f'CREATE INDEX IF NOT EXISTS idx_net_shortage_project '
            f'ON {_q(OUTPUT_TABLE)} ("project_id")'
        )
        conn.execute(
            f'CREATE INDEX IF NOT EXISTS idx_net_shortage_identity '
            f'ON {_q(OUTPUT_TABLE)} ("project_id", "Material Ref Code", '
            f'"Material Code", "WBS Element")'
        )
        return

    # Table exists — check if migration needed
    actual_table = tables[OUTPUT_TABLE.casefold()]
    actual_info = conn.execute(
        f"PRAGMA table_info({_q(actual_table)})"
    ).fetchall()
    actual_names = [str(row[1]) for row in actual_info]
    expected_names = list(ordered_types.keys())

    # Add any extra columns from the existing schema that we don't know about
    extra_cols = {}
    actual_lower_set = {n.casefold() for n in expected_names}
    for row in actual_info:
        col_name = str(row[1])
        if col_name.casefold() not in actual_lower_set:
            extra_cols[col_name] = str(row[2]) or "TEXT"

    full_schema = {**ordered_types, **extra_cols}

    # Check if column order matches
    if actual_names == list(full_schema.keys()):
        # Schema matches, just ensure indexes
        conn.execute(
            f'CREATE INDEX IF NOT EXISTS idx_net_shortage_project '
            f'ON {_q(OUTPUT_TABLE)} ("project_id")'
        )
        conn.execute(
            f'CREATE INDEX IF NOT EXISTS idx_net_shortage_identity '
            f'ON {_q(OUTPUT_TABLE)} ("project_id", "Material Ref Code", '
            f'"Material Code", "WBS Element")'
        )
        return

    # Need to rebuild table to reorder columns
    logger.info("Migrating net_shortage schema (column order change)")
    temp_table = f"{OUTPUT_TABLE}__migration"
    conn.execute(f"DROP TABLE IF EXISTS {_q(temp_table)}")

    defs = ", ".join(
        f"{_q(col)} {kind}" for col, kind in full_schema.items()
    )
    conn.execute(f"CREATE TABLE {_q(temp_table)} ({defs})")

    # Copy data from old to new
    actual_lower_map = {name.casefold(): name for name in actual_names}
    cols_to_copy = [
        (name, actual_lower_map[name.casefold()])
        for name in full_schema
        if name.casefold() in actual_lower_map
    ]
    if cols_to_copy:
        target_cols = ", ".join(_q(t) for t, _ in cols_to_copy)
        source_cols = ", ".join(_q(s) for _, s in cols_to_copy)
        conn.execute(
            f"INSERT INTO {_q(temp_table)} ({target_cols}) "
            f"SELECT {source_cols} FROM {_q(actual_table)}"
        )

    conn.execute(f"DROP TABLE {_q(actual_table)}")
    conn.execute(
        f"ALTER TABLE {_q(temp_table)} RENAME TO {_q(OUTPUT_TABLE)}"
    )

    # Create indexes
    conn.execute(
        f'CREATE INDEX IF NOT EXISTS idx_net_shortage_project '
        f'ON {_q(OUTPUT_TABLE)} ("project_id")'
    )
    conn.execute(
        f'CREATE INDEX IF NOT EXISTS idx_net_shortage_identity '
        f'ON {_q(OUTPUT_TABLE)} ("project_id", "Material Ref Code", '
        f'"Material Code", "WBS Element")'
    )


# ---------------------------------------------------------------------------
# STEP 9: Save to database
# ---------------------------------------------------------------------------
def save_net_shortage(
    material_shortage: list[dict[str, Any]],
    project_id: str,
    db_path: Path = SERVICES_DB_PATH,
) -> int:
    """Atomically replace project records in net_shortage.

    1. Uses a staging approach: inserts to temp, validates, then swaps
    2. Only deletes THIS project's rows
    3. Preserves all other projects
    4. Returns count of rows written
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)

    with sqlite3.connect(db_path) as conn:
        conn.execute("PRAGMA busy_timeout = 5000")
        conn.execute("PRAGMA journal_mode = WAL")

        with conn:  # transaction
            _ensure_net_shortage_schema(conn)

            # Count existing rows for this project (for logging)
            try:
                old_count = conn.execute(
                    f'SELECT COUNT(*) FROM {_q(OUTPUT_TABLE)} WHERE "project_id" = ?',
                    (project_id,),
                ).fetchone()[0]
            except Exception:
                old_count = 0

            # Delete only this project's rows
            conn.execute(
                f'DELETE FROM {_q(OUTPUT_TABLE)} WHERE "project_id" = ?',
                (project_id,),
            )

            # Verify other projects untouched
            other_count_before = conn.execute(
                f'SELECT COUNT(*) FROM {_q(OUTPUT_TABLE)} WHERE "project_id" != ? OR "project_id" IS NULL',
                (project_id,),
            ).fetchone()[0]

            # Build insert data
            insert_cols = list(FULL_DB_COLUMNS)
            quoted_cols = ", ".join(_q(c) for c in insert_cols)
            placeholders = ", ".join("?" for _ in insert_cols)

            rows_to_insert: list[tuple[Any, ...]] = []
            for rec in material_shortage:
                row_vals = []
                for col in insert_cols:
                    if col == "project_id":
                        row_vals.append(project_id)
                    else:
                        val = rec.get(col)
                        # Clean up internal keys
                        if val is None and col in COLUMN_TYPES:
                            if COLUMN_TYPES[col] == "REAL":
                                val = 0.0
                            else:
                                val = ""
                        row_vals.append(val)
                rows_to_insert.append(tuple(row_vals))

            if rows_to_insert:
                conn.executemany(
                    f"INSERT INTO {_q(OUTPUT_TABLE)} ({quoted_cols}) VALUES ({placeholders})",
                    rows_to_insert,
                )

            # Verify other projects still untouched
            other_count_after = conn.execute(
                f'SELECT COUNT(*) FROM {_q(OUTPUT_TABLE)} WHERE "project_id" != ? OR "project_id" IS NULL',
                (project_id,),
            ).fetchone()[0]

            if other_count_after != other_count_before:
                raise RuntimeError(
                    f"Project isolation violation: other project rows changed "
                    f"({other_count_before} → {other_count_after})"
                )

    logger.info(
        "[DATABASE] Project %s: %d old rows deleted, %d new rows inserted. "
        "Other project rows: %d (unchanged)",
        project_id, old_count, len(rows_to_insert), other_count_after,
    )
    return len(rows_to_insert)


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------
def generate_material_shortage_register(
    project_id: str,
    *,
    global_db: Path | None = None,
    zpp7_db: Path | None = None,
    services_db: Path | None = None,
) -> dict[str, Any]:
    """Orchestrate the complete Material Shortage Register generation.

    This is the single authoritative entry point for shortage generation.

    Args:
        project_id: The project identifier (e.g. 'IN-10026042').
        global_db: Path to global.db (default from env/config).
        zpp7_db: Path to zpp7_breakdown.db (default from env/config).
        services_db: Path to services.db (default from env/config).

    Returns:
        Dict with processing results and diagnostics.
    """
    global_db = global_db or GLOBAL_DB_PATH
    zpp7_db = zpp7_db or ZPP7_DB_PATH
    services_db = services_db or SERVICES_DB_PATH

    start_time = time.time()
    logger.info("[MATERIAL SHORTAGE] Starting project: %s", project_id)

    # Validate source databases exist
    for name, path in [("global.db", global_db), ("zpp7_breakdown.db", zpp7_db)]:
        if not path.is_file():
            raise FileNotFoundError(f"Source database {name} not found: {path}")

    logger.info("[SCHEMA VALIDATION] global.db: %s, zpp7.db: %s, services.db: %s",
                global_db, zpp7_db, services_db)

    # Open read-only connection to global.db
    with closing(sqlite3.connect(
        global_db.as_uri() + "?mode=ro", uri=True
    )) as global_conn:
        global_conn.execute("PRAGMA query_only = ON")

        # Validate required tables exist
        global_tables = _list_tables(global_conn)
        required_tables = ["MB25", "MB52", "ME2J", "ME2M", "PR", "ScopeList"]
        missing = [t for t in required_tables if t.casefold() not in global_tables]
        if missing:
            raise RuntimeError(
                f"Missing required tables in global.db: {missing}. "
                f"Available: {list(global_tables.values())}"
            )

        t1 = time.time()
        # STEP 1: Build base from ZPP7 + MB25
        material_shortage = build_material_shortage(zpp7_db, global_conn)
        logger.info("[TIMING] Step 1 (ZPP7+MB25): %.2fs", time.time() - t1)

        t2 = time.time()
        # STEP 2: Enrich with MB52
        enrich_with_mb52(material_shortage, global_conn)
        logger.info("[TIMING] Step 2 (MB52): %.2fs", time.time() - t2)

        t3 = time.time()
        # STEP 3: Enrich with ME2J
        enrich_with_me2j(material_shortage, global_conn)
        logger.info("[TIMING] Step 3 (ME2J): %.2fs", time.time() - t3)

        t4 = time.time()
        # STEP 4: Enrich with PR
        enrich_with_pr(material_shortage, global_conn)
        logger.info("[TIMING] Step 4 (PR): %.2fs", time.time() - t4)

        t5 = time.time()
        # STEP 5: Enrich with ScopeList
        enrich_with_scope(material_shortage, global_conn)
        logger.info("[TIMING] Step 5 (Scope): %.2fs", time.time() - t5)

        t6 = time.time()
        # STEP 6: Enrich with ME2M
        enrich_with_me2m(material_shortage, global_conn)
        logger.info("[TIMING] Step 6 (ME2M): %.2fs", time.time() - t6)

    t7 = time.time()
    # STEP 7: Calculate Net Shortage
    calculate_net_shortage(material_shortage)
    logger.info("[TIMING] Step 7 (Calculate): %.2fs", time.time() - t7)

    t8 = time.time()
    # STEP 8: Preserve existing Final Date + Remarks
    with closing(sqlite3.connect(
        services_db.as_uri() + "?mode=ro", uri=True
    )) if services_db.is_file() else _null_conn() as svc_conn:
        preserve_existing_fields(material_shortage, project_id, svc_conn)
    logger.info("[TIMING] Step 8 (Preserve): %.2fs", time.time() - t8)

    # Validation
    diagnostics = validate_material_shortage(material_shortage, project_id)
    if diagnostics["errors"]:
        for err in diagnostics["errors"]:
            logger.error("[VALIDATION] %s", err)
    if not diagnostics["valid"]:
        raise RuntimeError(
            f"Validation failed: {'; '.join(diagnostics['errors'])}"
        )

    logger.info(
        "[VALIDATION] Total: %d, Matched: %d, Unmatched: %d, "
        "Duplicates: %d, Net shortage > 0: %d",
        diagnostics["total_records"],
        diagnostics["matched_records"],
        diagnostics["unmatched_records"],
        len(diagnostics["duplicate_keys"]),
        sum(1 for r in material_shortage if (r.get("Net Shortage") or 0) > 0),
    )

    t9 = time.time()
    # STEP 9: Save to database
    rows_written = save_net_shortage(material_shortage, project_id, services_db)
    logger.info("[TIMING] Step 9 (Save): %.2fs", time.time() - t9)

    elapsed = time.time() - start_time
    logger.info(
        "[MATERIAL SHORTAGE] Completed project %s: %d records in %.2fs",
        project_id, rows_written, elapsed,
    )

    return {
        "project_id": project_id,
        "rows_written": rows_written,
        "diagnostics": diagnostics,
        "elapsed_seconds": round(elapsed, 2),
    }


@contextmanager
def _null_conn() -> Generator[sqlite3.Connection, None, None]:
    """Provide an in-memory connection for when services.db doesn't exist yet."""
    conn = sqlite3.connect(":memory:")
    try:
        yield conn
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Backward-compatible entry point (replaces shortage_service.main)
# ---------------------------------------------------------------------------
def main(project_id: str = "") -> int:
    """CLI entry point, compatible with the existing main.py interface."""
    import sys

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )

    try:
        print("Building ABB net_shortage table...")
        print(f"Global database: {GLOBAL_DB_PATH}")
        print(f"ZPP7 database:   {ZPP7_DB_PATH}")
        print(f"Output database: {SERVICES_DB_PATH}")
        if project_id:
            print(f"Project ID:      {project_id}")

        result = generate_material_shortage_register(
            project_id,
            global_db=GLOBAL_DB_PATH,
            zpp7_db=ZPP7_DB_PATH,
            services_db=SERVICES_DB_PATH,
        )

        print(f"\nRows written: {result['rows_written']}")
        print(f"Elapsed: {result['elapsed_seconds']}s")

        # Preview
        with sqlite3.connect(SERVICES_DB_PATH) as conn:
            preview = conn.execute(
                f"SELECT * FROM {_q(OUTPUT_TABLE)} "
                f"WHERE \"project_id\" = ? LIMIT 5",
                (project_id,),
            ).fetchall()
            if preview:
                print("\nPreview:")
                for row in preview:
                    print(row)

        return 0
    except Exception as error:
        print(f"Shortage service failed: {error}", file=sys.stderr)
        logger.exception("Shortage service failed")
        return 1


if __name__ == "__main__":
    import sys
    import argparse

    parser = argparse.ArgumentParser(description="Generate Material Shortage Register")
    parser.add_argument("--project-id", default="", help="Project ID")
    args = parser.parse_args()
    raise SystemExit(main(project_id=args.project_id))


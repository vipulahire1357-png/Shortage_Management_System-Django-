"""
Build the ABB net-shortage table from the existing report databases.

Inputs:
    data/global.db
        - wbs_element
        - scopelist
        - Storage
        - ME2J
        - ME2M
        - PR
    data/zpp7_breakdown.db
        - one table per Material Ref Code, plus parser_metadata

Output:
    data/services.db -> net_shortage

The source databases are read only. Only net_shortage is refreshed.
"""

from __future__ import annotations

import os
import sqlite3
import sys
from contextlib import closing
from collections import defaultdict
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = Path(os.getenv("ABB_DATA_ROOT", PROJECT_ROOT / "data")).resolve()

GLOBAL_DB_PATH = Path(os.getenv("ABB_GLOBAL_DB", DATA_DIR / "global.db")).resolve()
ZPP7_DB_PATH = Path(os.getenv("ABB_ZPP7_DB", DATA_DIR / "zpp7_breakdown.db")).resolve()
SERVICES_DB_PATH = Path(os.getenv("ABB_SERVICES_DB", DATA_DIR / "services.db")).resolve()

OUTPUT_TABLE = "net_shortage"

OUTPUT_COLUMNS = [
    "Material Code",
    "Material Description",
    "ZPP7 Quantity",
    "Project Stock Quantity",
    "PO Quantity",
    "Open PO Quantity",
    "PR Quantity",
    "Net Shortage",
    "Free Stock",
    "Vendor Detail",
    "Final Date",
    "Remarks",
    # Retained as supporting fields for sidebar filters; dashboard hides them.
    "Material Ref Code",
    "WBS Element",
    "Scope",
]

# Columns written during build_rows (excludes project_id which is added at write time)
BUILD_COLUMNS = [
    "Material Code",
    "Material Description",
    "ZPP7 Quantity",
    "Project Stock Quantity",
    "PO Quantity",
    "Open PO Quantity",
    "PR Quantity",
    "Net Shortage",
    "Free Stock",
    "Vendor Detail",
    "Remarks",
    "Material Ref Code",
    "WBS Element",
    "Scope",
]


def quote_identifier(value: str) -> str:
    """Safely quote an SQLite table or column name."""
    return '"' + value.replace('"', '""') + '"'


def list_tables(connection: sqlite3.Connection) -> dict[str, str]:
    """Return a case-insensitive map of table names to actual names."""
    rows = connection.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
    ).fetchall()
    return {str(row[0]).casefold(): str(row[0]) for row in rows}


def resolve_table(connection: sqlite3.Connection, requested_name: str) -> str:
    """Resolve a table name case-insensitively or raise a useful error."""
    tables = list_tables(connection)
    actual_name = tables.get(requested_name.casefold())
    if actual_name is None:
        available = ", ".join(sorted(tables.values())) or "(no tables found)"
        raise RuntimeError(
            f"Required table {requested_name!r} was not found in the database. "
            f"Available tables: {available}"
        )
    return actual_name


def table_columns(
    connection: sqlite3.Connection, table_name: str
) -> dict[str, str]:
    """Return a case-insensitive map of column names to actual names."""
    rows = connection.execute(
        f"PRAGMA table_info({quote_identifier(table_name)})"
    ).fetchall()
    return {str(row[1]).strip().casefold(): str(row[1]) for row in rows}


def resolve_columns(
    connection: sqlite3.Connection,
    table_name: str,
    required_columns: list[str],
) -> dict[str, str]:
    """Resolve required columns case-insensitively."""
    existing = table_columns(connection, table_name)
    missing = [name for name in required_columns if name.casefold() not in existing]
    if missing:
        raise RuntimeError(
            f"Table {table_name!r} is missing required columns: "
            f"{', '.join(missing)}. Found: {', '.join(existing.values())}"
        )
    return {name: existing[name.casefold()] for name in required_columns}


def read_table(
    connection: sqlite3.Connection,
    table_name: str,
    columns: list[str],
    *,
    order_by_rowid: bool = False,
) -> list[dict[str, Any]]:
    """Read selected columns into dictionaries using actual database names."""
    resolved = resolve_columns(connection, table_name, columns)
    sql_columns = ", ".join(quote_identifier(resolved[c]) for c in columns)
    sql = f"SELECT {sql_columns} FROM {quote_identifier(table_name)}"
    if order_by_rowid:
        sql += " ORDER BY rowid"
    cursor = connection.execute(sql)
    return [dict(zip(columns, row)) for row in cursor.fetchall()]


def clean_text(value: Any) -> str:
    """Normalize a join-key/text value without changing its case."""
    if value is None:
        return ""
    return str(value).strip()


def normalize_scope(value: Any) -> str:
    """Normalize scope for exact ABB matching."""
    return clean_text(value).upper()


def resolve_material_vendor(me2m_vendors: list[str]) -> tuple[str | None, bool]:
    """Return the first ME2M Vendor Detail for a material key.

    Preserve the ME2M report order when a material appears more than once and
    return one value only. The bool indicates that the material had multiple
    distinct ME2M values; it does not prevent the material-key lookup.
    """
    unique_vendors: list[str] = []
    seen: set[str] = set()
    for value in me2m_vendors:
        vendor = " ".join(clean_text(value).split())
        identity = vendor.casefold()
        if vendor and identity not in seen:
            seen.add(identity)
            unique_vendors.append(vendor)
    if not unique_vendors:
        return None, False
    return unique_vendors[0], len(unique_vendors) > 1


def to_number(value: Any, *, context: str) -> float:
    """Convert report quantities to float, treating blank values as zero."""
    if value is None:
        return 0.0
    if isinstance(value, bool):
        return float(value)

    text = str(value).strip()
    if not text:
        return 0.0

    text = text.replace(",", "").replace(" ", "")
    try:
        return float(text)
    except ValueError as exc:
        raise ValueError(
            f"Non-numeric quantity {value!r} encountered in {context}."
        ) from exc


def read_zpp7_rows(db_path: Path) -> list[dict[str, Any]]:
    """Read material rows from each ZPP7 reference table."""
    if not db_path.is_file():
        raise FileNotFoundError(f"ZPP7 database not found: {db_path}")

    required = ["Material Ref Code", "Material Code", "Material Description", "Req"]
    extracted: list[dict[str, Any]] = []

    with closing(sqlite3.connect(db_path.resolve().as_uri() + "?mode=ro", uri=True)) as conn:
        tables = list_tables(conn)
        candidates = [
            actual_name for folded, actual_name in tables.items()
            if folded != "parser_metadata"
        ]

        for table_name in candidates:
            available = table_columns(conn, table_name)
            if not all(column.casefold() in available for column in required):
                continue

            rows = read_table(conn, table_name, required)
            for row in rows:
                ref_code = clean_text(row["Material Ref Code"])
                material_code = clean_text(row["Material Code"])
                if not ref_code or not material_code:
                    continue

                extracted.append({
                    "Material Ref Code": ref_code,
                    "Material Code": material_code,
                    "Material Description": clean_text(row["Material Description"]),
                    "Req Quantity": to_number(
                        row["Req"],
                        context=f"ZPP7 table {table_name!r}, material {material_code!r}",
                    ),
                })

    if not extracted:
        raise RuntimeError(
            f"No ZPP7 material tables with the required columns were found in {db_path}."
        )
    return extracted


def build_rows(
    global_db_path: Path,
    zpp7_db_path: Path,
) -> list[tuple[Any, ...]]:
    """Join report data and calculate shortage from ZPP7 requirements less project stock."""
    if not global_db_path.is_file():
        raise FileNotFoundError(f"Global database not found: {global_db_path}")

    with closing(sqlite3.connect(global_db_path.resolve().as_uri() + "?mode=ro", uri=True)) as conn:
        wbs_table = resolve_table(conn, "wbs_element")
        scope_table = resolve_table(conn, "scopelist")
        storage_table = resolve_table(conn, "Storage")
        me2j_table = resolve_table(conn, "ME2J")
        me2m_table = resolve_table(conn, "ME2M")
        pr_table = resolve_table(conn, "PR")

        wbs_rows = read_table(conn, wbs_table, ["Material Ref Code", "WBS Element"])
        scope_rows = read_table(conn, scope_table, ["Material Code", "Scope"])
        storage_rows = read_table(
            conn, storage_table, ["Material Code", "Unrestricted", "WBS Element"]
        )
        me2j_rows = read_table(
            conn, me2j_table,
            ["Material Code", "Order Quantity", "Open Quantity", "WBS Element"],
        )
        me2m_rows = read_table(
            conn, me2m_table, ["Material Code", "Vendor Detail"],
            order_by_rowid=True,
        )
        pr_rows = read_table(
            conn, pr_table, ["Material Code", "PR Quantity", "WBS Element"]
        )

    # Vendor Detail comes exclusively from ME2M, keyed only by Material Code.
    # Preserve report order so repeated ME2M rows resolve consistently to the
    # first non-empty value rather than creating a multi-vendor shortage row.
    vendor_details: dict[str, list[str]] = defaultdict(list)
    for row in me2m_rows:
        material = clean_text(row["Material Code"])
        vendor = clean_text(row["Vendor Detail"])
        if material and vendor:
            vendor_details[material].append(vendor)

    # One Material Ref Code may theoretically map to multiple WBS elements.
    wbs_by_ref: dict[str, set[str]] = defaultdict(set)
    for row in wbs_rows:
        ref_code = clean_text(row["Material Ref Code"])
        wbs = clean_text(row["WBS Element"])
        if ref_code and wbs:
            wbs_by_ref[ref_code].add(wbs)

    # Build a material-to-scope lookup, preferring ABB over Vendor Scope.
    scope_priority = {"ABB": 0, "VENDOR SCOPE": 1}
    material_scope: dict[str, str] = {}
    for row in scope_rows:
        material = clean_text(row["Material Code"])
        scope_raw = clean_text(row["Scope"])
        if not material or not scope_raw:
            continue
        scope_upper = normalize_scope(scope_raw)
        if scope_upper not in scope_priority:
            continue
        existing = material_scope.get(material)
        if existing is None or scope_priority[scope_upper] < scope_priority.get(
            normalize_scope(existing), 99
        ):
            material_scope[material] = scope_raw

    scoped_materials = set(material_scope)

    storage_totals: dict[tuple[str, str], float] = defaultdict(float)
    free_stock_totals: dict[str, float] = defaultdict(float)
    for row in storage_rows:
        wbs = clean_text(row["WBS Element"])
        material = clean_text(row["Material Code"])
        qty = to_number(
            row["Unrestricted"],
            context=f"Storage, WBS {wbs!r}, material {material!r}",
        )
        if wbs and material:
            storage_totals[(wbs, material)] += qty
        elif not wbs and material:
            free_stock_totals[material] += qty

    order_totals: dict[tuple[str, str], list[float]] = defaultdict(lambda: [0.0, 0.0])
    for row in me2j_rows:
        wbs = clean_text(row["WBS Element"])
        material = clean_text(row["Material Code"])
        if wbs and material:
            totals = order_totals[(wbs, material)]
            totals[0] += to_number(
                row["Order Quantity"],
                context=f"ME2J Order Quantity, WBS {wbs!r}, material {material!r}",
            )
            totals[1] += to_number(
                row["Open Quantity"],
                context=f"ME2J Open Quantity, WBS {wbs!r}, material {material!r}",
            )

    pr_totals: dict[tuple[str, str], float] = defaultdict(float)
    for row in pr_rows:
        wbs = clean_text(row["WBS Element"])
        material = clean_text(row["Material Code"])
        if wbs and material:
            pr_totals[(wbs, material)] += to_number(
                row["PR Quantity"],
                context=f"PR, WBS {wbs!r}, material {material!r}",
            )

    zpp7_rows = read_zpp7_rows(zpp7_db_path)
    bom_totals: dict[tuple[str, str, str], dict[str, Any]] = {}
    for row in zpp7_rows:
        ref_code = row["Material Ref Code"]
        material = row["Material Code"]
        if material not in scoped_materials:
            continue
        mapped_wbs = wbs_by_ref.get(ref_code)
        if not mapped_wbs:
            continue

        for wbs in sorted(mapped_wbs):
            key = (ref_code, wbs, material)
            if key not in bom_totals:
                bom_totals[key] = {"description": row["Material Description"], "req": 0.0}
            elif not bom_totals[key]["description"] and row["Material Description"]:
                bom_totals[key]["description"] = row["Material Description"]
            bom_totals[key]["req"] += row["Req Quantity"]

    output: list[tuple[Any, ...]] = []
    for (ref_code, wbs, material), bom in sorted(bom_totals.items()):
        unrestricted = storage_totals.get((wbs, material), 0.0)
        order_qty, open_qty = order_totals.get((wbs, material), [0.0, 0.0])
        pr_qty = pr_totals.get((wbs, material), 0.0)
        net_shortage = max(bom["req"] - unrestricted, 0.0)
        free_stock = free_stock_totals.get(material, 0.0)
        vendor_detail, _ = resolve_material_vendor(
            vendor_details.get(material, [])
        )
        remarks = ""
        scope = material_scope.get(material, "")

        output.append((
            material,
            bom["description"] or None,
            bom["req"],
            unrestricted,
            order_qty,
            open_qty,
            pr_qty,
            net_shortage,
            free_stock,
            vendor_detail,
            remarks,
            ref_code,
            wbs,
            scope,
        ))

    if not output:
        raise RuntimeError(
            "No rows qualified for net_shortage. Check that ZPP7 Material "
            "Ref Codes match wbs_element and that scopelist Scope values "
            "contain 'ABB' or 'Vendor Scope'."
        )
    return output


def _ensure_net_shortage_schema(conn: sqlite3.Connection) -> None:
    """Create/migrate net_shortage with Final Date immediately before Remarks."""
    expected_types = {
        "project_id": "TEXT",
        "Material Code": "TEXT",
        "Material Description": "TEXT",
        "ZPP7 Quantity": "REAL",
        "Project Stock Quantity": "REAL",
        "PO Quantity": "REAL",
        "Open PO Quantity": "REAL",
        "PR Quantity": "REAL",
        "Net Shortage": "REAL",
        "Free Stock": "REAL",
        "Vendor Detail": "TEXT",
        "Final Date": "TEXT",
        "Remarks": "TEXT",
        "Material Ref Code": "TEXT",
        "WBS Element": "TEXT",
        "Scope": "TEXT",
    }
    tables = list_tables(conn)
    if OUTPUT_TABLE.casefold() not in tables:
        definitions = ", ".join(
            f"{quote_identifier(col)} {kind}"
            for col, kind in expected_types.items()
        )
        conn.execute(
            f"CREATE TABLE {quote_identifier(OUTPUT_TABLE)} ({definitions})"
        )
        return

    # Rebuild old schemas transactionally so Final Date can move before
    # Remarks. Copy all known and any legacy extra columns to preserve data.
    actual_table = tables[OUTPUT_TABLE.casefold()]
    actual_info = conn.execute(
        f"PRAGMA table_info({quote_identifier(actual_table)})"
    ).fetchall()
    actual_names = [str(row[1]) for row in actual_info]
    expected_names = list(expected_types)
    extra_types = {
        str(row[1]): (str(row[2]) or "TEXT")
        for row in actual_info
        if str(row[1]).casefold() not in {name.casefold() for name in expected_names}
    }
    schema_types = {**expected_types, **extra_types}
    if actual_names == list(schema_types):
        return

    new_table = f"{OUTPUT_TABLE}__ordered"
    conn.execute(f"DROP TABLE IF EXISTS {quote_identifier(new_table)}")
    definitions = ", ".join(
        f"{quote_identifier(col)} {kind}"
        for col, kind in schema_types.items()
    )
    conn.execute(
        f"CREATE TABLE {quote_identifier(new_table)} ({definitions})"
    )
    actual_by_folded = {name.casefold(): name for name in actual_names}
    columns_to_copy = [
        (name, actual_by_folded[name.casefold()])
        for name in schema_types
        if name.casefold() in actual_by_folded
    ]
    if columns_to_copy:
        target_columns = ", ".join(
            quote_identifier(target) for target, _ in columns_to_copy
        )
        source_columns = ", ".join(
            quote_identifier(source) for _, source in columns_to_copy
        )
        conn.execute(
            f"INSERT INTO {quote_identifier(new_table)} ({target_columns}) "
            f"SELECT {source_columns} FROM {quote_identifier(actual_table)}"
        )
    conn.execute(f"DROP TABLE {quote_identifier(actual_table)}")
    conn.execute(
        f"ALTER TABLE {quote_identifier(new_table)} "
        f"RENAME TO {quote_identifier(OUTPUT_TABLE)}"
    )


def write_output(
    rows: list[tuple[Any, ...]],
    project_id: str,
    db_path: Path = SERVICES_DB_PATH,
) -> None:
    """
    Project-aware refresh of net_shortage.

    - Deletes only THIS project's rows, leaving other projects intact.
    - Preserves existing Final Dates for matching records
      (matched by Material Ref Code + WBS Element + Material Code).
    - Adds project_id to every inserted row.
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)

    # Keep the physical SQLite order aligned with the UI/export column order.
    all_columns = ["project_id"] + list(OUTPUT_COLUMNS)
    # BUILD_COLUMNS is the tuple contract returned by build_rows().
    build_column_indexes = {name: index for index, name in enumerate(BUILD_COLUMNS)}
    quoted_columns = ", ".join(quote_identifier(c) for c in all_columns)
    placeholders = ", ".join("?" for _ in all_columns)

    with sqlite3.connect(db_path) as conn:
        conn.execute("PRAGMA busy_timeout = 5000")
        with conn:
            _ensure_net_shortage_schema(conn)

            # ── Read existing Final Dates for this project ─────────────
            existing_dates: dict[tuple[str, str, str], str] = {}
            try:
                cur = conn.execute(
                    f'SELECT "Material Ref Code", "WBS Element", "Material Code", '
                    f'"Final Date" FROM {quote_identifier(OUTPUT_TABLE)} '
                    f'WHERE "project_id" = ?',
                    (project_id,),
                )
                for row in cur.fetchall():
                    ref = clean_text(row[0])
                    wbs = clean_text(row[1])
                    mat = clean_text(row[2])
                    fdate = clean_text(row[3])
                    if fdate:
                        existing_dates[(ref, wbs, mat)] = fdate
            except Exception:
                pass  # Table may not have data yet

            # ── Delete only this project's rows ────────────────────────
            conn.execute(
                f'DELETE FROM {quote_identifier(OUTPUT_TABLE)} '
                f'WHERE "project_id" = ?',
                (project_id,),
            )

            # ── Insert new rows with preserved Final Dates ─────────────
            insert_rows: list[tuple[Any, ...]] = []
            for row in rows:
                # row is (material, desc, zpp7_qty, ..., ref_code, wbs, scope)
                # indices: 0=mat, 11=ref_code, 12=wbs
                ref_code = clean_text(row[11])
                wbs = clean_text(row[12])
                material = clean_text(row[0])
                final_date = existing_dates.get(
                    (ref_code, wbs, material), ""
                )
                values_by_column = {
                    name: row[index]
                    for name, index in build_column_indexes.items()
                }
                values_by_column["Final Date"] = final_date
                insert_rows.append(
                    tuple(
                        project_id if col == "project_id" else values_by_column[col]
                        for col in all_columns
                    )
                )

            if insert_rows:
                conn.executemany(
                    f'INSERT INTO {quote_identifier(OUTPUT_TABLE)} '
                    f'({quoted_columns}) VALUES ({placeholders})',
                    insert_rows,
                )


def main(project_id: str = "") -> int:
    """Build and refresh the net-shortage table for a specific project."""
    try:
        print("Building ABB net_shortage table...")
        print(f"Global database: {GLOBAL_DB_PATH}")
        print(f"ZPP7 database:   {ZPP7_DB_PATH}")
        print(f"Output database: {SERVICES_DB_PATH}")
        if project_id:
            print(f"Project ID:      {project_id}")

        rows = build_rows(GLOBAL_DB_PATH, ZPP7_DB_PATH)
        write_output(rows, project_id=project_id, db_path=SERVICES_DB_PATH)

        print(f"\nRows written: {len(rows)}")
        print(f"Output table: {OUTPUT_TABLE}")
        print("\nPreview:")
        with sqlite3.connect(SERVICES_DB_PATH) as conn:
            preview = conn.execute(
                f"SELECT * FROM {quote_identifier(OUTPUT_TABLE)} LIMIT 10"
            ).fetchall()
            for row in preview:
                print(row)
        return 0
    except Exception as error:
        print(f"Shortage service failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

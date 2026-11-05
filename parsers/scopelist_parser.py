"""
MATERIAL_SCOPE_LIST parser template — ABB Shortage System

Run from the ABB_Shortage_System project root:
    python parsers\scopelist_parser.py

Input:
    A Material Scope List workbook in raw, project, or global input folders.

Extracts:
    Material -> Material Code
    Description -> Description
    A "Final Scope" column (including Final Scope Finalization/Finalisation)
        -> Final Scope

Output:
    global.db, table "ScopeList"
    Columns: Material Code | Description | Final Scope

This template does not process any report until you run it.
"""

import os
from pathlib import Path
import re
import sqlite3
import sys

import pandas as pd


PROJECT_ROOT = Path(__file__).resolve().parent.parent
RAW_DIR = Path(os.getenv("ABB_RAW_DIR", PROJECT_ROOT / "data" / "raw")).resolve()
DB_PATH = Path(os.getenv("ABB_GLOBAL_DB", PROJECT_ROOT / "data" / "global.db")).resolve()

TABLE_NAME = "ScopeList"
OUTPUT_COLUMNS = ["Material Code", "Description", "Final Scope"]


def normalize_header(value) -> str:
    """Normalize headers but retain words so longer scope headers can match."""
    if pd.isna(value):
        return ""
    text = str(value).strip().lower()
    text = re.sub(r"[_\-]+", " ", text)
    text = re.sub(r"[^a-z0-9 ]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def find_scope_column(columns):
    """
    Match 'Final Scope Finalization' or British spelling 'Finalisation'
    at the start of the header. Any text after the phrase is allowed.
    """
    prefixes = (
        "final scope",
        "final scope finalization",
        "final scope finalisation",
    )
    for column in columns:
        normalized = normalize_header(column)
        if any(normalized.startswith(prefix) for prefix in prefixes):
            return column
    return None


def find_input_file(raw_dir: Path) -> Path:
    """Find a named scope workbook or identify a generic filename by headers."""
    roots = [raw_dir, PROJECT_ROOT / "projects", PROJECT_ROOT / "global"]
    extensions = {".xlsx", ".xls", ".xlsm"}
    files = sorted({
        path.resolve() for root in roots if root.exists() for path in root.rglob("*")
        if path.is_file()
        and path.suffix.lower() in extensions
        and not path.name.startswith("~$")
    })
    candidates = [path for path in files if "material_scope_list" in path.stem.casefold()]
    if not candidates:
        for path in files:
            try:
                extract_scope_list(path)
                candidates.append(path)
            except Exception:
                continue

    if not candidates:
        raise FileNotFoundError(
            f"No Material Scope List workbook found under {raw_dir} or project/global inputs."
        )
    if len(candidates) > 1:
        names = "\n".join(f"  - {path.name}" for path in candidates)
        raise RuntimeError(
            "Multiple Material Scope List files found. Keep only the intended "
            f"report in the input folders:\n{names}"
        )
    return candidates[0]


def find_header_row(sheet: pd.DataFrame):
    """Find a row with Material, Description, and a matching scope header."""
    for row_index in range(min(len(sheet), 50)):
        headers = list(sheet.iloc[row_index])
        normalized = [normalize_header(value) for value in headers]

        material_index = next(
            (i for i, name in enumerate(normalized)
             if name in {"material", "material code"}),
            None,
        )
        description_index = next(
            (i for i, name in enumerate(normalized)
             if name in {"description", "material description"}),
            None,
        )
        scope_index = next(
            (i for i, value in enumerate(headers)
             if find_scope_column([value]) is not None),
            None,
        )

        if (material_index is not None and description_index is not None
                and scope_index is not None):
            return row_index, {
                "Material": material_index,
                "Description": description_index,
                "Final Scope": scope_index,
            }
    return None, None


def extract_scope_list(report_path: Path) -> pd.DataFrame:
    """Extract only Material, Description, and Final Scope."""
    with pd.ExcelFile(report_path) as workbook:
        for sheet_name in workbook.sheet_names:
            sheet = pd.read_excel(
                workbook, sheet_name=sheet_name, header=None, dtype=object
            )
            header_row, positions = find_header_row(sheet)
            if positions is None:
                continue

            result = sheet.iloc[
                header_row + 1:,
                [
                    positions["Material"],
                    positions["Description"],
                    positions["Final Scope"],
                ],
            ].copy()
            result.columns = OUTPUT_COLUMNS

            for column in OUTPUT_COLUMNS:
                result[column] = result[column].map(
                    lambda value: None
                    if pd.isna(value) or str(value).strip() == ""
                    else str(value).strip()
                )

            result = result.dropna(how="all", subset=OUTPUT_COLUMNS)
            return result.reset_index(drop=True)

    raise ValueError(
        "Could not find headers for Material, Description, and Final Scope "
        "'Final Scope Finalization/Finalisation...' in any worksheet. "
        "Update find_header_row() if the workbook uses different headings."
    )


def write_scope_list(data: pd.DataFrame, db_path: Path) -> None:
    """Replace global.db's ScopeList table and remove the legacy table."""
    db_path.parent.mkdir(parents=True, exist_ok=True)

    with sqlite3.connect(db_path) as connection:
        with connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute('DROP TABLE IF EXISTS "ScopeList"')
            connection.execute('DROP TABLE IF EXISTS "scopelist"')
            connection.execute(
                '''CREATE TABLE "ScopeList" (
                    "Material Code" TEXT,
                    "Description" TEXT,
                    "Final Scope" TEXT
                )'''
            )
            connection.executemany(
                '''INSERT INTO "ScopeList" ("Material Code", "Description", "Final Scope")
                   VALUES (?, ?, ?)''',
                data[OUTPUT_COLUMNS].itertuples(index=False, name=None),
            )


def main() -> int:
    try:
        report_path = find_input_file(RAW_DIR)
        print(f"Input report found: {report_path.name}")

        extracted = extract_scope_list(report_path)
        write_scope_list(extracted, DB_PATH)

        print(f"Rows imported: {len(extracted)}")
        print(f"Database: {DB_PATH}")
        print(f"Table: {TABLE_NAME}")
        print("\nPreview:")
        print(
            extracted.head(20).to_string(index=False)
            if not extracted.empty else "(No data rows found.)"
        )
        return 0

    except Exception as exc:
        print(f"Scope-list parser failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

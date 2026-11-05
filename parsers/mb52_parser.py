"""MB52 Storage Parser — ABB Shortage System.

Run from the ABB_Shortage_System project root:
    python parsers\\mb52_parser.py

Finds an MB52 report in raw or project input folders (HTML or Excel).
Extracts Material, Description, Project Stock Quantity, and WBS Element.
Writes them to data/global.db table "MB52".

Only "MB52" is refreshed; all other tables in global.db are preserved.
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
TABLE_NAME = "MB52"
OUTPUT_COLUMNS = ["Material Code", "Description", "Project Stock Quantity", "WBS Element"]

SOURCE_ALIASES = {
    "Material Code": {"material", "material number", "material no", "material code"},
    "Description": {"material description", "material short text", "short text", "description"},
    "Project Stock Quantity": {"unrestricted", "unrestricted stock", "unrestricted quantity"},
    "WBS Element": {"wbs element", "wbs", "work breakdown structure element", "work breakdown structure"},
}


def normalize_header(value) -> str:
    if pd.isna(value):
        return ""
    text = str(value).replace("\xa0", " ").strip().lower()
    text = re.sub(r"[\s_-]+", " ", text)
    text = re.sub(r"[^a-z0-9 ]", "", text)
    return re.sub(r"\s+", " ", text).strip()


def clean_cell(value):
    """Trim cells and retain identifiers/quantities as text."""
    if pd.isna(value):
        return None
    value = str(value).replace("\xa0", " ").strip()
    return value if value else None


def find_report_file(raw_dir: Path) -> Path:
    """Find a named MB52 report or identify a generically named report by headers."""
    roots = [raw_dir, PROJECT_ROOT / "projects", PROJECT_ROOT / "global"]
    extensions = {".htm", ".html", ".xlsx", ".xls", ".xlsm"}
    files = sorted({
        path.resolve() for root in roots if root.exists() for path in root.rglob("*")
        if path.is_file() and path.suffix.casefold() in extensions
        and not path.name.startswith("~$")
    })
    candidates = [p for p in files if "in_mm_innr_mb52" in p.stem.casefold()]
    if not candidates:
        candidates = [p for p in files if "mb52" in p.stem.casefold()]
    if not candidates:
        for path in files:
            try:
                extract_mb52(path)
                candidates.append(path)
            except Exception:
                continue
    if not candidates:
        raise FileNotFoundError(
            f"No MB52 HTML or Excel report found under {raw_dir} or project/global inputs."
        )
    if len(candidates) > 1:
        names = "\n".join(f"  - {path.name}" for path in candidates)
        raise RuntimeError("Multiple MB52 report files found; keep only the intended report in data/raw/:\n" + names)
    return candidates[0]


def find_header_positions(row_values):
    normalized = [normalize_header(value) for value in row_values]
    positions = {}
    for source_name, aliases in SOURCE_ALIASES.items():
        accepted = {normalize_header(alias) for alias in aliases}
        for index, value in enumerate(normalized):
            if value in accepted:
                positions[source_name] = index
                break
    return positions if len(positions) == len(SOURCE_ALIASES) else None


def extract_from_tables(tables) -> pd.DataFrame:
    """Extract rows from SAP HTML tables; header rows may repeat on every page."""
    frames = []
    for table in tables:
        if table.empty:
            continue
        positions = None
        header_index = None
        for index in range(min(len(table), 10)):
            positions = find_header_positions(table.iloc[index].tolist())
            if positions:
                header_index = index
                break
        if positions is None:
            continue

        selected = table.iloc[
            header_index + 1:,
            [positions["Material Code"], positions["Description"],
             positions["Project Stock Quantity"], positions["WBS Element"]],
        ].copy()
        selected.columns = OUTPUT_COLUMNS
        for column in OUTPUT_COLUMNS:
            selected[column] = selected[column].map(clean_cell)
        selected = selected.dropna(how="all", subset=OUTPUT_COLUMNS)
        if not selected.empty:
            frames.append(selected)

    if not frames:
        raise ValueError(
            "Could not find a table containing Material, Material Description, "
            "Unrestricted Quantity, and WBS Element."
        )

    result = pd.concat(frames, ignore_index=True)
    # Drop any repeated report-header rows.
    signatures = {column: normalize_header(column) for column in OUTPUT_COLUMNS}
    signatures["Material Code"] = normalize_header("Material")
    signatures["Description"] = normalize_header("Material Description")
    signatures["Project Stock Quantity"] = normalize_header("Unrestricted")
    is_header = result.apply(
        lambda row: all(normalize_header(row[col]) == signatures[col] for col in OUTPUT_COLUMNS),
        axis=1,
    )
    return result.loc[~is_header].reset_index(drop=True)


def extract_from_excel(report_path: Path) -> pd.DataFrame:
    frames = []
    for sheet_name in pd.ExcelFile(report_path).sheet_names:
        raw = pd.read_excel(report_path, sheet_name=sheet_name, header=None, dtype=object)
        positions = None
        header_index = None
        for index in range(min(len(raw), 50)):
            positions = find_header_positions(raw.iloc[index].tolist())
            if positions:
                header_index = index
                break
        if positions is None:
            continue

        selected = raw.iloc[
            header_index + 1:,
            [positions["Material Code"], positions["Description"],
             positions["Project Stock Quantity"], positions["WBS Element"]],
        ].copy()
        selected.columns = OUTPUT_COLUMNS
        for column in OUTPUT_COLUMNS:
            selected[column] = selected[column].map(clean_cell)
        selected = selected.dropna(how="all", subset=OUTPUT_COLUMNS)
        if not selected.empty:
            frames.append(selected)

    if not frames:
        raise ValueError(
            "Could not find a header row containing Material, Material Description, "
            "Unrestricted Quantity, and WBS Element in the Excel workbook."
        )
    return pd.concat(frames, ignore_index=True).reset_index(drop=True)


def extract_mb52(report_path: Path) -> pd.DataFrame:
    if report_path.suffix.lower() in {".htm", ".html"}:
        tables = pd.read_html(report_path, header=None, displayed_only=False)
        return extract_from_tables(tables)
    return extract_from_excel(report_path)


def write_mb52_table(data: pd.DataFrame, db_path: Path) -> None:
    """Refresh MB52 and remove the legacy Storage table."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as connection:
        with connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute('DROP TABLE IF EXISTS "MB52"')
            connection.execute('DROP TABLE IF EXISTS "Storage"')
            connection.execute(
                """
                CREATE TABLE "MB52" (
                    "Material Code" TEXT,
                    "Description" TEXT,
                    "Project Stock Quantity" TEXT,
                    "WBS Element" TEXT
                )
                """
            )
            connection.executemany(
                """
                INSERT INTO "MB52"
                    ("Material Code", "Description", "Project Stock Quantity", "WBS Element")
                VALUES (?, ?, ?, ?)
                """,
                data[OUTPUT_COLUMNS].itertuples(index=False, name=None),
            )


def main() -> int:
    try:
        report_path = find_report_file(RAW_DIR)
        print(f"MB52 report found: {report_path.name}")
        extracted = extract_mb52(report_path)
        write_mb52_table(extracted, DB_PATH)
        print(f"Rows imported: {len(extracted)}")
        print(f"Database: {DB_PATH}")
        print(f'Table: "{TABLE_NAME}"')
        print("\nPreview:")
        print(extracted.head(20).to_string(index=False) if not extracted.empty else "(No data rows found.)")
        return 0
    except Exception as exc:
        print(f"MB52 parser failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

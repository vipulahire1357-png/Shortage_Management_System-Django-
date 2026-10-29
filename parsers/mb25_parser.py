"""MB25 WBS Element Parser — ABB Shortage System.

Run from the ABB_Shortage_System project root:
    python parsers\mb25_parser.py

Finds one Excel filename containing "MB25" in data/raw/.
Extracts Material, Material Description, and WBS Element.
Writes Material -> Material Ref Code, Material Description -> MRC Description,
and WBS Element into data/global.db, table wbs_element.

Only wbs_element is refreshed; all other tables in global.db are preserved.
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
TABLE_NAME = "wbs_element"
OUTPUT_COLUMNS = ["Material Ref Code", "MRC Description", "WBS Element"]

COLUMN_ALIASES = {
    "Material": {"material", "material number", "material no", "material code"},
    "Material Description": {
        "material description", "material short text", "short text", "description"
    },
    "WBS Element": {
        "wbs element", "wbs", "work breakdown structure element",
        "work breakdown structure"
    },
}


def normalize_header(value) -> str:
    if pd.isna(value):
        return ""
    text = str(value).strip().lower()
    text = re.sub(r"[\s_\-]+", " ", text)
    text = re.sub(r"[^a-z0-9 ]", "", text)
    return re.sub(r"\s+", " ", text).strip()


def find_mb25_file(raw_dir: Path) -> Path:
    """Find exactly one MB25-named Excel file in data/raw."""
    if not raw_dir.exists():
        raise FileNotFoundError(
            f"Raw-data folder not found: {raw_dir}. Create data/raw/ first."
        )

    extensions = {".xlsx", ".xls", ".xlsm"}
    candidates = sorted(
        path for path in raw_dir.iterdir()
        if path.is_file()
        and "mb25" in path.name.lower()
        and path.suffix.lower() in extensions
        and not path.name.startswith("~$")
    )
    if not candidates:
        raise FileNotFoundError(
            f"No Excel filename containing 'MB25' found in {raw_dir}"
        )
    if len(candidates) > 1:
        names = "\n".join(f"  - {path.name}" for path in candidates)
        raise RuntimeError(
            "Multiple MB25 files found. Keep only the intended report in data/raw/:\n"
            + names
        )
    return candidates[0]


def locate_header_row(raw_sheet: pd.DataFrame):
    """Find a row containing all three required source headers."""
    aliases = {
        target: {normalize_header(alias) for alias in names}
        for target, names in COLUMN_ALIASES.items()
    }
    for row_index in range(min(len(raw_sheet), 50)):
        values = [normalize_header(v) for v in raw_sheet.iloc[row_index].tolist()]
        matched = {}
        for target, accepted in aliases.items():
            for column_index, value in enumerate(values):
                if value in accepted:
                    matched[target] = column_index
                    break
        if len(matched) == len(COLUMN_ALIASES):
            return row_index, matched
    return None, None


def clean_cell(value):
    """Convert blank values to None and preserve identifiers as text."""
    if pd.isna(value):
        return None
    value = str(value).strip()
    return value if value else None


def extract_mb25(report_path: Path) -> pd.DataFrame:
    """Extract only Material, Material Description, and WBS Element."""
    workbook = pd.ExcelFile(report_path)
    for sheet_name in workbook.sheet_names:
        raw_sheet = pd.read_excel(
            report_path, sheet_name=sheet_name, header=None, dtype=object
        )
        header_row, positions = locate_header_row(raw_sheet)
        if positions is None:
            continue

        selected = raw_sheet.iloc[
            header_row + 1:,
            [
                positions["Material"],
                positions["Material Description"],
                positions["WBS Element"],
            ],
        ].copy()
        selected.columns = OUTPUT_COLUMNS
        for column in OUTPUT_COLUMNS:
            selected[column] = selected[column].map(clean_cell)

        return selected.dropna(how="all", subset=OUTPUT_COLUMNS).reset_index(drop=True)

    raise ValueError(
        "Could not find a header row containing Material, Material Description, "
        "and WBS Element in any worksheet. Check the report's column headings."
    )


def write_wbs_table(data: pd.DataFrame, db_path: Path) -> None:
    """Replace only wbs_element, in a transaction."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS wbs_element (
                "Material Ref Code" TEXT,
                "MRC Description" TEXT,
                "WBS Element" TEXT
            )
            """
        )
        with connection:
            connection.execute("DELETE FROM wbs_element")
            connection.executemany(
                """
                INSERT INTO wbs_element
                    ("Material Ref Code", "MRC Description", "WBS Element")
                VALUES (?, ?, ?)
                """,
                data[OUTPUT_COLUMNS].itertuples(index=False, name=None),
            )


def main() -> int:
    try:
        report_path = find_mb25_file(RAW_DIR)
        print(f"MB25 report found: {report_path.name}")
        extracted = extract_mb25(report_path)
        write_wbs_table(extracted, DB_PATH)

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
        print(f"MB25 parser failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

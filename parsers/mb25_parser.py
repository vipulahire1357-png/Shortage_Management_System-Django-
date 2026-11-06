"""MB25 WBS Element Parser — ABB Shortage System.

Run from the ABB_Shortage_System project root:
    python parsers\\mb25_parser.py

Finds one MB25 workbook in the raw or project input folders.
Extracts only Material -> Material Ref Code and WBS Element into global.db.MB25.

Only MB25 is refreshed; all other tables in global.db are preserved.
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
TABLE_NAME = "MB25"
OUTPUT_COLUMNS = ["Material Ref Code", "WBS Element"]

COLUMN_ALIASES = {
    "Material": {"material", "material number", "material no", "material code"},
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
    """Find one MB25 workbook independent of case and project subfolder.

    Prioritises ``data/raw/`` so that when reports are staged there the
    parser does not also pick up originals in ``projects/``.
    """
    all_roots = [raw_dir, PROJECT_ROOT / "projects"]
    if not any(root.exists() for root in all_roots):
        raise FileNotFoundError(
            f"No raw or project input folder found for MB25: {raw_dir}"
        )

    extensions = {".xlsx", ".xls", ".xlsm"}

    def _gather(roots):
        return sorted(
            path.resolve() for root in roots if root.exists()
            for path in root.rglob("*")
            if path.is_file()
            and path.suffix.lower() in extensions
            and not path.name.startswith("~$")
        )

    # Search raw_dir first; only fall back to projects/ when nothing found
    for roots in ([raw_dir], all_roots):
        files = _gather(roots)
        candidates = [path for path in files if "mb25" in path.stem.casefold()]
        if not candidates:
            # SAP exports are often renamed during download. Identify an MB25 by
            # its Material/WBS header and reference-code data when the filename is generic.
            for path in files:
                try:
                    frame = extract_mb25(path)
                    if not frame.empty and frame["Material Ref Code"].str.upper().str.startswith("1SYN").any():
                        candidates.append(path)
                except Exception:
                    continue
        if candidates:
            break

    if not candidates:
        raise FileNotFoundError(
            f"No Excel filename containing 'MB25' found under {raw_dir} or {PROJECT_ROOT / 'projects'}"
        )
    if len(candidates) > 1:
        names = "\n".join(f"  - {path.name}" for path in candidates)
        raise RuntimeError(
            "Multiple MB25 files found. Keep only the intended report in the input folders:\n"
            + names
        )
    return candidates[0]


def locate_header_row(raw_sheet: pd.DataFrame):
    """Find a row containing the two required source headers."""
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
    """Extract only Material (as Material Ref Code) and WBS Element."""
    with pd.ExcelFile(report_path) as workbook:
        for sheet_name in workbook.sheet_names:
            raw_sheet = pd.read_excel(
                workbook, sheet_name=sheet_name, header=None, dtype=object
            )
            header_row, positions = locate_header_row(raw_sheet)
            if positions is None:
                continue

            selected = raw_sheet.iloc[
                header_row + 1:,
                [
                    positions["Material"],
                    positions["WBS Element"],
                ],
            ].copy()
            selected.columns = OUTPUT_COLUMNS
            for column in OUTPUT_COLUMNS:
                selected[column] = selected[column].map(clean_cell)

            return selected.dropna(subset=OUTPUT_COLUMNS, how="any").drop_duplicates(
                subset=OUTPUT_COLUMNS
            ).reset_index(drop=True)

    raise ValueError(
        "Could not find a header row containing Material and WBS Element "
        "in any worksheet. Check the report's column headings."
    )


def write_wbs_table(data: pd.DataFrame, db_path: Path) -> None:
    """Replace only MB25, in a transaction, and remove the legacy table."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as connection:
        with connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("DROP TABLE IF EXISTS MB25")
            connection.execute("DROP TABLE IF EXISTS wbs_element")
            connection.execute(
                '''CREATE TABLE MB25 (
                    "Material Ref Code" TEXT NOT NULL,
                    "WBS Element" TEXT NOT NULL,
                    PRIMARY KEY ("Material Ref Code", "WBS Element")
                )'''
            )
            connection.executemany(
                """
                INSERT OR IGNORE INTO MB25
                    ("Material Ref Code", "WBS Element")
                VALUES (?, ?)
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

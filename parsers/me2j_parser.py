"""
ME2J report parser for the ABB Shortage System.

Reads an ME2J Excel report from data/raw, selects the required columns,
renames them to the canonical schema, and refreshes the ME2J table in
data/global.db without modifying other tables.
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
TABLE_NAME = "ME2J"

# Source report header -> database column
COLUMN_MAP = {
    "Material": "Material Code",
    "Short Text": "Description",
    "Vendor/supplying plant": "Vendor",
    "Order Quantity": "Order Quantity",
    "Still to be delivered (qty)": "Open Quantity",
    "WBS Element": "WBS Element",
}

OUTPUT_COLUMNS = [
    "Material Code",
    "Description",
    "Vendor",
    "Order Quantity",
    "Open Quantity",
    "WBS Element",
]

SUPPORTED_EXTENSIONS = {".xlsx", ".xls", ".xlsm"}


def normalize_header(value):
    """Normalize a header for tolerant matching."""
    text = "" if pd.isna(value) else str(value)
    text = text.replace("\xa0", " ").strip().lower()
    text = re.sub(r"[\s_\-]+", " ", text)
    return text


def find_report():
    """Find an ME2J Excel report in data/raw."""
    if not RAW_DIR.exists():
        raise FileNotFoundError(f"Input directory does not exist: {RAW_DIR}")

    candidates = [
        path for path in RAW_DIR.iterdir()
        if path.is_file()
        and path.suffix.lower() in SUPPORTED_EXTENSIONS
        and "me2j" in path.stem.lower()
        and not path.name.startswith("~$")
    ]

    if not candidates:
        raise FileNotFoundError(
            f"No ME2J Excel report found in {RAW_DIR}. "
            "Place the report there and include 'ME2J' in its filename."
        )

    if len(candidates) > 1:
        names = "\n".join(f"  - {path.name}" for path in candidates)
        raise RuntimeError(
            "More than one ME2J report was found. Keep only the intended "
            f"report in {RAW_DIR}, or update find_report():\n{names}"
        )

    return candidates[0]


def load_me2j(report_path):
    """Read the report and return only the requested, renamed columns."""
    # The supplied ME2J report is an Excel workbook with one header row.
    sheets = pd.read_excel(report_path, sheet_name=None, dtype=object)
    required_headers = {normalize_header(header) for header in COLUMN_MAP}

    extracted_frames = []

    for sheet_name, frame in sheets.items():
        if frame.empty:
            continue

        # Handle minor whitespace/case variations in Excel headers.
        normalized_to_original = {}
        for column in frame.columns:
            normalized_to_original[normalize_header(column)] = column

        if not required_headers.issubset(normalized_to_original):
            continue

        source_columns = {
            normalized_to_original[normalize_header(source)]: target
            for source, target in COLUMN_MAP.items()
        }

        selected = frame[list(source_columns.keys())].copy()
        selected.rename(columns=source_columns, inplace=True)
        selected = selected[OUTPUT_COLUMNS]

        # Remove fully empty rows and any repeated header rows.
        selected = selected.dropna(how="all")
        if not selected.empty:
            material_header = selected["Material Code"].map(normalize_header)
            selected = selected[material_header != "material"]
            extracted_frames.append(selected)

    if not extracted_frames:
        raise ValueError(
            "Could not find all required columns in the workbook. "
            f"Required source headers: {', '.join(COLUMN_MAP.keys())}"
        )

    result = pd.concat(extracted_frames, ignore_index=True)
    result = result[OUTPUT_COLUMNS]

    # Keep identifiers as text and trim strings without altering numeric values.
    for column in OUTPUT_COLUMNS:
        result[column] = result[column].map(
            lambda value: (
                None if pd.isna(value) or str(value).strip() == ""
                else str(value).strip()
            )
        )

    return result


def write_to_database(frame):
    """Replace only the ME2J table, preserving all other database tables."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)

    with sqlite3.connect(DB_PATH) as connection:
        # TEXT storage preserves material codes and WBS identifiers as written.
        column_definitions = ", ".join(
            f'"{column}" TEXT' for column in OUTPUT_COLUMNS
        )
        connection.execute(
            f'CREATE TABLE IF NOT EXISTS "{TABLE_NAME}" '
            f"({column_definitions})"
        )

        # Recreate the target table schema in case an earlier version differed.
        connection.execute(f'DROP TABLE IF EXISTS "{TABLE_NAME}_new"')
        connection.execute(
            f'CREATE TABLE "{TABLE_NAME}_new" ({column_definitions})'
        )

        placeholders = ", ".join("?" for _ in OUTPUT_COLUMNS)
        quoted_columns = ", ".join(f'"{column}"' for column in OUTPUT_COLUMNS)
        connection.executemany(
            f'INSERT INTO "{TABLE_NAME}_new" ({quoted_columns}) '
            f"VALUES ({placeholders})",
            frame[OUTPUT_COLUMNS].itertuples(index=False, name=None),
        )

        connection.execute(f'DROP TABLE "{TABLE_NAME}"')
        connection.execute(
            f'ALTER TABLE "{TABLE_NAME}_new" RENAME TO "{TABLE_NAME}"'
        )


def main():
    try:
        report_path = find_report()
        print(f"Input report: {report_path}")

        data = load_me2j(report_path)
        write_to_database(data)

        print(f"Imported {len(data)} rows into table '{TABLE_NAME}'.")
        print(f"Database: {DB_PATH}")
        print("\nPreview:")
        print(data.head(10).to_string(index=False))

    except Exception as error:
        print(f"ME2J parser failed: {error}", file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()

"""ME2M purchase-order report parser for the ABB Shortage System.

Reads an ME2M Excel report from data/raw and refreshes only the ME2M table
in data/global.db. The parser is compatible with the project's parser registry:
main() returns 0 on success and 1 on failure.
"""

import os
from pathlib import Path
import re
import sqlite3
import sys

import pandas as pd


PROJECT_ROOT = Path(__file__).resolve().parents[1]
RAW_DIR = Path(os.getenv("ABB_RAW_DIR", PROJECT_ROOT / "data" / "raw")).resolve()
DB_PATH = Path(os.getenv("ABB_GLOBAL_DB", PROJECT_ROOT / "data" / "global.db")).resolve()
TABLE_NAME = "ME2M"

# Exact source report header -> canonical database column.
COLUMN_MAP = {
    "Material": "Material Code",
    "Short Text": "Description",
    "Vendor/supplying plant": "Vendor Detail",
}
OUTPUT_COLUMNS = ["Material Code", "Description", "Vendor Detail"]
SUPPORTED_EXTENSIONS = {".xlsx", ".xls", ".xlsm", ".xlsb"}


def normalize_header(value) -> str:
    """Normalize header text for case/whitespace-insensitive matching."""
    if pd.isna(value):
        return ""
    text = str(value).replace("\ufeff", "").replace("\xa0", " ").strip().lower()
    return re.sub(r"\s+", " ", text)


def clean_cell(value):
    """Keep report values as trimmed text, preserving material identifiers."""
    if pd.isna(value):
        return None
    text = str(value).replace("\xa0", " ").strip()
    return text if text else None


def find_report() -> Path:
    """Find exactly one ME2M Excel workbook in data/raw."""
    if not RAW_DIR.is_dir():
        raise FileNotFoundError(f"Raw reports directory not found: {RAW_DIR}")

    candidates = sorted(
        path for path in RAW_DIR.iterdir()
        if path.is_file()
        and path.suffix.lower() in SUPPORTED_EXTENSIONS
        and "me2m" in path.stem.lower()
        and not path.name.startswith("~$")
    )

    if not candidates:
        raise FileNotFoundError(
            f"No ME2M Excel report found in {RAW_DIR}. "
            "Place the workbook in data/raw and include 'ME2M' in its filename."
        )

    if len(candidates) > 1:
        names = "\n".join(f"  - {path.name}" for path in candidates)
        raise RuntimeError(
            "Multiple ME2M reports found. Keep only the intended workbook in "
            f"{RAW_DIR}:\n{names}"
        )

    return candidates[0]


def load_me2m(report_path: Path) -> pd.DataFrame:
    """Find the required headers in workbook sheets and extract the 3 columns."""
    try:
        # pandas uses pyxlsb for Excel Binary Workbook (.xlsb) files.
        sheets = pd.read_excel(report_path, sheet_name=None, header=None, dtype=object)
    except ImportError as error:
        if report_path.suffix.lower() == ".xlsb":
            raise ImportError(
                "This ME2M report is an .xlsb workbook. Install its Excel reader "
                "dependency with: python -m pip install pyxlsb"
            ) from error
        raise
    required = {normalize_header(header) for header in COLUMN_MAP}
    frames = []

    for sheet_name, raw in sheets.items():
        if raw.empty:
            continue

        # SAP exports may have title/blank rows before the actual column headers.
        header_index = None
        header_map = {}
        for row_index in range(min(len(raw), 50)):
            normalized = {
                normalize_header(value): col_index
                for col_index, value in enumerate(raw.iloc[row_index].tolist())
                if normalize_header(value)
            }
            if required.issubset(normalized):
                header_index = row_index
                header_map = normalized
                break

        if header_index is None:
            continue

        source_positions = [
            header_map[normalize_header(source)] for source in COLUMN_MAP
        ]
        data = raw.iloc[header_index + 1:, source_positions].copy()
        data.columns = OUTPUT_COLUMNS

        for column in OUTPUT_COLUMNS:
            data[column] = data[column].map(clean_cell)

        data = data.dropna(how="all", subset=OUTPUT_COLUMNS)

        # Ignore repeated report header rows and rows with no material identifier.
        data = data[
            data["Material Code"].map(normalize_header) != "material"
        ]
        data = data.dropna(subset=["Material Code"])

        if not data.empty:
            frames.append(data)

    if not frames:
        raise ValueError(
            "Could not find a worksheet header containing all required columns: "
            + ", ".join(COLUMN_MAP)
        )

    result = pd.concat(frames, ignore_index=True)
    return result[OUTPUT_COLUMNS].reset_index(drop=True)


def write_to_database(data: pd.DataFrame, db_path: Path = DB_PATH) -> None:
    """Transactionally replace only ME2M, preserving every other database table."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    column_definitions = ", ".join(f'"{column}" TEXT' for column in OUTPUT_COLUMNS)
    quoted_columns = ", ".join(f'"{column}"' for column in OUTPUT_COLUMNS)
    placeholders = ", ".join("?" for _ in OUTPUT_COLUMNS)

    with sqlite3.connect(db_path, timeout=30) as connection:
        connection.execute("PRAGMA busy_timeout = 5000")
        with connection:
            connection.execute(f'DROP TABLE IF EXISTS "{TABLE_NAME}_new"')
            connection.execute(
                f'CREATE TABLE "{TABLE_NAME}_new" ({column_definitions})'
            )
            connection.executemany(
                f'INSERT INTO "{TABLE_NAME}_new" ({quoted_columns}) '
                f"VALUES ({placeholders})",
                data[OUTPUT_COLUMNS].itertuples(index=False, name=None),
            )
            connection.execute(f'DROP TABLE IF EXISTS "{TABLE_NAME}"')
            connection.execute(
                f'ALTER TABLE "{TABLE_NAME}_new" RENAME TO "{TABLE_NAME}"'
            )


def main() -> int:
    """Parser-registry entry point."""
    try:
        report_path = find_report()
        print(f"ME2M report found: {report_path.name}")

        data = load_me2m(report_path)
        write_to_database(data)

        print(f"Rows imported: {len(data)}")
        print(f"Database: {DB_PATH}")
        print(f'Table: "{TABLE_NAME}"')
        print("\nPreview:")
        print(data.head(10).to_string(index=False))
        return 0

    except Exception as error:
        print(f"ME2M parser failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

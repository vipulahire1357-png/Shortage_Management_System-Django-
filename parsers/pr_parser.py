"""
Purchase Requisition (PR) report parser.

Extracts:
    Material       -> Material Code
    Short Text     -> Description
    Quantity       -> PR Quantity
    WBS Element    -> WBS Element

Writes the result to data/global.db, table PR.
Supports SAP tab-delimited UTF-16 exports that may have an .xls extension,
as well as genuine Excel workbooks.
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
TABLE_NAME = "PR"

OUTPUT_COLUMNS = [
    "Material Code",
    "Description",
    "PR Quantity",
    "WBS Element",
]

# Accepted source headers (matched case/spacing-insensitively).
# The uploaded PR workbook uses "Quantity requested".
SOURCE_COLUMN_ALIASES = {
    "Material": "Material Code",
    "Short Text": "Description",
    "Quantity requested": "PR Quantity",
    "WBS Element": "WBS Element",
}
SOURCE_HEADER_ALIASES = {
    "Material": ("Material",),
    "Short Text": ("Short Text",),
    "Quantity requested": ("Quantity requested", "Quantity"),
    "WBS Element": ("WBS Element",),
}


def normalize_header(value) -> str:
    """Normalize column labels for case/spacing-insensitive matching."""
    if pd.isna(value):
        return ""
    text = str(value).replace("\ufeff", "").replace("\xa0", " ").strip().lower()
    return re.sub(r"\s+", " ", text)


def find_report() -> Path:
    """Find one PR report in data/raw."""
    if not RAW_DIR.is_dir():
        raise FileNotFoundError(f"Raw reports directory not found: {RAW_DIR}")

    candidates = [
        path for path in RAW_DIR.iterdir()
        if path.is_file()
        and path.suffix.lower() in {".xls", ".xlsx", ".xlsm", ".txt", ".tsv", ".csv"}
        and not path.name.startswith("~$")
        and (
            path.stem.strip().lower() in {"pr", "pr 1", "purchase requisition"}
            or path.stem.strip().lower().startswith("pr ")
            or "purchase requisition" in path.stem.lower()
        )
    ]

    if not candidates:
        raise FileNotFoundError(
            f"No PR report found in {RAW_DIR}. "
            "Place the report there and name it with 'PR' in the filename."
        )

    if len(candidates) > 1:
        names = "\n".join(f"  - {path.name}" for path in candidates)
        raise RuntimeError(
            "Multiple PR reports found. Keep only the intended report in "
            f"{RAW_DIR}, or update find_report():\n{names}"
        )

    return candidates[0]


def read_sap_text_export(path: Path) -> pd.DataFrame:
    """Read SAP Dynamic List Display exports saved as UTF-16 tab-delimited text."""
    raw = path.read_bytes()

    # SAP exports can use UTF-16 (often with BOM), UTF-8, or Windows encoding.
    decoded = None
    for encoding in ("utf-16", "utf-8-sig", "cp1252"):
        try:
            decoded = raw.decode(encoding)
            break
        except UnicodeDecodeError:
            continue

    if decoded is None:
        raise ValueError(f"Could not decode report file: {path}")

    lines = decoded.splitlines()
    header_index = None
    header_cells = None

    required = {
        normalize_header(alias)
        for aliases in SOURCE_HEADER_ALIASES.values()
        for alias in aliases
    }
    required_groups = [
        {normalize_header(alias) for alias in aliases}
        for aliases in SOURCE_HEADER_ALIASES.values()
    ]

    for index, line in enumerate(lines):
        cells = [cell.strip() for cell in line.split("\t")]
        normalized = {normalize_header(cell) for cell in cells if cell.strip()}
        if all(group.intersection(normalized) for group in required_groups):
            header_index = index
            header_cells = cells
            break

    if header_index is None or header_cells is None:
        raise ValueError(
            "Could not locate a tab-delimited header containing Material, "
            "Short Text, Quantity requested (or Quantity), and WBS Element."
        )

    rows = []
    width = len(header_cells)

    for line in lines[header_index + 1:]:
        if not line.strip():
            continue
        cells = line.split("\t")

        # Ignore non-data footer/header lines that do not have the expected
        # report shape. Preserve internal empty cells so columns stay aligned.
        if len(cells) < width:
            cells += [""] * (width - len(cells))
        elif len(cells) > width:
            cells = cells[:width]

        rows.append(cells)

    frame = pd.DataFrame(rows, columns=header_cells)
    return frame


def load_pr_report(path: Path) -> pd.DataFrame:
    """Load report data and return the four canonical columns."""
    # SAP Dynamic List Display may be UTF-16 TSV despite having an .xls suffix.
    prefix = path.read_bytes()[:200]
    is_text_export = (
        prefix.startswith((b"\xff\xfe", b"\xfe\xff"))
        or (
            path.suffix.lower() not in {".xlsx", ".xlsm"}
            and b"\x00" in prefix
        )
    )

    if is_text_export or path.suffix.lower() in {".txt", ".tsv", ".csv"}:
        frame = read_sap_text_export(path)
    else:
        # For actual Excel workbooks, scan sheets for the required headers.
        sheets = pd.read_excel(path, sheet_name=None, dtype=object, header=0)
        frame = None
        for candidate in sheets.values():
            normalized = {normalize_header(c): c for c in candidate.columns}
            source_to_target = {}
            for canonical_source, aliases in SOURCE_HEADER_ALIASES.items():
                matched = next(
                    (normalized[normalize_header(alias)]
                     for alias in aliases
                     if normalize_header(alias) in normalized),
                    None,
                )
                if matched is None:
                    break
                source_to_target[matched] = SOURCE_COLUMN_ALIASES[canonical_source]

            if len(source_to_target) == len(SOURCE_HEADER_ALIASES):
                frame = candidate.rename(columns=source_to_target)
                break

        if frame is None:
            raise ValueError(
                "No Excel sheet contains the required columns: WBS Element, "
                "Material, Short Text, and Quantity requested (or Quantity)."
            )

        result = frame[OUTPUT_COLUMNS].copy()
        return clean_data(result)

    normalized_to_original = {}
    for column in frame.columns:
        normalized_to_original[normalize_header(column)] = column

    selected = pd.DataFrame()
    missing = []
    for canonical_source, target in SOURCE_COLUMN_ALIASES.items():
        matched = next(
            (
                normalized_to_original[normalize_header(alias)]
                for alias in SOURCE_HEADER_ALIASES[canonical_source]
                if normalize_header(alias) in normalized_to_original
            ),
            None,
        )
        if matched is None:
            missing.append(canonical_source)
        else:
            selected[target] = frame[matched]

    if missing:
        raise ValueError(f"PR report is missing required columns: {missing}")

    # Remove repeated header lines and blank records.
    selected = selected.dropna(how="all", subset=OUTPUT_COLUMNS)
    selected = selected[
        selected["Material Code"].map(normalize_header) != "material"
    ]

    return clean_data(selected)


def clean_data(frame: pd.DataFrame) -> pd.DataFrame:
    """Trim text, validate quantity values, and keep IDs as text."""
    result = frame[OUTPUT_COLUMNS].copy()

    for column in ("Material Code", "Description", "WBS Element"):
        result[column] = result[column].map(
            lambda value: (
                None if pd.isna(value) or str(value).strip() == ""
                else str(value).strip()
            )
        )

    def parse_quantity(value):
        if pd.isna(value) or str(value).strip() == "":
            return None
        text = str(value).strip().replace(",", "").replace(" ", "")
        try:
            number = float(text)
        except ValueError as exc:
            raise ValueError(f"Invalid PR Quantity value: {value!r}") from exc
        return int(number) if number.is_integer() else number

    result["PR Quantity"] = result["PR Quantity"].map(parse_quantity)

    # Remove rows without the keys needed for reliable joins.
    result = result.dropna(subset=["Material Code", "WBS Element"], how="any")
    result = result.reset_index(drop=True)

    if result.empty:
        raise ValueError("The PR report contains no usable material/WBS rows.")

    return result


def write_to_database(frame: pd.DataFrame) -> None:
    """Atomically replace only the PR table in global.db."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)

    with sqlite3.connect(DB_PATH) as connection:
        connection.execute("PRAGMA busy_timeout = 5000")

        with connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS "PR" (
                    "Material Code" TEXT,
                    "Description" TEXT,
                    "PR Quantity" REAL,
                    "WBS Element" TEXT
                )
                """
            )

            placeholders = ", ".join("?" for _ in OUTPUT_COLUMNS)
            columns_sql = ", ".join(f'"{column}"' for column in OUTPUT_COLUMNS)

            # Delete and insert within one transaction; other tables are untouched.
            connection.execute('DELETE FROM "PR"')
            connection.executemany(
                f'INSERT INTO "PR" ({columns_sql}) VALUES ({placeholders})',
                frame[OUTPUT_COLUMNS].itertuples(index=False, name=None),
            )


def main() -> int:
    """Parse the PR report and refresh global.db -> PR."""
    try:
        report_path = find_report()
        print(f"PR report found: {report_path.name}")

        data = load_pr_report(report_path)
        write_to_database(data)

        print(f"Rows imported: {len(data)}")
        print(f"Database: {DB_PATH}")
        print(f"Table: {TABLE_NAME}")
        print("\nPreview:")
        print(data.head(10).to_string(index=False))
        return 0

    except Exception as error:
        print(f"PR parser failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

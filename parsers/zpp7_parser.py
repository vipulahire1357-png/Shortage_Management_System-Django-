"""Parse an ABB ZPP7 report into one SQLite table per Material Ref Code."""
from __future__ import annotations

import os
import re
import sqlite3
from pathlib import Path
from typing import Optional

PROJECT_ROOT = Path(__file__).resolve().parents[1]
RAW_DIR = Path(os.getenv("ABB_RAW_DIR", PROJECT_ROOT / "data" / "raw")).resolve()
DB_PATH = Path(os.getenv("ABB_ZPP7_DB", PROJECT_ROOT / "data" / "zpp7_breakdown.db")).resolve()

# ZPP7 report columns are separated by tabs in the exported report.
REF_SLNO_IDX = 2
REF_CODE_IDX = 4
REF_DESC_IDX = 7
BOM_SLNO_IDX = 1
BOM_CODE_IDX = 3
BOM_DESC_IDX = 6
REQ_START_IDX = 35  # Req 1; Req 1..Req 6 are contiguous in the report.


def find_zpp7_file(raw_dir: Path = RAW_DIR) -> Path:
    """Find one file whose filename contains ZPP7 (case-insensitive)."""
    matches = sorted(
        p for p in raw_dir.iterdir()
        if p.is_file() and "zpp7" in p.name.casefold()
    ) if raw_dir.exists() else []
    if not matches:
        raise FileNotFoundError(
            f"No filename containing 'ZPP7' found in {raw_dir}. "
            "Place the report in data/raw and try again."
        )
    if len(matches) > 1:
        names = ", ".join(p.name for p in matches)
        raise RuntimeError(
            f"Found multiple ZPP7 files in {raw_dir}: {names}. "
            "Keep only the intended report in data/raw for this run."
        )
    return matches[0]


def read_report(path: Path) -> str:
    """Read the tab-separated SAP export, including UTF-16 .xls exports."""
    raw = path.read_bytes()
    for encoding in ("utf-16", "utf-8-sig", "cp1252", "latin-1"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise UnicodeError(f"Could not decode report: {path}")


def split_fields(line: str) -> list[str]:
    return [part.strip() for part in line.rstrip("\r\n").split("\t")]


def as_number(value: str) -> float:
    value = value.strip().replace(",", "")
    if not value:
        return 0.0
    return float(value)


def extract_ref_materials(lines: list[str]) -> list[dict[str, str]]:
    """Extract Slno, Material Ref Code and Description from the first section."""
    refs: list[dict[str, str]] = []
    current: Optional[dict[str, str]] = None

    for line in lines:
        if "Temporary BOM Explosion" in line:
            break
        fields = split_fields(line)
        is_ref_row = (
            len(fields) > REF_DESC_IDX
            and fields[REF_SLNO_IDX].isdigit()
            and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]*", fields[REF_CODE_IDX] or "") is not None
            and fields[REF_CODE_IDX].upper().startswith("1SYN")
        )
        if is_ref_row:
            current = {
                "slno": fields[REF_SLNO_IDX],
                "ref_code": fields[REF_CODE_IDX],
                "description": fields[REF_DESC_IDX],
            }
            refs.append(current)
        elif current is not None:
            # Some long product descriptions wrap onto the next physical line.
            nonempty = [v for v in fields if v]
            if nonempty and fields and fields[0] and not fields[0].isdigit():
                continuation = fields[0]
                # Only accept a short text continuation, not report metadata.
                if not re.search(r"\b(Date|Page No|Time)\s*:", continuation, re.I):
                    current["description"] = (current["description"] + " " + continuation).strip()

    if not refs:
        raise ValueError("Could not find any Material Ref Codes in the first ZPP7 section.")
    return refs


def extract_bom_materials(lines: list[str]) -> list[dict[str, object]]:
    """Extract material rows and Req 1..Req 6 from the lower BOM table."""
    header_index = None
    for i, line in enumerate(lines):
        if "Req 1" in line and "Req 6" in line and "Material Code" in line:
            header_index = i
            break
    if header_index is None:
        raise ValueError("Could not locate the lower BOM table header containing Req 1 and Req 6.")

    bom: list[dict[str, object]] = []
    i = 0
    body = lines[header_index + 1:]
    while i < len(body):
        line = body[i]
        fields = split_fields(line)
        is_row_start = (
            len(fields) > BOM_CODE_IDX
            and fields[BOM_SLNO_IDX].isdigit()
            and bool(fields[BOM_CODE_IDX])
        )
        if not is_row_start:
            i += 1
            continue

        # SAP sometimes wraps a long description across physical lines. Join
        # continuation lines until the Req columns are present in the record.
        while len(fields) <= REQ_START_IDX + 5 and i + 1 < len(body):
            next_line = body[i + 1]
            next_fields = split_fields(next_line)
            next_is_row_start = (
                len(next_fields) > BOM_CODE_IDX
                and next_fields[BOM_SLNO_IDX].isdigit()
                and bool(next_fields[BOM_CODE_IDX])
            )
            if next_is_row_start:
                break
            line = line + " " + next_line
            i += 1
            fields = split_fields(line)

        if len(fields) > REQ_START_IDX + 5:
            # Skip page headers or other non-data rows by requiring all six Req cells to be numeric/blank.
            req_cells = fields[REQ_START_IDX:REQ_START_IDX + 6]
            try:
                reqs = [as_number(v) for v in req_cells]
            except ValueError:
                i += 1
                continue
            bom.append({
                "slno": int(fields[BOM_SLNO_IDX]),
                "material_code": fields[BOM_CODE_IDX],
                "description": fields[BOM_DESC_IDX],
                "reqs": reqs,
            })
        i += 1

    if not bom:
        raise ValueError("No BOM material rows were found under the lower table header.")
    return bom


def quote_identifier(identifier: str) -> str:
    """Safely quote an SQLite identifier."""
    return '"' + identifier.replace('"', '""') + '"'


def build_database(report_path: Path, db_path: Path = DB_PATH) -> dict[str, object]:
    text = read_report(report_path)
    lines = text.splitlines()
    refs = extract_ref_materials(lines)
    bom = extract_bom_materials(lines)

    if len(refs) < 4:
        raise ValueError(f"Expected at least 4 Material Ref Codes, found {len(refs)}.")
    if len(refs) > 4:
        print(f"Warning: found {len(refs)} Material Ref Codes; mapping only the first four to Req 1..Req 4 as instructed.")

    # Explicit user-approved mapping: first ref -> Req 1, second -> Req 2, etc.
    mapped_refs = refs[:4]
    db_path.parent.mkdir(parents=True, exist_ok=True)
    if db_path.exists():
        db_path.unlink()  # Rebuild cleanly so old rows cannot remain after a rerun.

    with sqlite3.connect(db_path) as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("""
            CREATE TABLE parser_metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        """)
        conn.executemany(
            "INSERT INTO parser_metadata(key, value) VALUES (?, ?)",
            [
                ("source_file", report_path.name),
                ("mapping_rule", "first four Material Ref Codes map to Req 1 through Req 4; Req 5 and Req 6 ignored"),
                ("bom_row_count", str(len(bom))),
            ],
        )

        for ref_index, ref in enumerate(mapped_refs):
            req_index = ref_index  # Req 1..Req 4; Req 5 and Req 6 are intentionally ignored.
            table = quote_identifier(ref["ref_code"])
            conn.execute(f"""
                CREATE TABLE {table} (
                    "Material Ref Code" TEXT NOT NULL,
                    "MRC Description" TEXT,
                    "Material Code" TEXT NOT NULL,
                    "Material Description" TEXT,
                    "Req" REAL NOT NULL DEFAULT 0
                )
            """)
            rows = [
                (
                    ref["ref_code"], ref["description"],
                    str(item["material_code"]), str(item["description"]),
                    float(item["reqs"][req_index]),
                )
                for item in bom
            ]
            conn.executemany(
                f"INSERT INTO {table} ("
                '"Material Ref Code", "MRC Description", '
                '"Material Code", "Material Description", "Req") '
                "VALUES (?, ?, ?, ?, ?)",
                rows,
            )
            conn.execute(f"CREATE INDEX {quote_identifier('idx_' + ref['ref_code'] + '_material')} ON {table}(" + '"Material Code")')

    return {
        "source_file": str(report_path),
        "database": str(db_path),
        "ref_count_found": len(refs),
        "ref_codes_mapped": [r["ref_code"] for r in mapped_refs],
        "bom_material_rows": len(bom),
        "tables_created": len(mapped_refs),
        "ignored_columns": ["Req 5", "Req 6"],
    }


def main() -> None:
    source = find_zpp7_file()
    result = build_database(source)
    print("ZPP7 parsing complete.")
    for key, value in result.items():
        print(f"{key}: {value}")


if __name__ == "__main__":
    main()

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
BOM_SLNO_IDX = 1
BOM_CODE_IDX = 3
BOM_DESC_IDX = 6


def find_zpp7_file(raw_dir: Path = RAW_DIR) -> Path:
    """Find a ZPP7 report regardless of capitalization, suffix, or subfolder.

    Prioritises ``data/raw/`` so that when reports are staged there the
    parser does not also pick up originals in ``projects/``.
    """
    all_roots = [raw_dir, PROJECT_ROOT / "projects"]

    def _gather(roots):
        return sorted({
            p.resolve() for root in roots if root.exists()
            for p in root.rglob("*")
            if p.is_file() and "zpp7" in p.stem.casefold()
            and p.suffix.casefold() in {".xls", ".xlsx", ".xlsm", ".txt", ".tsv", ".csv"}
            and not p.name.startswith("~$")
        })

    # Search raw_dir first; only fall back to projects/ when nothing found
    matches = _gather([raw_dir])
    if not matches:
        matches = _gather(all_roots)

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
    """Extract only Material Ref Codes from the first section."""
    refs: list[dict[str, str]] = []
    current: Optional[dict[str, str]] = None

    for line in lines:
        if "Temporary BOM Explosion" in line:
            break
        fields = split_fields(line)
        is_ref_row = (
            len(fields) > REF_CODE_IDX
            and fields[REF_SLNO_IDX].isdigit()
            and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]*", fields[REF_CODE_IDX] or "") is not None
            and fields[REF_CODE_IDX].upper().startswith("1SYN")
        )
        if is_ref_row:
            current = {
                "slno": fields[REF_SLNO_IDX],
                "ref_code": fields[REF_CODE_IDX],
            }
            refs.append(current)

    if not refs:
        raise ValueError("Could not find any Material Ref Codes in the first ZPP7 section.")
    return refs


def extract_bom_materials(lines: list[str]) -> list[dict[str, object]]:
    """Extract BOM rows and every numbered Req column in the lower table."""
    header_index = None
    for i, line in enumerate(lines):
        if re.search(r"\bReq\s*1\b", line, re.I) and "Material Code" in line:
            header_index = i
            break
    if header_index is None:
        raise ValueError("Could not locate the lower BOM table header containing Material Code and Req 1.")

    header_fields = split_fields(lines[header_index])
    req_indexes = [i for i, value in enumerate(header_fields) if re.fullmatch(r"Req\s*\d+", value, re.I)]
    req_indexes.sort(key=lambda idx: int(re.search(r"\d+", header_fields[idx]).group()))
    if not req_indexes:
        raise ValueError("No numbered Req columns found in the ZPP7 table header.")

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
        while len(fields) <= req_indexes[-1] and i + 1 < len(body):
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

        if len(fields) > req_indexes[-1]:
            req_cells = [fields[idx] for idx in req_indexes]
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

    if len(refs) > len(bom[0]["reqs"]):
        raise ValueError(f"Found {len(refs)} Material Ref Codes but only {len(bom[0]['reqs'])} Req columns; refusing an ambiguous mapping.")
    # The report may offer more Req slots than entered product references.
    # Only the first N Req slots correspond to the N references in its first section.
    for item in bom:
        item["reqs"] = item["reqs"][:len(refs)]
    mapped_refs = refs
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("BEGIN IMMEDIATE")
        existing_tables = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
        for (name,) in existing_tables:
            conn.execute(f"DROP TABLE {quote_identifier(name)}")
        for ref_index, ref in enumerate(mapped_refs):
            req_index = ref_index
            table = quote_identifier(ref["ref_code"])
            conn.execute(f"""
                CREATE TABLE {table} (
                    "Material Ref Code" TEXT NOT NULL,
                    "Material Code" TEXT NOT NULL,
                    "Description" TEXT,
                    "ZPP7 Quantity" REAL NOT NULL DEFAULT 0,
                    PRIMARY KEY ("Material Ref Code", "Material Code")
                )
            """)
            rows = [
                (
                    ref["ref_code"], str(item["material_code"]), str(item["description"]),
                    float(item["reqs"][req_index]),
                )
                for item in bom
            ]
            conn.executemany(
                f"INSERT INTO {table} ("
                '"Material Ref Code", "Material Code", "Description", "ZPP7 Quantity") '
                "VALUES (?, ?, ?, ?)",
                rows,
            )
    staging_db = db_path.with_name(db_path.name + ".tmp")
    if staging_db.exists():
        staging_db.unlink()

    return {
        "source_file": str(report_path),
        "database": str(db_path),
        "ref_count_found": len(refs),
        "ref_codes_mapped": [r["ref_code"] for r in mapped_refs],
        "bom_material_rows": len(bom),
        "tables_created": len(mapped_refs),
        "quantity_columns": len(bom[0]["reqs"]),
    }


def main() -> None:
    source = find_zpp7_file()
    result = build_database(source)
    print("ZPP7 parsing complete.")
    for key, value in result.items():
        print(f"{key}: {value}")


if __name__ == "__main__":
    main()


# Format.md — ABB Shortage Management System
## Input Report Formats, Naming Rules, Parsing Specifications, and Database Schemas

**Source of truth:** Derived from live code analysis of `parsers/`, `services/`, `shortage/`, and `abbsite/`, and live inspection of `data/*.db`.
**Last updated:** October 2026  
**Version:** ABB SM 0.5

---

## Table of Contents

1. [System Overview](#1-system-overview)
2. [Processing Pipeline and Execution Order](#2-processing-pipeline-and-execution-order)
3. [File Locations and Upload Methods](#3-file-locations-and-upload-methods)
4. [Report 1 — ZPP7 (BOM Requirements)](#4-report-1--zpp7-bom-requirements)
5. [Report 2 — MB25 (WBS Element Mapping)](#5-report-2--mb25-wbs-element-mapping)
6. [Report 3 — MB52 (Warehouse / Unrestricted Stock)](#6-report-3--mb52-warehouse--unrestricted-stock)
7. [Report 4 — ME2J (Purchase Orders)](#7-report-4--me2j-purchase-orders)
8. [Report 5 — PR (Purchase Requisitions)](#8-report-5--pr-purchase-requisitions)
9. [Report 6 — Scope List (Material Scope Classification)](#9-report-6--scope-list-material-scope-classification)
10. [File Naming Quick-Reference Table](#10-file-naming-quick-reference-table)
11. [Database Schemas](#11-database-schemas)
12. [Net Shortage Calculation](#12-net-shortage-calculation)
13. [Input Validation and Error Handling](#13-input-validation-and-error-handling)
14. [Django Upload and Dashboard Behavior](#14-django-upload-and-dashboard-behavior)
15. [Project vs. Global Reports](#15-project-vs-global-reports)

---

## 1. System Overview

The ABB Shortage Management System processes seven SAP-exported reports, parses them into three SQLite databases, and computes a net-shortage table served in server-rendered Django pages.

```
SAP exports (7 report types)
        |
        v
  data/raw/             <- staging area for all input files
        |
   +----+--------------------------------------------+
   |  parsers/ (one per report)                      |
   |  mb25_parser    -->  global.db / wbs_element    |
   |  mb52_parser    -->  global.db / Storage        |
   |  me2j_parser    -->  global.db / ME2J           |
   |  me2m_parser   -->  global.db / ME2M          |
  |  pr_parser      -->  global.db / PR             |
   |  scopelist_parser -> global.db / scopelist      |
   |  zpp7_parser    -->  zpp7_breakdown.db          |
   +-------------------------------------------------+
        |
        v
  services/shortage_service.py
        |
        v
  services.db / net_shortage   <- dashboard output table
```

---

## 2. Processing Pipeline and Execution Order

All seven report parsers must run successfully before the shortage service executes. A single parser failure aborts shortage generation.

| Step | Parser / Service | Output DB | Output Table | Mandatory |
|------|-----------------|-----------|--------------|-----------|
| 1 | `mb25_parser` | `global.db` | `wbs_element` | Yes |
| 2 | `mb52_parser` | `global.db` | `Storage` | Yes |
| 3 | `me2j_parser` | `global.db` | `ME2J` | Yes |
| 4 | `me2m_parser` | `global.db` | `ME2M` | Yes |
| 5 | `pr_parser` | `global.db` | `PR` | Yes |
| 6 | `scopelist_parser` | `global.db` | `scopelist` | Yes |
| 7 | `zpp7_parser` | `zpp7_breakdown.db` | one table per MRC | Yes |
| 8 | `shortage_service` | `services.db` | `net_shortage` | Yes |

**Parser execution order** (fixed by `parsers/__init__.py` `PARSER_MODULES` dict):
```
mb25 -> mb52 -> me2j -> me2m -> pr -> scopelist -> zpp7
```

**CLI:**
```
python main.py                        # run all parsers + shortage service
python main.py --report zpp7          # single parser only (no shortage)
python main.py --report all --skip-shortage   # parsers only
```

---

## 3. File Locations and Upload Methods

### 3.1 Staging Directory

Place project-specific files in the project folder and shared files in `global/` before processing. The processing action copies selected inputs into `data/raw/`.

Files arrive there via:
1. **Manual copy** — place project-specific reports in `projects/{project_id}/` and shared reports in `global/`.
2. **Django form upload** — use Input & Imports to save a report under the selected project or shared `global/` folder. Existing files are not overwritten.
3. **Project workflow** — reports from `projects/{project_id}/` and `global/` are staged to `data/raw/` for parser processing, then pre-existing staged reports are restored.

### 3.2 Source Folders by Report (Project Workflow)

| Report | Source Folder |
|--------|--------------|
| ZPP7 | `projects/{project_id}/` |
| MB25 | `projects/{project_id}/` |
| ME2J | `projects/{project_id}/` |
| ME2M | `projects/{project_id}/` |
| PR | `projects/{project_id}/` |
| MB52 | `global/` |
| Scope List | `global/` |

---

## 4. Report 1 — ZPP7 (BOM Requirements)

### 4.1 Business Purpose

ZPP7 is ABB's Bill-of-Material (BOM) explosion report. It lists every component material and its required quantity for each Material Reference Code (MRC / top-level assembly). The ZPP7 `Req` quantity is the **primary demand** input for net-shortage calculation.

### 4.2 File Discovery

```python
# parsers/zpp7_parser.py  find_zpp7_file()
"zpp7" in filename.casefold()   # case-insensitive substring, full filename
```

- Exactly **one** file must match. Multiple matches → `RuntimeError`. No match → `FileNotFoundError`.
- Observed file: `ZPP7.xls` (1.3 MB)

### 4.3 Supported File Extensions

| Extension | Parser Accepts | Upload Form Accepts | Actual Format |
|-----------|---------------|-------------|---------------|
| `.xls` | Yes (primary) | Yes | SAP tab-delimited UTF-16 text — NOT a real Excel binary |
| `.xlsx` | No (parser reads as text; may work) | Yes | Real Excel |
| `.xlsm` | No | Yes | Real Excel macro |
| `.txt` | No (parser reads as text; may work) | Yes | Tab-delimited |
| `.csv` | No | Yes | Comma-separated |
| `.htm/.html` | No | No | Not supported |

> **Critical:** The ZPP7 `.xls` file produced by SAP is a **raw tab-delimited UTF-16 text file** with an `.xls` extension. Do not convert it to `.xlsx` before processing — the parser reads raw bytes and decodes, not via openpyxl/xlrd.

**Encoding detection order:** `utf-16` > `utf-8-sig` > `cp1252` > `latin-1`

### 4.4 Report Structure

The file has **two distinct sections** within one flat text file:

**Section 1 — MRC Header Block:**
- Rows where column index 2 is a digit (serial number), column index 4 matches `[A-Za-z0-9][A-Za-z0-9-]*` and **starts with `1SYN`** → MRC row.
- Ends at the line containing `"Temporary BOM Explosion"`.
- Long descriptions may wrap to the next physical line (joined with a space).

**Section 2 — BOM Table:**
- Header line contains `"Req 1"`, `"Req 6"`, and `"Material Code"` on one line.
- Data rows: column index 1 (BOM serial number) is a digit, column index 3 is the material code.
- Req quantities are at **contiguous tab positions starting at index 35**.

### 4.5 Column Index Map (tab positions)

| Constant | Index | Content |
|----------|-------|---------|
| REF_SLNO_IDX | 2 | MRC serial number |
| REF_CODE_IDX | 4 | Material Reference Code (e.g., `1SYN126019R001A`) |
| REF_DESC_IDX | 7 | MRC description |
| BOM_SLNO_IDX | 1 | BOM row serial number |
| BOM_CODE_IDX | 3 | BOM material code |
| BOM_DESC_IDX | 6 | BOM material description |
| REQ_START_IDX | 35 | Req 1; Req 1..Req 6 are contiguous |

### 4.6 MRC-to-Req Mapping Rule

The parser finds all MRCs in Section 1 and maps the first 4 by position:

| MRC found | Maps to Req column |
|-----------|-------------------|
| 1st | Req 1 (index 35) |
| 2nd | Req 2 (index 36) |
| 3rd | Req 3 (index 37) |
| 4th | Req 4 (index 38) |
| 5th+ | Ignored (warning printed) |
| Req 5, Req 6 | Always ignored |

**Validation:** Fewer than 4 MRCs → `ValueError`. Req cell non-numeric → row skipped.

### 4.7 Destination: `zpp7_breakdown.db`

Entire database is deleted and recreated on every run.

**Schema — one table per MRC (up to 4 tables):**
```sql
CREATE TABLE "{MRC_Code}" (
    "Material Ref Code"    TEXT NOT NULL,
    "MRC Description"      TEXT,
    "Material Code"        TEXT NOT NULL,
    "Material Description" TEXT,
    "Req"                  REAL NOT NULL DEFAULT 0
);
CREATE INDEX "idx_{MRC_Code}_material" ON "{MRC_Code}" ("Material Code");
```

**Metadata table:**
```sql
CREATE TABLE parser_metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
-- Keys: source_file, mapping_rule, bom_row_count
```

**Live tables (actual database):**
| Table | Row Count | Description |
|-------|-----------|-------------|
| `1SYN126019R001A` | 1,584 | Main LV Switchgear |
| `1SYN226019R001A` | 1,584 | Stage-1 Process MCC Panel |
| `1SYN226019R002A` | 1,584 | Stage-2 MRU Compr MCC Panel |
| `1SYN426019R001A` | 1,584 | Operational & Comm Spares |

### 4.8 Column Specification

| Source | DB Column | Required | Type | Example |
|--------|-----------|----------|------|---------|
| Tab idx 4 | Material Ref Code | Yes | TEXT | `1SYN126019R001A` |
| Tab idx 7 | MRC Description | No | TEXT | `0084SWG0001(MAIN LV SWGR)` |
| Tab idx 3 | Material Code | Yes | TEXT | `1MYN528631-004` |
| Tab idx 6 | Material Description | No | TEXT | `MO Resistor 22K/10W, 5%` |
| Tab 35+N | Req | Yes | REAL | `11.0` |

---

## 5. Report 2 — MB25 (WBS Element Mapping)

### 5.1 Business Purpose

MB25 maps each Material Reference Code (MRC) to its WBS Element. This mapping is the essential join key between ZPP7 BOM requirements and project-specific stock and PO data.

### 5.2 File Discovery

```python
# parsers/mb25_parser.py  find_mb25_file()
"mb25" in path.name.lower()          # full filename incl. extension
extensions = {".xlsx", ".xls", ".xlsm"}
```

Observed file: `MB25.XLSX` (12 KB)

### 5.3 Supported Extensions

`.xlsx`, `.xls`, `.xlsm` only. No HTML, CSV, or text.

### 5.4 Header Detection

Scans first 50 rows of each sheet. A row is the header if it contains all three required columns (any accepted alias). No sheet-name constraint.

### 5.5 Column Aliases

| Canonical | Accepted Aliases (normalized) |
|-----------|-------------------------------|
| Material | `material`, `material number`, `material no`, `material code` |
| Material Description | `material description`, `material short text`, `short text`, `description` |
| WBS Element | `wbs element`, `wbs`, `work breakdown structure element`, `work breakdown structure` |

**Normalization:** lowercase → collapse `[\s_\-]+` to space → remove non-alphanumeric.

### 5.6 Output Mapping

| Source Column | DB Column | Notes |
|--------------|-----------|-------|
| Material (any alias) | `Material Ref Code` | This is the MRC, NOT a component code |
| Material Description | `MRC Description` | Panel/assembly name |
| WBS Element | `WBS Element` | e.g., `IN-10026019.A.1` |

### 5.7 Destination: `global.db` / `wbs_element`

```sql
CREATE TABLE wbs_element (
    "Material Ref Code" TEXT,
    "MRC Description"   TEXT,
    "WBS Element"       TEXT
);
```

Refresh: DELETE + INSERT. Row count: 4 (live). Other tables untouched.

### 5.8 Column Specification

| Source | DB Column | Required | Type | Example | Notes |
|--------|-----------|----------|------|---------|-------|
| Material | Material Ref Code | Yes | TEXT | `1SYN126019R001A` | MRC not component |
| Material Description | MRC Description | No | TEXT | `0084SWG0001(MAIN LV SWGR)` | |
| WBS Element | WBS Element | Yes | TEXT | `IN-10026019.A.1` | |

---

## 6. Report 3 — MB52 (Warehouse / Unrestricted Stock)

### 6.1 Business Purpose

MB52 is SAP's warehouse stock report. Unrestricted stock per material + WBS = Project Stock Quantity. Unrestricted stock with no WBS = Free Stock. Both are outputs of the shortage calculation.

### 6.2 File Discovery

```python
# parsers/mb52_parser.py  find_report_file()
# Tries longer prefix first, falls back to shorter
candidates = matches("in_mm_innr_mb52") or matches("mb52")
extensions = {".htm", ".html", ".xlsx", ".xls", ".xlsm"}
```

Observed files: `Job IN_MM_INNR_MB52_D_458330, Step 1.htm` AND `MB52.htm` (keep only one).

### 6.3 Supported Extensions

| Extension | Supported | Notes |
|-----------|-----------|-------|
| `.htm` / `.html` | Yes | SAP HTML export — most common |
| `.xlsx` | Yes | Real Excel |
| `.xls` | Yes | Real Excel binary or HTML-in-XLS |
| `.xlsm` | Yes | |
| `.csv` / `.txt` | No | Not supported |

### 6.4 Header Detection

- **HTML:** `pd.read_html()` reads all tables; each scanned at rows 0–9 for all four required columns.
- **Excel:** Each sheet scanned at rows 0–49.
- SAP repeats column headers on every page — these are detected and removed by signature.

### 6.5 Column Aliases

| Canonical | Accepted Aliases (normalized) |
|-----------|-------------------------------|
| Material | `material`, `material number`, `material no`, `material code` |
| Material Description | `material description`, `material short text`, `short text`, `description` |
| Unrestricted | `unrestricted`, `unrestricted stock` |
| WBS Element | `wbs element`, `wbs`, `work breakdown structure element`, `work breakdown structure` |

### 6.6 Output Mapping

| Source | DB Column | Notes |
|--------|-----------|-------|
| Material | `Material Code` | Text |
| Material Description | `Material Description` | Text |
| Unrestricted | `Unrestricted` | **Stored as TEXT** — converted to float at join time |
| WBS Element | `WBS Element` | NULL allowed (= free warehouse stock) |

### 6.7 Free Stock vs. Project Stock (shortage service behavior)

- Row with WBS Element set → `storage_totals[(wbs, material)]` → `Project Stock Quantity`
- Row with WBS Element null/empty → `free_stock_totals[material]` → `Free Stock`

### 6.8 Destination: `global.db` / `Storage`

```sql
CREATE TABLE "Storage" (
    "Material Code"        TEXT,
    "Material Description" TEXT,
    "Unrestricted"         TEXT,
    "WBS Element"          TEXT
);
```

Refresh: DELETE + INSERT. Row count: ~832 (live).

### 6.9 Column Specification

| Source | DB Column | Required | Type | Example | Notes |
|--------|-----------|----------|------|---------|-------|
| Material | Material Code | Yes | TEXT | `1MYN528631-004` | |
| Material Description | Material Description | No | TEXT | `MO Resistor 22K/10W, 5%` | |
| Unrestricted | Unrestricted | Yes | TEXT (stored) | `24` or `0` | Blank → 0.0 at join |
| WBS Element | WBS Element | No | TEXT | `IN-10026019.A.1` or NULL | NULL = free stock |

---

## 7. Report 4 — ME2J (Purchase Orders)

### 7.1 Business Purpose

ME2J is SAP's open purchase order report. Provides ordered and still-to-be-delivered quantities per material per WBS → feeds `PO Quantity` and `Open PO Quantity` in the shortage table.

### 7.2 File Discovery

```python
# parsers/me2j_parser.py  find_report()
"me2j" in path.stem.lower()          # stem only, not full filename
extensions = {".xlsx", ".xls", ".xlsm"}
```

Observed file: `ME2J.XLSX` (32 KB)

### 7.3 Supported Extensions

`.xlsx`, `.xls`, `.xlsm` only. No HTML, CSV, text.

### 7.4 Header Detection

Header is assumed to be **row 0** (first row). All sheets read simultaneously with `header=0`. Sheets lacking all required columns are skipped.

### 7.5 Required Source Columns (exact, case/space insensitive)

| Source Header | DB Column |
|--------------|-----------|
| Material | Material Code |
| Short Text | Description |
| Vendor/supplying plant | Vendor |
| Order Quantity | Order Quantity |
| Still to be delivered (qty) | Open Quantity |
| WBS Element | WBS Element |

All required source columns are mandatory. If no sheet has all required columns → `ValueError`.

### 7.6 Destination: `global.db` / `ME2J`

```sql
CREATE TABLE "ME2J" (
    "Material Code"  TEXT,
    "Description"    TEXT,
    "Vendor"         TEXT,
    "Order Quantity" TEXT,
    "Open Quantity"  TEXT,
    "WBS Element"    TEXT
);
```

Refresh: DROP + CREATE (via `ME2J_new` rename). Row count: ~114 (live).

**Note:** `Order Quantity` and `Open Quantity` are stored as TEXT; converted to float by shortage service.

### 7.7 Column Specification

| Source | DB Column | Required | Type | Example |
|--------|-----------|----------|------|---------|
| Material | Material Code | Yes | TEXT | `1SAJ650000R0101` |
| Short Text | Description | No | TEXT | `VI150.0 Volt.-module...` |
| Vendor/supplying plant | Vendor | Yes | TEXT | `701501026 ABB India Limited` |
| Order Quantity | Order Quantity | Yes | TEXT→REAL | `30` |
| Still to be delivered (qty) | Open Quantity | Yes | TEXT→REAL | `30` |
| WBS Element | WBS Element | Yes | TEXT | `IN-10026019.A.2` |

---

### ME2M — Vendor / Material Details

ME2M report workbooks (`.xls`, `.xlsx`, `.xlsm`, `.xlsb`) are detected by a filename containing `ME2M`. The parser reads the `Material`, `Short Text`, and `Vendor/supplying plant` columns into the `ME2M` table in `global.db` as Material Code, Description, and Vendor Detail. The shortage service uses Vendor Detail by Material Code and retains the first non-empty value in report order when repeated entries differ.

---
## 8. Report 5 — PR (Purchase Requisitions)

### 8.1 Business Purpose

PR lists open purchase requisitions. `PR Quantity` appears in the shortage table as an informational field. In the current implementation it is **not subtracted** from Net Shortage.

### 8.2 File Discovery

```python
# parsers/pr_parser.py  find_report()
extensions = {".xls", ".xlsx", ".xlsm", ".txt", ".tsv", ".csv"}
# Accepted stems (case-insensitive):
stem in {"pr", "pr 1", "purchase requisition"}
or stem.startswith("pr ")   # space after PR
or stem.startswith("pr_")   # underscore after PR
or "purchase requisition" in stem
```

Observed file: `PR.XLSX` (15 KB)

### 8.3 Supported Extensions

`.xls`, `.xlsx`, `.xlsm`, `.txt`, `.tsv`, `.csv`

### 8.4 Format Auto-Detection

```python
is_text_export = (
    prefix.startswith((b"\xff\xfe", b"\xfe\xff"))     # UTF-16 BOM
    or (suffix not in {".xlsx", ".xlsm"} and b"\x00" in prefix)
)
```

- Text path: decode UTF-16 / UTF-8-sig / CP1252; scan lines for header row.
- Excel path: `pd.read_excel(header=0)` + sheet scan.

### 8.5 Required Columns and Aliases

| Primary | Alias | DB Column |
|---------|-------|-----------|
| Material | Material | Material Code |
| Short Text | Short Text | Description |
| Quantity requested | Quantity | PR Quantity |
| WBS Element | WBS Element | WBS Element |

### 8.6 Quantity Cleaning

- Strip, remove commas and spaces, parse as float.
- Whole-number float → stored as int.
- Invalid text → `ValueError` (parser aborts).
- Blank → `None`.

### 8.7 Row Filtering

- Rows where `Material Code` = null **or** `WBS Element` = null are **dropped**.
- Repeated header rows (Material Code normalizes to `"material"`) removed.
- All rows dropped → `ValueError`.

### 8.8 Destination: `global.db` / `PR`

```sql
CREATE TABLE "PR" (
    "Material Code" TEXT,
    "Description"   TEXT,
    "PR Quantity"   REAL,
    "WBS Element"   TEXT
);
```

Refresh: DELETE + INSERT in transaction. Row count: ~91 (live).

### 8.9 Column Specification

| Source | DB Column | Required | Type | Example | Notes |
|--------|-----------|----------|------|---------|-------|
| Material | Material Code | Yes | TEXT | `1SAJ260000R0100` | Drop if null |
| Short Text | Description | No | TEXT | `MTQ22-FBP.0 ModbusTCP...` | |
| Quantity requested / Quantity | PR Quantity | Yes | REAL | `1.0` | Invalid → abort |
| WBS Element | WBS Element | Yes | TEXT | `IN-10026019.A.3` | Drop if null |

---

## 9. Report 6 — Scope List (Material Scope Classification)

### 9.1 Business Purpose

Classifies each material as `ABB`, `Vendor Scope`, or other. Only `ABB` and `Vendor Scope` materials flow into the net-shortage table. Others (`Header`, `Not Required`, etc.) are excluded.

### 9.2 File Discovery

```python
# parsers/scopelist_parser.py  find_input_file()
"material_scope_list" in path.name.lower()   # full filename incl. extension
extensions = {".xlsx", ".xls", ".xlsm"}
```

Observed file: `MATERIAL_SCOPE_LIST.xlsx` (3.4 MB, 84,511 rows)

### 9.3 Supported Extensions

`.xlsx`, `.xls`, `.xlsm` only.

### 9.4 Header Detection

Scans first 50 rows of each sheet. Requires all three columns in the same row.

### 9.5 Required Columns

| Accepted Source Values | DB Column | Detection |
|-----------------------|-----------|-----------|
| `material` or `material code` | Material Code | Exact normalized match |
| `description` or `material description` | Description | Exact normalized match |
| Header **starting with** `final scope finalization` OR `final scope finalisation` | Scope | Prefix match; any suffix allowed |

> The scope column header may contain extra text after the prefix (e.g., `"Final Scope Finalization Status"`). Only the prefix is checked.

### 9.6 Scope Value Handling (in shortage service)

| Scope in file | Normalized | Included | Priority |
|--------------|-----------|----------|---------|
| `ABB` | ABB | Yes | 0 (highest) |
| `Vendor Scope` | VENDOR SCOPE | Yes | 1 |
| Any other | — | No | — |

When a material appears multiple times: lower priority number (ABB) wins.

### 9.7 Destination: `global.db` / `scopelist`

```sql
CREATE TABLE scopelist (
    "Material Code" TEXT,
    "Description"   TEXT,
    "Scope"         TEXT
);
```

Refresh: DELETE + INSERT. Row count: ~84,511 (live).

### 9.8 Column Specification

| Source | DB Column | Required | Type | Example |
|--------|-----------|----------|------|---------|
| Material / Material Code | Material Code | Yes | TEXT | `1SAJ650000R0101` |
| Description / Material Description | Description | No | TEXT | `VI150.0 Volt.-module...` |
| Final Scope Finalization... | Scope | Yes | TEXT | `ABB` or `Vendor Scope` |

---

## 10. File Naming Quick-Reference Table

| Report | Key | Match Rule | Match Target | Parser Extensions | Upload Form Extensions | Mandatory |
|--------|-----|-----------|-------------|------------------|----------------|-----------|
| ZPP7 | `zpp7` | contains `zpp7` | full filename | `.xls` (primary) | `.xls`, `.xlsx`, `.xlsm`, `.txt`, `.csv` | Yes |
| MB25 | `mb25` | contains `mb25` | full filename | `.xlsx`, `.xls`, `.xlsm` | `.xls`, `.xlsx`, `.xlsm` | Yes |
| MB52 | `mb52` | contains `in_mm_innr_mb52` OR `mb52` | full filename | `.htm`, `.html`, `.xlsx`, `.xls`, `.xlsm` | `.htm`, `.html`, `.xls`, `.xlsx`, `.xlsm` | Yes |
| ME2J | `me2j` | contains `me2j` | **stem only** | `.xlsx`, `.xls`, `.xlsm` | `.xls`, `.xlsx`, `.xlsm` | Yes |
| ME2M | `me2m` | contains `me2m` | full filename | `.xls`, `.xlsx`, `.xlsm`, `.xlsb` | `.xls`, `.xlsx`, `.xlsm`, `.xlsb` | Yes |
| PR | `pr` | stem = `pr`/`pr 1`/`purchase requisition`, starts `pr `/`pr_`, contains `purchase requisition` | **stem only** | `.xls`, `.xlsx`, `.xlsm`, `.txt`, `.tsv`, `.csv` | `.xls`, `.xlsx`, `.xlsm`, `.txt`, `.tsv`, `.csv` | Yes |
| Scope List | `scopelist` | contains `material_scope_list` | full filename | `.xlsx`, `.xls`, `.xlsm` | `.xls`, `.xlsx`, `.xlsm` | Yes |

**Global rules:**
- All matching is **case-insensitive**.
- Files starting with `~$` (temp/lock files) are excluded.
- More than one match per report type → `RuntimeError` (parser aborts).
- The upload form accepts only the extension formats listed for each report in `shortage/data.py`.

---

## 11. Database Schemas

### 11.1 `data/global.db` — Populated by six parsers

Each parser refreshes only its own table.

#### `wbs_element` (from MB25)
```sql
CREATE TABLE wbs_element (
    "Material Ref Code" TEXT,  -- MRC id, e.g. 1SYN126019R001A
    "MRC Description"   TEXT,  -- Assembly name
    "WBS Element"       TEXT   -- e.g. IN-10026019.A.1
);
```
Live: 4 rows | Refresh: DELETE + INSERT

#### `Storage` (from MB52)
```sql
CREATE TABLE "Storage" (
    "Material Code"        TEXT,
    "Material Description" TEXT,
    "Unrestricted"         TEXT,  -- qty stored as text; converted at join
    "WBS Element"          TEXT   -- NULL = free warehouse stock
);
```
Live: ~832 rows | Refresh: DELETE + INSERT

#### `ME2J` (from ME2J parser)
```sql
CREATE TABLE "ME2J" (
    "Material Code"  TEXT,
    "Description"    TEXT,
    "Vendor"         TEXT,
    "Order Quantity" TEXT,   -- total PO qty (text)
    "Open Quantity"  TEXT,   -- still-to-deliver (text)
    "WBS Element"    TEXT
);
```
Live: ~114 rows | Refresh: DROP TABLE + CREATE via rename

#### `PR` (from PR parser)
```sql
CREATE TABLE "PR" (
    "Material Code" TEXT,
    "Description"   TEXT,
    "PR Quantity"   REAL,   -- parsed to numeric at parse time
    "WBS Element"   TEXT
);
```
Live: ~91 rows | Refresh: DELETE + INSERT in transaction

#### `scopelist` (from Scope List parser)
```sql
CREATE TABLE scopelist (
    "Material Code" TEXT,
    "Description"   TEXT,
    "Scope"         TEXT   -- 'ABB', 'Vendor Scope', etc.
);
```
Live: ~84,511 rows | Refresh: DELETE + INSERT

---

### 11.2 `data/zpp7_breakdown.db` — Rebuilt on every ZPP7 run

#### `parser_metadata`
```sql
CREATE TABLE parser_metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
```
Keys: `source_file`, `mapping_rule`, `bom_row_count`

#### `{MRC_Code}` — one table per MRC (up to 4)
```sql
CREATE TABLE "{MRC_Code}" (
    "Material Ref Code"    TEXT NOT NULL,
    "MRC Description"      TEXT,
    "Material Code"        TEXT NOT NULL,
    "Material Description" TEXT,
    "Req"                  REAL NOT NULL DEFAULT 0
);
CREATE INDEX "idx_{MRC_Code}_material" ON "{MRC_Code}" ("Material Code");
```

---

### 11.3 `data/services.db` — Output of shortage service

#### `net_shortage`
```sql
CREATE TABLE "net_shortage" (
    "Material Code"          TEXT,
    "Material Description"   TEXT,
    "ZPP7 Quantity"          REAL,   -- aggregated Req from ZPP7
    "Project Stock Quantity" REAL,   -- unrestricted stock at WBS (MB52)
    "PO Quantity"            REAL,   -- total order qty (ME2J)
    "Open PO Quantity"       REAL,   -- still-to-deliver qty (ME2J)
    "PR Quantity"            REAL,   -- sum of PR qty (PR table)
    "Net Shortage"           REAL,   -- max(ZPP7 - Project Stock, 0)
    "Free Stock"             REAL,   -- unrestricted stock with no WBS (MB52)
    "Final Date"              TEXT,   -- vendor accepted date
    "Remarks"                TEXT,   -- editable notes
    "Material Ref Code"      TEXT,
    "WBS Element"            TEXT,
    "Scope"                  TEXT    -- 'ABB' or 'Vendor Scope'
);
```

Output grain: `(Material Ref Code, WBS Element, Material Code)`  
Live: ~4,084 rows | Refresh: atomic rename via `_netshortage_table_new`

**Template field mapping:**

| DB Column | Template Field |
|-----------|-----------|
| Material Ref Code | material_ref_code |
| WBS Element | wbs_element |
| Material Code | material_code |
| Material Description | material_description |
| ZPP7 Quantity | zpp7_quantity |
| Project Stock Quantity | project_stock_quantity |
| PO Quantity | po_quantity |
| Open PO Quantity | open_po_quantity |
| PR Quantity | pr_quantity |
| Net Shortage | net_shortage |
| Free Stock | free_stock |
| Remarks | remarks |
| Scope | scope |

---

## 12. Net Shortage Calculation

### 12.1 Verified Formula (from `services/shortage_service.py` line 376)

```python
net_shortage = max(bom["req"] - unrestricted, 0.0)
```

```
Net Shortage = max(ZPP7 Quantity - Project Stock Quantity, 0)
```

This matches the stated business rule.

### 12.2 Field Definitions

| Output Field | Source | Formula |
|-------------|--------|---------|
| ZPP7 Quantity | `zpp7_breakdown.db` | SUM of `Req` for `(MRC, WBS, Material)` |
| Project Stock Quantity | `global.db / Storage` | SUM of `Unrestricted` where WBS is set, for `(WBS, Material)` |
| PO Quantity | `global.db / ME2J` | SUM of `Order Quantity` for `(WBS, Material)` |
| Open PO Quantity | `global.db / ME2J` | SUM of `Open Quantity` for `(WBS, Material)` |
| PR Quantity | `global.db / PR` | SUM of `PR Quantity` for `(WBS, Material)` |
| Net Shortage | Calculated | max(ZPP7 Quantity - Project Stock Quantity, 0) |
| Free Stock | `global.db / Storage` | SUM of `Unrestricted` where WBS is NULL/empty, per `Material` |

### 12.3 Inclusion Filters

Before computing shortage, the service filters:

1. **Scope filter:** Material must be in `scopelist` with Scope = `ABB` or `Vendor Scope`.
2. **WBS filter:** MRC must have at least one WBS Element in `wbs_element`.

Materials failing either filter are **excluded** from `net_shortage`.

### 12.4 Scope Conflict Resolution

If a material appears in `scopelist` with multiple scope values, `ABB` (priority 0) always beats `Vendor Scope` (priority 1).

### 12.5 Free Stock Note

Free Stock is **informational only**. It is NOT subtracted from Net Shortage in the current implementation.

### 12.6 Remarks Column

Always an empty string `""` in the current implementation. Reserved for future use.

---

## 13. Input Validation and Error Handling

### 13.1 File Discovery Errors

| Condition | Error | Pipeline Effect |
|-----------|-------|----------------|
| No file matching the report in `data/raw/` | `FileNotFoundError` | Parser fails; shortage skipped |
| `data/raw/` does not exist | `FileNotFoundError` | Parser fails; shortage skipped |
| Multiple files match | `RuntimeError` | Parser fails; shortage skipped |

### 13.2 Upload Validation

The Django form validates the selected report type and supported extension, rejects empty uploads, and refuses to overwrite an existing filename. Mutating forms include CSRF tokens. Validation errors are shown in the page and leave existing reports untouched.### 13.3 Column Validation Errors (by parser)

| Parser | Error message |
|--------|--------------|
| MB25 | "Could not find a header row containing Material, Material Description, and WBS Element in any worksheet." |
| MB52 | "Could not find a table containing Material, Material Description, Unrestricted, and WBS Element." |
| ME2J | "Could not find all required columns in the workbook. Required source headers: ..." |
| PR | "PR report is missing required columns: [...]" |
| Scope List | "Could not find headers for Material, Description, and 'Final Scope Finalization/Finalisation...' in any worksheet." |
| ZPP7 | "Could not locate the lower BOM table header containing Req 1 and Req 6." |

### 13.4 ZPP7-Specific Validation

| Condition | Error |
|-----------|-------|
| No MRCs starting with `1SYN` found | `ValueError`: "Could not find any Material Ref Codes in the first ZPP7 section." |
| Fewer than 4 MRCs | `ValueError`: "Expected at least 4 Material Ref Codes, found N." |
| No BOM rows | `ValueError`: "No BOM material rows were found under the lower table header." |
| Non-numeric Req cell | Row silently skipped |

### 13.5 PR Quantity Validation

| Condition | Behavior |
|-----------|---------|
| Non-numeric quantity text | `ValueError("Invalid PR Quantity value: {value!r}")` — parser aborts |
| Blank quantity | Stored as `None` |
| Null Material Code or WBS Element | Row dropped (not an error) |
| All rows dropped | `ValueError("The PR report contains no usable material/WBS rows.")` |

### 13.6 Shortage Service Validation

| Condition | Error |
|-----------|-------|
| `global.db` not found | `FileNotFoundError` → HTTP 503 |
| `zpp7_breakdown.db` not found | `FileNotFoundError` → service fails |
| Required table missing | `RuntimeError("Required table '{name}' was not found...")` |
| Required column missing | `RuntimeError("Table '{name}' is missing required columns: ...")` |
| Non-numeric quantity in source | `ValueError("Non-numeric quantity {v!r} encountered in {context}.")` |
| No rows qualify for net_shortage | `RuntimeError("No rows qualified for net_shortage. Check ZPP7 MRC matches wbs_element and scopelist Scope values contain 'ABB' or 'Vendor Scope'.")` |

### 13.7 Blank and Duplicate Row Handling

| Parser | Handling |
|--------|---------|
| All | `dropna(how="all", subset=OUTPUT_COLUMNS)` — all-blank rows dropped |
| MB52 | SAP repeated page-header rows detected by column-signature match and removed |
| ME2J | Rows where Material Code = `"material"` (normalized) removed |
| PR | Rows with null Material Code OR null WBS Element dropped; repeated headers removed |
| Shortage service | Repeated `(MRC, WBS, Material)` keys are summed, not duplicated |

---

## 14. Django Upload and Dashboard Behavior

### 14.1 Pages and actions

| Page | Purpose |
|------|---------|
| Input & Imports | Select a project, review report readiness, upload source reports, process reports, and export a project register. |
| Shortage Management | Filter by project, Material Ref Code, WBS Element, Material Code, and Scope; search, sort, paginate, edit remarks, and export CSV/XLSX. |
| Vendor Track | Rebuild selected projects, filter records by vendor/WBS, paginate, and export a vendor date request. |
| Vendor Response Import | Select a vendor folder, open its register, save Proposed Dates, and apply accepted Vendor Dates to matching shortage registers. |

### 14.2 Upload file behavior

Upload requires a selected project, a known report type, and a supported extension from `shortage/data.py`. Empty files and unsupported extensions are rejected. Original filenames are preserved and a colliding upload is rejected rather than overwriting the existing report. Project reports are stored in `projects/{project_id}/`; MB52 and Scope List files are stored in `global/`.

Processing copies the selected project and shared reports into the configured raw staging directory for the existing parser pipeline. Existing staged reports are restored when the run completes or fails. Source files in project and shared folders remain in place.

### 14.3 Filter and table behavior

Shortage filters include project, Material Ref Code, WBS Element, Material Code, and Scope. Search checks displayed values; sorting uses the selected output column and direction. The default page size is 25 (selectable up to 100). CSV and XLSX exports use the active filters. Remarks are saved through a CSRF-protected form.

Vendor Track lets users rebuild selected projects, filter by vendor and WBS, and paginate at 50 rows. Vendor date request export requires a single vendor. Vendor Response Import lists workbooks in `Vendor Date Registers/`, saves Proposed Date edits back to the selected workbook, and updates matching shortage registers when selected Vendor Dates are accepted.
## 15. Project vs. Global Reports

### 15.1 Report Classification in Project Workflow

| Report | Source | Detection |
|--------|--------|----------|
| ZPP7 | `projects/{id}/` | `"zpp7"` in `path.name.casefold()` |
| MB25 | `projects/{id}/` | `"mb25"` in `path.name.casefold()` |
| ME2J | `projects/{id}/` | `"me2j"` in `path.stem.casefold()` |
| PR | `projects/{id}/` | stem logic (see Section 8.2) |
| MB52 | `global/` | `"in_mm_innr_mb52"` or `"mb52"` in name |
| Scope List | `global/` | `"material_scope_list"` in name |

### 15.2 Prerequisites for Processing

| Group | Required Reports |
|-------|----------------|
| Project (`projects/{id}/`) | ZPP7, MB25, ME2J, ME2M, PR |
| Global (`global/`) | MB52, Scope List |

All seven reports must be present and parseable, including ME2M before processing succeeds.

### 15.3 Project ID Rules

- Allowed characters: alphanumeric + hyphens
- Must start with alphanumeric
- Maximum 64 characters
- Regex: `^[A-Za-z0-9][A-Za-z0-9\-]{0,63}$`

---

*This document was generated by automated codebase analysis. Verify against source code after any parser or schema change.*

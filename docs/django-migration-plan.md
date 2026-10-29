# Django migration plan and feature map

## Audit snapshot

- Git branch: `main` tracking `origin/main`. The pre-existing user edit in `README.md` removes portable-package instructions and adds a stray `git` line; preserve the intent while rewriting the stale architecture documentation.
- Existing architecture: React 18 + TypeScript + Vite (`frontend/`) and FastAPI/Uvicorn (`backend/app/`), plus a second legacy Streamlit dashboard (`dashboard/app.py`). Python parsers and SQLite business services live in `parsers/` and `services/`.
- Operational state is present and must be retained: `data/global.db`, `data/zpp7_breakdown.db`, `data/services.db`, SAP inputs in `data/raw/`, `global/`, and `projects/`, and generated registers/vendor workbooks. `.gitignore` excludes the databases and most operational workbooks.
- Current application workflows include project discovery and report readiness, report staging and processing, shortage table filters/pagination/sorting/search/export, register export/open/delete, editable remarks, vendor tracking, vendor date request export, vendor response preview/apply, submitted-date archives, and report-specific parsers for MB25, MB52, ME2J, ME2M, PR, scope list, and ZPP7.
- Current backend is FastAPI, with project and vendor API routers and read-only shortage queries; no authentication or user accounts are present. FastAPI has no application-level authentication/authorization policy to preserve. Mutating actions will use Django CSRF-protected POST forms.
- Existing CI publishes a Windows portable release and builds the Vite frontend. Existing tests cover project structure, launcher behavior, and shortage workflows.

## Feature mapping and verification checklist

| Existing feature | Current implementation | Django replacement | Verification |
|---|---|---|---|
| Report parsing (ZPP7, MB25, MB52, ME2J, ME2M, PR, scope list) | `parsers/` and `main.py` | Reuse Python parser/service code, invoked by Django processing view | Parser and service regression tests using isolated fixtures |
| Project folders, report classification and readiness | `backend/app/projects.py`, `projects/`, `global/` | Django service/view and input page | Unit tests for safe IDs, classifications, readiness |
| Stage reports, process selected project | FastAPI project route shells files into `data/raw/`, invokes existing pipeline | CSRF protected Django POST using the same processing lock and pipeline | Django client tests with temp data paths; do not process real inputs during tests |
| Shortage calculations and preserved final dates | `services/shortage_service.py` | Reuse unchanged; SQLite remains authoritative | Regression tests for mapping, formula, negative clamp and retained dates |
| Shortage dashboard, filters, search, sorting, pagination | React frontend + FastAPI read-only query endpoints | Django template, GET forms, safe SQL parameters and pagination | Django client checks with fixture SQLite database |
| CSV/XLSX shortage export | FastAPI export route | Django download view using established columns and formats | Content type, headers, rows and active filters |
| Remarks editing | FastAPI app includes legacy route; React inline editor | CSRF protected Django POST | Update only matching project/material key in isolated DB |
| Material Shortage Register generation / management | Project API + `services/shortage_export.py` | Django POST actions and download link; remove machine-local Excel launch behavior | Export checks and path confinement |
| Vendor Track reports, filters and date request export | `backend/app/vendortrack.py`, `services/vendortrack_service.py`, `services/vendor_request_export.py` | Django views/forms reusing service functions | Existing vendor service tests plus fixture-backed view checks |
| Static ABB identity/design, responsive dashboard | `frontend/src/styles.css`, `frontend/public/ABB_logo.svg` | Django static assets and templates | Django checks, template rendering and static configuration checks |
| Authentication/authorization | No existing auth | No new user/account system without business policy; Django CSRF on mutations | POST without CSRF rejected by middleware test client |
| Runtime and dependencies | Vite, FastAPI, Uvicorn, portable launchers | Django development server on `127.0.0.1:18000`; configurable paths | Clean Python environment setup, system check, tests, local HTTP smoke check |
| CI | Portable release workflow only | Python-only CI: install, checks, migration check, tests | Workflow inspection and local equivalents |

## Incremental implementation order

1. Add this audit and mapping before modifying application files.
2. Add Django configuration, URLs, views, forms, templates, static CSS, and regression tests, reusing existing parser/business modules and SQLite databases.
3. Run Django checks and relevant regression tests with isolated temporary storage. Confirm data paths and existing databases are unchanged.
4. Replace Node/package manifests, portable packaging and launcher workflows only after the Django app renders and its core views are verified.
5. Update documentation/configuration and scan the full tracked tree for obsolete runtime/release references.

## Data-safety constraints

No database reset, deletion, recreation, migration, or conversion is planned. The application continues to use the existing SQLite files and legacy schema. Tests must point at isolated temporary databases and files. Keep the existing output tables and service-level rules; `Net Shortage = max(ZPP7 Req − project unrestricted stock, 0)` remains owned by the current shortage service.

# ABB Shortage and Vendor Management

A local Django application for processing SAP reports, reviewing project material shortages, maintaining vendor dates, and exporting Material Shortage Registers and vendor date requests. Django renders the frontend directly; Node.js and a frontend build are not needed.

## Features

- Discover projects and check required project and shared reports.
- Upload reports to the selected project or shared report folder without overwriting existing files.
- Run the existing parsers and shortage/vendor services.
- Review project shortage quantities, filters, search, sorting, pagination, CSV/XLSX exports, and editable remarks.
- Build and filter Vendor Track records and export vendor date requests.
- Manage vendor date registers: open vendor workbooks, save Proposed Dates, and apply accepted Vendor Dates to matching shortage registers.
- Keep operational records and reports in the existing local folders and SQLite databases.

## Prerequisites

- Python 3.11 or newer (Python 3.12 is supported).
- Git for source control.
- No Node.js, npm, React, TypeScript, Vite, Docker, or deployment setup.

## Set up on Windows (PowerShell)

From the repository root:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
Copy-Item .env.example .env
```

If your Python installation uses a different version, replace `3.12` with that installed version. PowerShell may require `Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass` to activate the virtual environment.

## Set up on macOS or Linux

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
cp .env.example .env
```

## Configuration

The optional `.env` file is read at startup. Keep machine-specific paths and credentials out of Git. Settings include:

| Variable | Default | Purpose |
|---|---|---|
| `DJANGO_SECRET_KEY` | Local development key | Set a private random value for a shared or non-development environment. |
| `DJANGO_DEBUG` | `1` | Django debug setting. |
| `ABB_DATA_ROOT` | `data/` | Operational data directory. |
| `ABB_RAW_DIR` | `<data root>/raw/` | Temporary report staging directory. |
| `ABB_GLOBAL_DB` | `<data root>/global.db` | Parsed shared report database. |
| `ABB_ZPP7_DB` | `<data root>/zpp7_breakdown.db` | Parsed ZPP7 database. |
| `ABB_SERVICES_DB` | `<data root>/services.db` | Existing shortage and vendor records. |
| `ABB_PROJECTS_DIR`, `ABB_GLOBAL_DIR` | `projects/`, `global/` | Project-specific and shared source report folders. |
| `SHORTAGE_REGISTERS_DIR` | `Material Shortage Registers/` | Generated project registers. |
| `VENDOR_REGISTERS_DIR` | `Vendor Registers/` | Generated vendor date request workbooks. |
| `DJANGO_METADATA_DB` | `data/django.sqlite3` | Django session/metadata database, separate from SAP application records. |

`manage.py migrate` creates or updates only Django's separate metadata database. It does not alter `services.db`, `global.db`, or `zpp7_breakdown.db`. Do not delete or recreate those existing application databases.

## Run locally

Run once after setup:

```powershell
python manage.py migrate
python manage.py check
python manage.py runserver 127.0.0.1:18000
```

Open [http://127.0.0.1:18000/](http://127.0.0.1:18000/) in your browser. The server binds to localhost only.

## Process SAP reports

1. Open **Input & Imports** and choose a project directory under `projects/<PROJECT_ID>/`.
2. Add ZPP7, MB25, ME2J, ME2M, and PR reports to that project folder. Use the upload form or copy the original files there yourself.
3. Add MB52 and `MATERIAL_SCOPE_LIST` to the shared `global/` folder.
4. Check the report readiness list and click **Process all reports & export register**.
5. Review the result on **Shortage Management** or under `Material Shortage Registers/<PROJECT_ID>/`.

Supported parser inputs include ZPP7, MB25, MB52, ME2J, ME2M (including `.xlsb`), PR, and Material Scope List. Their established parsing rules are in `parsers/`. Processing stages a temporary copy of inputs in `data/raw/`; pre-existing staged files are restored after the run. Source reports are kept in their project/shared folders.

Shortage calculations remain in `services/shortage_service.py`: ZPP7 `Req` is demand, Storage `Unrestricted` is project stock, ME2J `Order Quantity`/`Open Quantity` are PO/Open PO quantities, and PR is retained separately. Net Shortage remains `max(ZPP7 Req - Project Stock, 0)`.

## Tests and checks

```powershell
python manage.py check
python manage.py migrate --check
python manage.py test tests.test_django_app tests.test_project_structure
python -m unittest discover -s tests -p "test_shortage_workflow.py" -v
```

The tests use temporary databases and files. They do not run the production report pipeline against the repository's operational databases.

## Project structure

- `abbsite/` — Django settings and URL/WSGI/ASGI configuration.
- `shortage/` — Django views, report discovery, and SQLite metadata.
- `templates/shortage/`, `static/shortage/` — server-rendered pages and CSS.
- `parsers/` — existing SAP report parsers.
- `services/` — existing shortage, vendor, workbook, and import business logic.
- `data/` — local SQLite databases and raw/processed report storage.
- `projects/`, `global/` — project-specific and shared source reports.
- `Material Shortage Registers/`, `Vendor Registers/`, `Vendor Date Registers/` — generated shortage and vendor date request workbooks plus vendor response registers. Existing `Submitted Final Date/` archives are preserved.
- `tests/` — Django page/CSRF checks and Python service regression tests.

## Troubleshooting

- **Python not found:** install Python 3.11+ and recreate the virtual environment.
- **Missing reports:** compare the readiness list with required files in the selected project folder and `global/`.
- **No shortage rows:** check that ZPP7 Material Ref Codes map to WBS records and Scope List contains supported `ABB` or `VENDOR SCOPE` values.
- **SQLite locked:** close other local application instances and retry after they finish writing.
- **Workbook parse errors:** verify the SAP export format and that the report file is complete and not open for editing.
- **Vendor response not matched:** use Preview to check project, vendor, WBS, and material matching before applying dates.

## Legacy cleanup and scope

The Django application replaces the React/TypeScript/Vite frontend, FastAPI route layer, Streamlit dashboard, and Windows portable/laptop-server launch workflows. The SAP parsers, business services, SQLite application data, and existing report folders remain in use. Docker, public hosting, and deployment are intentionally not implemented in this task.

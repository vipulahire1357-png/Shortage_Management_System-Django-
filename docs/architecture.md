# Architecture

1. Django views render templates and accept CSRF-protected forms on localhost.
2. Project discovery checks safe project IDs and classifies SAP source files.
3. Existing parsers normalize ZPP7, MB25, MB52, ME2J, ME2M, PR, and Material Scope List reports.
4. Existing services calculate shortages, maintain Vendor Track data, import vendor dates, and generate Excel workbooks.
5. The original SQLite databases remain the source of application records. Django session metadata uses a separate database at `data/django.sqlite3` by default.
6. Source reports are kept in project/shared folders. Processing temporarily stages copies and restores any pre-existing staged reports after the pipeline finishes.

Business calculations stay in `services/shortage_service.py`. Parser-specific rules remain in their parser modules. Tests use isolated fixture databases and do not run the pipeline against operational data.

import os
import shutil
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
import openpyxl
import pandas as pd

from shortage.data import DB_COLUMNS, API_COLUMNS
from services.shortage_export import build_material_shortage_workbook
from services.vendortrack_service import (
    validate_material_shortage_register,
    read_register_to_vendor_track_rows,
    refresh_vendor_track_from_registers,
    REQUIRED_REGISTER_FIELDS,
)
from services.vendor_request_export import (
    calculate_batch_remark,
    sanitize_vendor_folder_name,
    build_vendor_request_workbook,
    export_vendor_date_request,
    VENDOR_REQUEST_COLUMNS,
)


class TestMaterialShortageColumns(unittest.TestCase):
    def test_vendor_and_final_date_position_in_columns(self):
        """Vendor Detail is followed by Final Date, then Remarks."""
        free_idx = DB_COLUMNS.index("Free Stock")
        vendor_idx = DB_COLUMNS.index("Vendor Detail")
        final_date_idx = DB_COLUMNS.index("Final Date")
        remarks_idx = DB_COLUMNS.index("Remarks")
        self.assertEqual(vendor_idx, free_idx + 1)
        self.assertEqual(final_date_idx, vendor_idx + 1)
        self.assertEqual(remarks_idx, final_date_idx + 1)

        api_free_idx = API_COLUMNS.index("free_stock")
        api_vendor_idx = API_COLUMNS.index("vendor_detail")
        api_final_date_idx = API_COLUMNS.index("final_date")
        api_remarks_idx = API_COLUMNS.index("remarks")
        self.assertEqual(api_vendor_idx, api_free_idx + 1)
        self.assertEqual(api_final_date_idx, api_vendor_idx + 1)
        self.assertEqual(api_remarks_idx, api_final_date_idx + 1)


class TestShortageExport(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_material_shortage_workbook_generation(self):
        """2 & 3. Material Shortage Register export includes Vendor Detail, has proper formatting and columns."""
        rows = [
            {
                "Scope": "ELECTRICAL",
                "Material Ref Code": "REF001",
                "WBS Element": "IN-TEST-01",
                "Material Code": "0000012345",
                "Material Description": "Contactor 24V",
                "ZPP7 Quantity": 10.0,
                "Project Stock Quantity": 2.0,
                "PO Quantity": 5.0,
                "Open PO Quantity": 2.0,
                "PR Quantity": 1.0,
                "Gross Shortage": 5.0,
                "Plant Stock": 2.0,
                "Free Stock": 1.0,
                "Vendor Detail": "1100001469 DEVSHREE ENTERPRISES",
                "Remarks": "Urgent",
                "Net Shortage": 4.0,
            }
        ]

        output_path = Path(self.test_dir) / "TEST_PROJ.xlsx"
        wb = build_material_shortage_workbook(rows, table_name_suffix="TEST_PROJ")
        wb.save(output_path)

        self.assertTrue(output_path.exists())
        loaded_wb = openpyxl.load_workbook(output_path)
        self.assertEqual(loaded_wb.active.title, "Material Shortage Register")

        ws = loaded_wb.active
        headers = [cell.value for cell in ws[1]]
        self.assertIn("Vendor Detail", headers)
        free_idx = headers.index("Free Stock")
        vend_idx = headers.index("Vendor Detail")
        final_date_idx = headers.index("Final Date")
        rem_idx = headers.index("Remarks")
        self.assertEqual(vend_idx, free_idx + 1)
        self.assertEqual(final_date_idx, vend_idx + 1)
        self.assertEqual(rem_idx, final_date_idx + 1)

        # Material code preserved as string
        mat_cell = ws.cell(row=2, column=headers.index("Material Code") + 1)
        self.assertEqual(str(mat_cell.value), "0000012345")


class TestVendorTrackIngestion(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_validation_detects_invalid_file(self):
        """Validate register files and detect invalid structure."""
        invalid_path = Path(self.test_dir) / "invalid.xlsx"
        df = pd.DataFrame([{"Random Col": 123}])
        df.to_excel(invalid_path, index=False)

        with self.assertRaises(ValueError) as ctx:
            validate_material_shortage_register(invalid_path)
        self.assertIn("missing required column", str(ctx.exception).lower())

    def test_read_register_to_vendor_track_rows(self):
        """5 & 8. Keep Vendor Detail intact as one vendor field per register row."""
        valid_path = Path(self.test_dir) / "PROJ_A.xlsx"
        df = pd.DataFrame([
            {
                "WBS Element": "WBS-001",
                "Material Code": "MAT-001",
                "Material Description": "Relay",
                "Net Shortage": 12.0,
                "Vendor Detail": "1100001469 DEVSHREE ENTERPRISES / 1100009999 SIEMENS",
            }
        ])
        df.to_excel(valid_path, index=False)

        resolved = validate_material_shortage_register(valid_path)
        self.assertIsInstance(resolved, dict)

        rows = read_register_to_vendor_track_rows(valid_path, "PROJ_A")
        # Slash may be part of a vendor's registered name; do not split it.
        self.assertEqual(len(rows), 1)
        v1 = rows[0]
        self.assertEqual(v1[0], "PROJ_A")
        self.assertEqual(v1[1], "1100001469 DEVSHREE ENTERPRISES / 1100009999 SIEMENS")
        self.assertEqual(v1[2], "WBS-001")
        self.assertEqual(v1[3], "MAT-001")
        self.assertEqual(v1[4], "Relay")
        self.assertEqual(v1[5], 12.0)
        self.assertEqual(v1[6], "")
        self.assertEqual(v1[7], "")
        self.assertEqual(v1[8], "")

    def test_refresh_multi_projects_and_preserve_dates(self):
        """4, 5, 6, 7. Multiple projects combined, unselected excluded, no duplicates on re-run, dates preserved."""
        db_path = Path(self.test_dir) / "services_test.db"

        # Register for PROJ-1
        p1_path = Path(self.test_dir) / "PROJ-1.xlsx"
        df1 = pd.DataFrame([
            {
                "WBS Element": "WBS-P1",
                "Material Code": "MAT-001",
                "Material Description": "Relay",
                "Net Shortage": 5.0,
                "Vendor Detail": "1100001469 DEVSHREE ENTERPRISES",
            }
        ])
        df1.to_excel(p1_path, index=False)

        # Register for PROJ-2
        p2_path = Path(self.test_dir) / "PROJ-2.xlsx"
        df2 = pd.DataFrame([
            {
                "WBS Element": "WBS-P2",
                "Material Code": "MAT-002",
                "Material Description": "Contactor",
                "Net Shortage": 8.0,
                "Vendor Detail": "1100009999 SIEMENS",
            }
        ])
        df2.to_excel(p2_path, index=False)

        # 1. Process both projects
        res = refresh_vendor_track_from_registers(
            ["PROJ-1", "PROJ-2"], registers_dir=Path(self.test_dir), db_path=db_path
        )
        self.assertEqual(res["total_rows"], 2)
        self.assertEqual(len(res["projects"]), 2)

        # Verify DB has 2 rows (one for each project)
        conn = sqlite3.connect(db_path)
        cur = conn.cursor()
        rows = cur.execute('SELECT "project_id", "Material Code", "Vendor Date" FROM vendor_track ORDER BY "project_id"').fetchall()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0][0], "PROJ-1")
        self.assertEqual(rows[1][0], "PROJ-2")

        # 2. Update a date in PROJ-1
        conn.execute('UPDATE vendor_track SET "Vendor Date" = "2026-12-01" WHERE "project_id" = "PROJ-1"')
        conn.commit()

        # 3. Reprocess only PROJ-1: must NOT duplicate rows, PROJ-2 must remain intact, and date must be preserved
        refresh_vendor_track_from_registers(
            ["PROJ-1"], registers_dir=Path(self.test_dir), db_path=db_path
        )

        rows_after = cur.execute('SELECT "project_id", "Material Code", "Vendor Date" FROM vendor_track ORDER BY "project_id"').fetchall()
        self.assertEqual(len(rows_after), 2)  # Still 2 rows (no duplicates!)
        self.assertEqual(rows_after[0][0], "PROJ-1")
        self.assertEqual(rows_after[0][2], "2026-12-01")  # Date preserved!
        self.assertEqual(rows_after[1][0], "PROJ-2")  # PROJ-2 untouched!
        conn.close()


class TestFirstSecondRemarkRule(unittest.TestCase):
    def test_first_remark_when_all_dates_empty(self):
        """12. First is assigned when all date values are empty across all rows."""
        rows = [
            {"Vendor Date": "", "Proposed Date": None, "Final Date": "   "},
            {"Vendor Date": None, "Proposed Date": "", "Final Date": ""},
        ]
        self.assertEqual(calculate_batch_remark(rows), "First")

    def test_second_remark_when_vendor_date_populated(self):
        """13. Second is assigned when any date is populated in any row."""
        rows = [
            {"Vendor Date": "2026-11-01", "Proposed Date": "", "Final Date": ""},
            {"Vendor Date": "", "Proposed Date": "", "Final Date": ""},
        ]
        self.assertEqual(calculate_batch_remark(rows), "Second")

    def test_second_remark_when_proposed_or_final_date_populated(self):
        rows1 = [{"Vendor Date": "", "Proposed Date": "2026-11-05", "Final Date": ""}]
        self.assertEqual(calculate_batch_remark(rows1), "Second")

        rows2 = [{"Vendor Date": "", "Proposed Date": "", "Final Date": "2026-11-10"}]
        self.assertEqual(calculate_batch_remark(rows2), "Second")


class TestVendorDateRequestExport(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.db_path = Path(self.test_dir) / "test_services.db"
        # Create a test sqlite db with vendor_track table
        conn = sqlite3.connect(self.db_path)
        with conn:
            conn.execute("""
                CREATE TABLE vendor_track (
                    "project_id" TEXT,
                    "Vendor Detail" TEXT,
                    "WBS Element" TEXT,
                    "Material Code" TEXT,
                    "Description" TEXT,
                    "Quantity" REAL,
                    "Vendor Date" TEXT,
                    "Proposed Date" TEXT,
                    "Final Date" TEXT,
                    "Status" TEXT
                )
            """)
        conn.close()

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_sanitize_vendor_folder_name(self):
        self.assertEqual(
            sanitize_vendor_folder_name("1100001469 DEVSHREE / ENTERPRISES: *?"),
            "1100001469 DEVSHREE ENTERPRISES"
        )

    def test_export_rejects_all_vendors(self):
        """14. Export rejects All Vendors."""
        with self.assertRaises(ValueError) as ctx:
            export_vendor_date_request(
                vendor="All Vendors",
                db_path=self.db_path,
                output_root=self.test_dir,
            )
        self.assertIn("select a specific vendor", str(ctx.exception))

    def test_export_rejects_empty_results(self):
        """14. Export rejects empty results."""
        with self.assertRaises(ValueError) as ctx:
            export_vendor_date_request(
                vendor="1100001469 DEVSHREE ENTERPRISES",
                db_path=self.db_path,
                output_root=self.test_dir,
            )
        self.assertIn("No records found", str(ctx.exception))

    def test_export_creates_folder_and_file_with_versioning(self):
        """9, 15, 16. Exports to Vendor Registers/<vendor>/Outgoing/Vendor_Date_Request/{First,Second}.xlsx with versioning."""
        # Insert records with empty dates
        conn = sqlite3.connect(self.db_path)
        with conn:
            conn.execute(
                """
                INSERT INTO vendor_track
                ("project_id", "Vendor Detail", "WBS Element", "Material Code", "Description", "Quantity", "Vendor Date", "Proposed Date", "Final Date")
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                ("PROJ-1", "1100001469 DEVSHREE ENTERPRISES", "IN-TEST-01", "000123", "Relay", 10.0, "", "", ""),
            )
        conn.close()

        # First export -> First.xlsx
        res1 = export_vendor_date_request(
            vendor="1100001469 DEVSHREE ENTERPRISES",
            db_path=self.db_path,
            output_root=self.test_dir,
        )
        self.assertEqual(res1["remark"], "First")
        self.assertEqual(res1["filename"], "First.xlsx")
        self.assertTrue(Path(res1["output_path"]).exists())
        self.assertIn("Date Request", res1["output_path"])

        # Second export of First condition should create First_v2.xlsx
        res2 = export_vendor_date_request(
            vendor="1100001469 DEVSHREE ENTERPRISES",
            db_path=self.db_path,
            output_root=self.test_dir,
        )
        self.assertEqual(res2["remark"], "First")
        self.assertEqual(res2["filename"], "First_v2.xlsx")
        self.assertTrue(Path(res2["output_path"]).exists())

        # Now update row with a date -> Second.xlsx
        conn = sqlite3.connect(self.db_path)
        with conn:
            conn.execute(
                """
                UPDATE vendor_track
                SET "Vendor Date" = '2026-11-20'
                WHERE "Vendor Detail" = '1100001469 DEVSHREE ENTERPRISES'
                """
            )
        conn.close()

        res3 = export_vendor_date_request(
            vendor="1100001469 DEVSHREE ENTERPRISES",
            db_path=self.db_path,
            output_root=self.test_dir,
        )
        self.assertEqual(res3["remark"], "Second")
        self.assertEqual(res3["filename"], "Second.xlsx")
        self.assertTrue(Path(res3["output_path"]).exists())

        # Verify Excel content has 8 exact columns in order
        wb = openpyxl.load_workbook(res3["output_path"])
        ws = wb.active
        self.assertEqual(ws.title, "Vendor Date Request")
        headers = [cell.value for cell in ws[1]]
        self.assertEqual(headers, VENDOR_REQUEST_COLUMNS)

        # Verify Material Code preserved as text
        mat_cell = ws.cell(row=2, column=headers.index("Material Code") + 1)
        self.assertEqual(str(mat_cell.value), "000123")


class TestME2MReportDiscovery(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_reports_definition_has_me2m(self):
        """REPORTS in database.py contains ME2M with label, stem, and .xlsb support."""
        from shortage.data import REPORTS
        self.assertIn("me2m", REPORTS)
        me2m_meta = REPORTS["me2m"]
        self.assertEqual(me2m_meta["stem"], "ME2M")
        self.assertIn(".xlsb", me2m_meta["extensions"])
        self.assertIn("Vendor / Material Details", me2m_meta["label"])

    def test_me2m_classifier_and_discovery(self):
        """_is_me2m classifies ME2M correctly and does not match other reports."""
        from shortage.projects import is_me2m as _is_me2m, classify_files as _classify_files, PROJECT_REPORT_CLASSIFIERS

        # Matches valid ME2M files (including .xlsb)
        self.assertTrue(_is_me2m(Path("ME2M  aLL  DATA_ (version 1).xlsb")))
        self.assertTrue(_is_me2m(Path("me2m_IN-10026019.XLSX")))
        self.assertTrue(_is_me2m(Path("ME2M.xls")))

        # Does NOT match other reports
        self.assertFalse(_is_me2m(Path("me2j_IN-10026019.XLSX")))
        self.assertFalse(_is_me2m(Path("mb25_IN-10026019.XLSX")))
        self.assertFalse(_is_me2m(Path("zpp7_IN-10026019.xls")))
        self.assertFalse(_is_me2m(Path("pr_IN-10026019.XLSX")))

        # Folder classification
        folder = Path(self.test_dir)
        (folder / "ME2M  aLL  DATA_ (version 1).xlsb").touch()
        (folder / "zpp7_test.xls").touch()
        (folder / "mb25_test.xlsx").touch()
        (folder / "me2j_test.xlsx").touch()
        (folder / "pr_test.xlsx").touch()

        matches = _classify_files(folder, PROJECT_REPORT_CLASSIFIERS)
        self.assertIsNotNone(matches.get("me2m"))
        self.assertEqual(matches["me2m"].name, "ME2M  aLL  DATA_ (version 1).xlsb")

    def test_missing_me2m_returns_none(self):
        """Folder without ME2M classifies me2m as None."""
        from shortage.projects import classify_files as _classify_files, PROJECT_REPORT_CLASSIFIERS

        folder = Path(self.test_dir)
        (folder / "zpp7_test.xls").touch()

        matches = _classify_files(folder, PROJECT_REPORT_CLASSIFIERS)
        self.assertIsNone(matches.get("me2m"))
class TestProjectAwarePersistence(unittest.TestCase):
    """Phase 1: project-aware write_output preserves Final Dates and isolates projects."""

    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.db_path = Path(self.test_dir) / "test_services.db"

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_write_output_tags_project_id(self):
        """write_output sets project_id on all inserted rows."""
        from services.shortage_service import write_output
        rows = [
            ("MAT001", "Relay", 10.0, 2.0, 5.0, 2.0, 1.0, 5.0, 1.0, "VENDOR_A", "", "REF01", "WBS01", "ELEC"),
        ]
        write_output(rows, project_id="PROJ-TEST", db_path=self.db_path)

        conn = sqlite3.connect(self.db_path)
        result = conn.execute('SELECT "project_id" FROM net_shortage').fetchall()
        conn.close()
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0][0], "PROJ-TEST")


    def test_write_output_preserves_final_dates(self):
        """Reprocessing preserves existing Final Dates for matching records."""
        from services.shortage_service import write_output
        rows = [
            ("MAT001", "Relay", 10.0, 2.0, 5.0, 2.0, 1.0, 5.0, 1.0, "VENDOR_A", "", "REF01", "WBS01", "ELEC"),
        ]
        write_output(rows, project_id="PROJ-A", db_path=self.db_path)

        # Set a Final Date manually
        conn = sqlite3.connect(self.db_path)
        conn.execute('UPDATE net_shortage SET "Final Date" = "2026-12-01" WHERE "Material Code" = "MAT001"')
        conn.commit()
        conn.close()

        # Reprocess same project
        write_output(rows, project_id="PROJ-A", db_path=self.db_path)

        conn = sqlite3.connect(self.db_path)
        result = conn.execute('SELECT "Final Date" FROM net_shortage WHERE "Material Code" = "MAT001"').fetchone()
        conn.close()
        self.assertEqual(result[0], "2026-12-01")

    def test_write_output_isolates_projects(self):
        """Reprocessing project A does not affect project B's rows."""
        from services.shortage_service import write_output
        rows_a = [
            ("MAT001", "Relay", 10.0, 2.0, 5.0, 2.0, 1.0, 5.0, 1.0, "VENDOR_A", "", "REF01", "WBS01", "ELEC"),
        ]
        rows_b = [
            ("MAT002", "Contactor", 20.0, 3.0, 8.0, 4.0, 2.0, 10.0, 2.0, "VENDOR_B", "", "REF02", "WBS02", "MECH"),
        ]
        write_output(rows_a, project_id="PROJ-A", db_path=self.db_path)
        write_output(rows_b, project_id="PROJ-B", db_path=self.db_path)

        # Reprocess PROJ-A with modified data
        rows_a_v2 = [
            ("MAT001", "Relay V2", 15.0, 2.0, 5.0, 2.0, 1.0, 8.0, 1.0, "VENDOR_A", "", "REF01", "WBS01", "ELEC"),
        ]
        write_output(rows_a_v2, project_id="PROJ-A", db_path=self.db_path)

        conn = sqlite3.connect(self.db_path)
        all_rows = conn.execute('SELECT "project_id", "Material Code", "Material Description" FROM net_shortage ORDER BY "project_id"').fetchall()
        conn.close()

        self.assertEqual(len(all_rows), 2)
        self.assertEqual(all_rows[0][0], "PROJ-A")
        self.assertEqual(all_rows[0][2], "Relay V2")  # Updated
        self.assertEqual(all_rows[1][0], "PROJ-B")
        self.assertEqual(all_rows[1][1], "MAT002")  # Untouched


class TestShortageCalculationMapping(unittest.TestCase):
    def test_report_quantity_mappings_and_negative_shortage_clamp(self):
        from services.shortage_service import BUILD_COLUMNS, build_rows

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            global_db, zpp_db = root / "global.db", root / "zpp7.db"
            with closing(sqlite3.connect(global_db)) as conn:
                conn.executescript('''
                    CREATE TABLE wbs_element ("Material Ref Code" TEXT, "WBS Element" TEXT);
                    CREATE TABLE scopelist ("Material Code" TEXT, "Scope" TEXT);
                    CREATE TABLE Storage ("Material Code" TEXT, "Unrestricted" REAL, "WBS Element" TEXT);
                    CREATE TABLE ME2J ("Material Code" TEXT, "Order Quantity" REAL, "Open Quantity" REAL, "WBS Element" TEXT);
                    CREATE TABLE ME2M ("Material Code" TEXT, "Vendor Detail" TEXT);
                    CREATE TABLE PR ("Material Code" TEXT, "PR Quantity" REAL, "WBS Element" TEXT);
                    INSERT INTO wbs_element VALUES ('REF1','WBS1');
                    INSERT INTO scopelist VALUES ('MAT-SHORT','ABB'),('MAT-OVER','VENDOR SCOPE');
                    INSERT INTO Storage VALUES ('MAT-SHORT',5,'WBS1'),('MAT-OVER',10,'WBS1');
                    INSERT INTO ME2J VALUES ('MAT-SHORT',7,3,'WBS1');
                    INSERT INTO ME2M VALUES ('MAT-SHORT','Vendor One');
                    INSERT INTO PR VALUES ('MAT-SHORT',4,'WBS1');
                ''')
                conn.commit()
            with closing(sqlite3.connect(zpp_db)) as conn:
                conn.execute('CREATE TABLE requirements ("Material Ref Code" TEXT, "Material Code" TEXT, "Material Description" TEXT, "Req" REAL)')
                conn.executemany('INSERT INTO requirements VALUES (?,?,?,?)', [
                    ('REF1','MAT-SHORT','Short item',20),
                    ('REF1','MAT-OVER','Overstocked item',5),
                ])
                conn.commit()
            rows = build_rows(global_db, zpp_db)
            by_material = {row[0]: dict(zip(BUILD_COLUMNS, row)) for row in rows}
            short = by_material["MAT-SHORT"]
            self.assertEqual(short["ZPP7 Quantity"], 20)
            self.assertEqual(short["Project Stock Quantity"], 5)
            self.assertEqual(short["PO Quantity"], 7)
            self.assertEqual(short["Open PO Quantity"], 3)
            self.assertEqual(short["PR Quantity"], 4)
            self.assertEqual(short["Net Shortage"], 15)
            self.assertEqual(short["Vendor Detail"], "Vendor One")
            self.assertEqual(by_material["MAT-OVER"]["Net Shortage"], 0)


class TestShortageMaterialVendorResolution(unittest.TestCase):
    """Vendor Detail uses ME2M values keyed only by Material Code."""

    def test_unique_me2m_vendor_is_used(self):
        from services.shortage_service import resolve_material_vendor

        vendor, conflict = resolve_material_vendor(
            ["1100001469 DEVSHREE ENTERPRISES"]
        )

        self.assertEqual(vendor, "1100001469 DEVSHREE ENTERPRISES")
        self.assertFalse(conflict)

    def test_same_material_uses_first_distinct_me2m_value_in_report_order(self):
        from services.shortage_service import resolve_material_vendor

        vendor, conflict = resolve_material_vendor(
            [
                "701501026  ABB India Limited",
                "701501026 ABB India Limited",
                "INHN EPDS-LV Systems, Bangalore",
            ]
        )

        self.assertEqual(vendor, "701501026 ABB India Limited")
        self.assertTrue(conflict)

    def test_multiple_me2m_values_still_produce_one_vendor_detail(self):
        from services.shortage_service import resolve_material_vendor

        vendor, conflict = resolve_material_vendor(
            ["1100001469 DEVSHREE ENTERPRISES", "1100009999 SIEMENS"]
        )

        self.assertEqual(vendor, "1100001469 DEVSHREE ENTERPRISES")
        self.assertTrue(conflict)

    def test_me2m_supplying_plant_value_is_used_when_it_is_first_match(self):
        from services.shortage_service import resolve_material_vendor

        self.assertEqual(
            resolve_material_vendor(["INHN EPDS-LV Systems, Bangalore"]),
            ("INHN EPDS-LV Systems, Bangalore", False),
        )

    def test_missing_me2m_vendor_remains_empty_without_conflict(self):
        from services.shortage_service import resolve_material_vendor

        self.assertEqual(resolve_material_vendor([]), (None, False))


class TestNestedRegisterExport(unittest.TestCase):
    """Phase 1: register export uses nested folder path."""

    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.db_path = Path(self.test_dir) / "test_services.db"
        # Create a test db with net_shortage
        conn = sqlite3.connect(self.db_path)
        conn.execute("""
            CREATE TABLE net_shortage (
                "project_id" TEXT,
                "Material Ref Code" TEXT, "WBS Element" TEXT,
                "Material Code" TEXT, "Material Description" TEXT,
                "ZPP7 Quantity" REAL, "Project Stock Quantity" REAL,
                "PO Quantity" REAL, "Open PO Quantity" REAL,
                "PR Quantity" REAL, "Net Shortage" REAL, "Free Stock" REAL,
                "Vendor Detail" TEXT, "Remarks" TEXT, "Scope" TEXT, "Final Date" TEXT
            )
        """)
        conn.execute("""
            INSERT INTO net_shortage VALUES (
                'PROJ-TEST', 'REF01', 'WBS01', 'MAT001', 'Relay',
                10.0, 2.0, 5.0, 2.0, 1.0, 5.0, 1.0,
                'VENDOR_A', '', 'ELECTRICAL', '2026-12-01'
            )
        """)
        conn.commit()
        conn.close()

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_export_creates_nested_path(self):
        """Register export creates <project_id>/<project_id>_Net_shortage_Table.xlsx."""
        from services.shortage_export import export_material_shortage_register
        output_dir = Path(self.test_dir) / "registers"
        result = export_material_shortage_register(
            "PROJ-TEST", db_path=self.db_path, output_dir=output_dir
        )
        expected_file = output_dir / "PROJ-TEST" / "PROJ-TEST_Net_shortage_Table.xlsx"
        self.assertTrue(expected_file.exists())
        self.assertEqual(result["filename"], "PROJ-TEST_Net_shortage_Table.xlsx")
        self.assertEqual(result["rows"], 1)

    def test_export_includes_final_date_column(self):
        """Exported register workbook includes Final Date header."""
        from services.shortage_export import export_material_shortage_register
        output_dir = Path(self.test_dir) / "registers"
        result = export_material_shortage_register(
            "PROJ-TEST", db_path=self.db_path, output_dir=output_dir
        )
        wb = openpyxl.load_workbook(result["path"])
        headers = [cell.value for cell in wb.active[1]]
        self.assertIn("Final Date", headers)
        wb.close()


class TestFinalDateInColumns(unittest.TestCase):
    """Phase 1: Final Date appears in DB_COLUMNS and API_COLUMNS."""

    def test_final_date_in_db_columns(self):
        self.assertIn("Final Date", DB_COLUMNS)

    def test_final_date_in_api_columns(self):
        self.assertIn("final_date", API_COLUMNS)

    def test_columns_same_length(self):
        self.assertEqual(len(DB_COLUMNS), len(API_COLUMNS))


if __name__ == "__main__":
    unittest.main()

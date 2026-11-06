"""Comprehensive tests for services/net_shortage.py — the Material Shortage Register engine.

Tests cover:
  1.  Normal ZPP7+MB25 matching
  2.  ZPP7 without MB25 match (unmatched ref codes)
  3.  Multiple WBS mappings for one material ref
  4.  MB52 project stock match (Material Code + WBS)
  5.  MB52 free stock fallback (Material Code only, no WBS)
  6.  Missing stock record
  7.  Multiple ME2J PO lines aggregation
  8.  Multiple PR lines aggregation
  9.  Missing ScopeList match
  10. ME2M single vendor
  11. ME2M multi-vendor WBS-resolved
  12. ME2M multi-vendor unresolved
  13. Missing delivery date
  14. Net Shortage formula (negative clamped to 0)
  15. Final Date preservation across reprocessing
  16. Remarks preservation (user vs system remarks)
  17. Project isolation (processing one project doesn't affect another)
  18. Idempotent reprocessing
  19. No duplicate records
  20. Column contract (all expected columns present)
  21. End-to-end orchestration
  22. Schema migration
  23. VendorResolution class
  24. Edge cases (empty tables, zero quantities, null values)
"""
from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

# Ensure project root is on the path
import sys
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from services.net_shortage import (
    ALL_DB_COLUMNS,
    FULL_DB_COLUMNS,
    IDENTITY_COLUMNS,
    OUTPUT_COLUMNS_ORDERED,
    VendorResolution,
    _clean,
    _to_float,
    _resolve_vendor,
    build_material_shortage,
    calculate_net_shortage,
    enrich_with_mb52,
    enrich_with_me2j,
    enrich_with_me2m,
    enrich_with_pr,
    enrich_with_scope,
    generate_material_shortage_register,
    preserve_existing_fields,
    save_net_shortage,
    validate_material_shortage,
)


# -----------------------------------------------------------------------
# Helpers to build in-memory test databases with CORRECT schemas
# -----------------------------------------------------------------------
def _create_zpp7_db(path: Path, table_name: str, rows: list[tuple]) -> None:
    """Create a ZPP7 breakdown database with a single table."""
    conn = sqlite3.connect(path)
    conn.execute(
        f'CREATE TABLE "{table_name}" ('
        f'"Material Ref Code" TEXT, '
        f'"Material Code" TEXT, '
        f'"Description" TEXT, '
        f'"ZPP7 Quantity" REAL, '
        f'PRIMARY KEY ("Material Ref Code", "Material Code"))'
    )
    conn.executemany(
        f'INSERT INTO "{table_name}" VALUES (?, ?, ?, ?)', rows
    )
    conn.commit()
    conn.close()


def _create_global_db(
    path: Path,
    *,
    mb25_rows: list[tuple] | None = None,
    mb52_rows: list[tuple] | None = None,
    me2j_rows: list[tuple] | None = None,
    pr_rows: list[tuple] | None = None,
    scope_rows: list[tuple] | None = None,
    me2m_rows: list[tuple] | None = None,
) -> None:
    """Create a global.db with all required report tables using correct schemas."""
    conn = sqlite3.connect(path)

    # MB25
    conn.execute(
        'CREATE TABLE "MB25" ('
        '"Material Ref Code" TEXT, '
        '"WBS Element" TEXT)'
    )
    if mb25_rows:
        conn.executemany('INSERT INTO "MB25" VALUES (?, ?)', mb25_rows)

    # MB52 — NOTE: Project Stock Quantity is TEXT in real DB
    conn.execute(
        'CREATE TABLE "MB52" ('
        '"Material Code" TEXT, '
        '"Description" TEXT, '
        '"Project Stock Quantity" TEXT, '
        '"WBS Element" TEXT)'
    )
    if mb52_rows:
        conn.executemany('INSERT INTO "MB52" VALUES (?, ?, ?, ?)', mb52_rows)

    # ME2J — NOTE: quantities are TEXT in real DB
    conn.execute(
        'CREATE TABLE "ME2J" ('
        '"WBS Element" TEXT, '
        '"Material Code" TEXT, '
        '"Description" TEXT, '
        '"Total PO Quantity" TEXT, '
        '"Open PO Quantity" TEXT)'
    )
    if me2j_rows:
        conn.executemany('INSERT INTO "ME2J" VALUES (?, ?, ?, ?, ?)', me2j_rows)

    # PR — NOTE: PR Quantity is TEXT in real DB
    conn.execute(
        'CREATE TABLE "PR" ('
        '"WBS Element" TEXT, '
        '"Material Code" TEXT, '
        '"Description" TEXT, '
        '"PR Quantity" TEXT)'
    )
    if pr_rows:
        conn.executemany('INSERT INTO "PR" VALUES (?, ?, ?, ?)', pr_rows)

    # ScopeList
    conn.execute(
        'CREATE TABLE "ScopeList" ('
        '"Material Code" TEXT, '
        '"Description" TEXT, '
        '"Final Scope" TEXT)'
    )
    if scope_rows:
        conn.executemany('INSERT INTO "ScopeList" VALUES (?, ?, ?)', scope_rows)

    # ME2M
    conn.execute(
        'CREATE TABLE "ME2M" ('
        '"Material Code" TEXT, '
        '"Description" TEXT, '
        '"Vendor Detail" TEXT, '
        '"WBS Element" TEXT, '
        '"Proposed Date" TEXT)'
    )
    if me2m_rows:
        conn.executemany('INSERT INTO "ME2M" VALUES (?, ?, ?, ?, ?)', me2m_rows)

    conn.commit()
    conn.close()


class NetShortageTestBase(unittest.TestCase):
    """Base class providing temp directory and standard test databases."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ns_test_")
        self.zpp7_db = Path(self.tmp) / "zpp7_breakdown.db"
        self.global_db = Path(self.tmp) / "global.db"
        self.services_db = Path(self.tmp) / "services.db"

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _build_standard_dbs(
        self,
        *,
        zpp7_table="1SYN_TEST_A",
        zpp7_rows=None,
        mb25_rows=None,
        mb52_rows=None,
        me2j_rows=None,
        pr_rows=None,
        scope_rows=None,
        me2m_rows=None,
    ):
        """Build standard test databases with defaults for all tables."""
        if zpp7_rows is None:
            zpp7_rows = [
                ("1SYN_TEST_A", "MAT001", "Bolt M10", 100.0),
                ("1SYN_TEST_A", "MAT002", "Nut M10", 50.0),
                ("1SYN_TEST_A", "MAT003", "Washer M10", 200.0),
            ]
        if mb25_rows is None:
            mb25_rows = [
                ("1SYN_TEST_A", "WBS.001"),
            ]
        if mb52_rows is None:
            mb52_rows = []
        if me2j_rows is None:
            me2j_rows = []
        if pr_rows is None:
            pr_rows = []
        if scope_rows is None:
            scope_rows = []
        if me2m_rows is None:
            me2m_rows = []

        _create_zpp7_db(self.zpp7_db, zpp7_table, zpp7_rows)
        _create_global_db(
            self.global_db,
            mb25_rows=mb25_rows,
            mb52_rows=mb52_rows,
            me2j_rows=me2j_rows,
            pr_rows=pr_rows,
            scope_rows=scope_rows,
            me2m_rows=me2m_rows,
        )


# =======================================================================
# Test 1: Normal ZPP7 + MB25 matching
# =======================================================================
class TestBuildMaterialShortage(NetShortageTestBase):
    """Tests for Step 1 — ZPP7 + MB25 join."""

    def test_normal_match(self):
        """ZPP7 records matched to MB25 should have WBS Element populated."""
        self._build_standard_dbs()
        with sqlite3.connect(self.global_db) as conn:
            result = build_material_shortage(self.zpp7_db, conn)

        # All 3 materials × 1 WBS = 3 records
        self.assertEqual(len(result), 3)
        for rec in result:
            self.assertEqual(rec["WBS Element"], "WBS.001")
            self.assertTrue(rec["_mb25_matched"])
            self.assertIn(rec["Material Code"], {"MAT001", "MAT002", "MAT003"})

    def test_unmatched_ref_codes(self):
        """ZPP7 records with no MB25 match should be retained with empty WBS."""
        self._build_standard_dbs(
            mb25_rows=[],  # No MB25 mappings at all
        )
        with sqlite3.connect(self.global_db) as conn:
            result = build_material_shortage(self.zpp7_db, conn)

        self.assertEqual(len(result), 3)
        for rec in result:
            self.assertEqual(rec["WBS Element"], "")
            self.assertFalse(rec["_mb25_matched"])

    def test_multiple_wbs_mappings(self):
        """One ref code mapping to multiple WBS should create one record per WBS."""
        self._build_standard_dbs(
            mb25_rows=[
                ("1SYN_TEST_A", "WBS.001"),
                ("1SYN_TEST_A", "WBS.002"),
                ("1SYN_TEST_A", "WBS.003"),
            ],
        )
        with sqlite3.connect(self.global_db) as conn:
            result = build_material_shortage(self.zpp7_db, conn)

        # 3 materials × 3 WBS = 9 records
        self.assertEqual(len(result), 9)
        wbs_values = {rec["WBS Element"] for rec in result}
        self.assertEqual(wbs_values, {"WBS.001", "WBS.002", "WBS.003"})

    def test_duplicate_zpp7_entries_aggregate(self):
        """Duplicate (ref, material) BOM entries should aggregate quantities."""
        _create_zpp7_db(self.zpp7_db, "1SYN_TEST_A", [
            ("1SYN_TEST_A", "MAT001", "Bolt M10", 60.0),
        ])
        # Add another table with same material (simulating multiple ZPP7 tables)
        conn = sqlite3.connect(self.zpp7_db)
        conn.execute(
            'CREATE TABLE "1SYN_TEST_B" ('
            '"Material Ref Code" TEXT, '
            '"Material Code" TEXT, '
            '"Description" TEXT, '
            '"ZPP7 Quantity" REAL, '
            'PRIMARY KEY ("Material Ref Code", "Material Code"))'
        )
        conn.execute(
            'INSERT INTO "1SYN_TEST_B" VALUES (?, ?, ?, ?)',
            ("1SYN_TEST_A", "MAT001", "Bolt M10", 40.0),
        )
        conn.commit()
        conn.close()

        _create_global_db(self.global_db, mb25_rows=[("1SYN_TEST_A", "WBS.001")])
        with sqlite3.connect(self.global_db) as gconn:
            result = build_material_shortage(self.zpp7_db, gconn)

        # Should aggregate: 60 + 40 = 100
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["ZPP7 Quantity"], 100.0)


# =======================================================================
# Test 4-6: MB52 enrichment
# =======================================================================
class TestEnrichMB52(NetShortageTestBase):
    """Tests for Step 2 — MB52 Project Stock and Free Stock."""

    def _base_records(self) -> list[dict]:
        return [
            {"Material Ref Code": "REF1", "Material Code": "MAT001",
             "WBS Element": "WBS.001", "Material Description": "Bolt",
             "ZPP7 Quantity": 100.0, "_mb25_matched": True},
            {"Material Ref Code": "REF1", "Material Code": "MAT002",
             "WBS Element": "WBS.001", "Material Description": "Nut",
             "ZPP7 Quantity": 50.0, "_mb25_matched": True},
        ]

    def test_project_stock_match(self):
        """MB52 record with matching Material Code + WBS → Project Stock."""
        self._build_standard_dbs(
            mb52_rows=[
                ("MAT001", "Bolt", "25", "WBS.001"),  # Project stock
            ],
        )
        records = self._base_records()
        with sqlite3.connect(self.global_db) as conn:
            enrich_with_mb52(records, conn)

        self.assertEqual(records[0]["Project Stock Quantity"], 25.0)
        self.assertEqual(records[1]["Project Stock Quantity"], 0.0)

    def test_free_stock_fallback(self):
        """MB52 record without WBS → Free Stock for all matching materials."""
        self._build_standard_dbs(
            mb52_rows=[
                ("MAT001", "Bolt", "30", None),   # Free stock (NULL WBS)
                ("MAT002", "Nut", "15", "None"),   # Free stock ("None" string)
            ],
        )
        records = self._base_records()
        with sqlite3.connect(self.global_db) as conn:
            enrich_with_mb52(records, conn)

        # No project stock for either
        self.assertEqual(records[0]["Project Stock Quantity"], 0.0)
        self.assertEqual(records[1]["Project Stock Quantity"], 0.0)
        # Free stock populated
        self.assertEqual(records[0]["Free Stock"], 30.0)
        self.assertEqual(records[1]["Free Stock"], 15.0)

    def test_project_and_free_stock_separate(self):
        """Project Stock and Free Stock must remain separate — never combined."""
        self._build_standard_dbs(
            mb52_rows=[
                ("MAT001", "Bolt", "25", "WBS.001"),  # Project stock
                ("MAT001", "Bolt", "100", None),       # Free stock
            ],
        )
        records = self._base_records()
        with sqlite3.connect(self.global_db) as conn:
            enrich_with_mb52(records, conn)

        self.assertEqual(records[0]["Project Stock Quantity"], 25.0)
        self.assertEqual(records[0]["Free Stock"], 100.0)
        # They must NOT be summed
        self.assertNotEqual(records[0]["Project Stock Quantity"], 125.0)

    def test_missing_stock_record(self):
        """No MB52 match → both stock columns default to 0.0."""
        self._build_standard_dbs(mb52_rows=[])
        records = self._base_records()
        with sqlite3.connect(self.global_db) as conn:
            enrich_with_mb52(records, conn)

        self.assertEqual(records[0]["Project Stock Quantity"], 0.0)
        self.assertEqual(records[0]["Free Stock"], 0.0)


# =======================================================================
# Test 7: ME2J enrichment
# =======================================================================
class TestEnrichME2J(NetShortageTestBase):
    """Tests for Step 3 — Purchase Order quantities."""

    def _base_records(self) -> list[dict]:
        return [
            {"Material Ref Code": "REF1", "Material Code": "MAT001",
             "WBS Element": "WBS.001", "Material Description": "Bolt",
             "ZPP7 Quantity": 100.0},
        ]

    def test_single_po_line(self):
        """Single ME2J PO line should populate PO and Open PO quantities."""
        self._build_standard_dbs(
            me2j_rows=[
                ("WBS.001", "MAT001", "Bolt", "50", "20"),
            ],
        )
        records = self._base_records()
        with sqlite3.connect(self.global_db) as conn:
            enrich_with_me2j(records, conn)

        self.assertEqual(records[0]["PO Quantity"], 50.0)
        self.assertEqual(records[0]["Open PO Quantity"], 20.0)

    def test_multiple_po_lines_aggregate(self):
        """Multiple ME2J lines for same Material+WBS should aggregate."""
        self._build_standard_dbs(
            me2j_rows=[
                ("WBS.001", "MAT001", "Bolt", "30", "10"),
                ("WBS.001", "MAT001", "Bolt", "20", "5"),
            ],
        )
        records = self._base_records()
        with sqlite3.connect(self.global_db) as conn:
            enrich_with_me2j(records, conn)

        self.assertEqual(records[0]["PO Quantity"], 50.0)
        self.assertEqual(records[0]["Open PO Quantity"], 15.0)

    def test_no_po_match(self):
        """No ME2J match → PO columns default to 0.0."""
        self._build_standard_dbs(me2j_rows=[])
        records = self._base_records()
        with sqlite3.connect(self.global_db) as conn:
            enrich_with_me2j(records, conn)

        self.assertEqual(records[0]["PO Quantity"], 0.0)
        self.assertEqual(records[0]["Open PO Quantity"], 0.0)


# =======================================================================
# Test 8: PR enrichment
# =======================================================================
class TestEnrichPR(NetShortageTestBase):
    """Tests for Step 4 — Purchase Requisition quantities."""

    def _base_records(self) -> list[dict]:
        return [
            {"Material Ref Code": "REF1", "Material Code": "MAT001",
             "WBS Element": "WBS.001", "Material Description": "Bolt",
             "ZPP7 Quantity": 100.0},
        ]

    def test_single_pr_line(self):
        self._build_standard_dbs(
            pr_rows=[("WBS.001", "MAT001", "Bolt", "25")],
        )
        records = self._base_records()
        with sqlite3.connect(self.global_db) as conn:
            enrich_with_pr(records, conn)

        self.assertEqual(records[0]["PR Quantity"], 25.0)

    def test_multiple_pr_lines_aggregate(self):
        """Multiple PR lines for same Material+WBS should sum."""
        self._build_standard_dbs(
            pr_rows=[
                ("WBS.001", "MAT001", "Bolt", "10"),
                ("WBS.001", "MAT001", "Bolt", "15"),
            ],
        )
        records = self._base_records()
        with sqlite3.connect(self.global_db) as conn:
            enrich_with_pr(records, conn)

        self.assertEqual(records[0]["PR Quantity"], 25.0)


# =======================================================================
# Test 9: ScopeList enrichment
# =======================================================================
class TestEnrichScope(NetShortageTestBase):
    """Tests for Step 5 — Final Scope."""

    def _base_records(self) -> list[dict]:
        return [
            {"Material Ref Code": "REF1", "Material Code": "MAT001",
             "WBS Element": "WBS.001", "Material Description": "Bolt",
             "ZPP7 Quantity": 100.0},
            {"Material Ref Code": "REF1", "Material Code": "MAT002",
             "WBS Element": "WBS.001", "Material Description": "Nut",
             "ZPP7 Quantity": 50.0},
        ]

    def test_scope_match(self):
        self._build_standard_dbs(
            scope_rows=[("MAT001", "Bolt", "ABB")],
        )
        records = self._base_records()
        with sqlite3.connect(self.global_db) as conn:
            enrich_with_scope(records, conn)

        self.assertEqual(records[0]["Scope"], "ABB")
        self.assertEqual(records[1]["Scope"], "")  # No match for MAT002

    def test_missing_scope_match(self):
        """Materials with no ScopeList entry get empty Scope."""
        self._build_standard_dbs(scope_rows=[])
        records = self._base_records()
        with sqlite3.connect(self.global_db) as conn:
            enrich_with_scope(records, conn)

        for rec in records:
            self.assertEqual(rec["Scope"], "")

    def test_scope_priority_abb_over_vendor(self):
        """ABB scope should take priority over Vendor Scope for same material."""
        self._build_standard_dbs(
            scope_rows=[
                ("MAT001", "Bolt", "Vendor Scope"),
                ("MAT001", "Bolt", "ABB"),
            ],
        )
        records = self._base_records()
        with sqlite3.connect(self.global_db) as conn:
            enrich_with_scope(records, conn)

        self.assertEqual(records[0]["Scope"], "ABB")


# =======================================================================
# Test 10-12: ME2M enrichment + Vendor resolution
# =======================================================================
class TestEnrichME2M(NetShortageTestBase):
    """Tests for Step 6 — Vendor Detail + Proposed Date."""

    def _base_records(self) -> list[dict]:
        return [
            {"Material Ref Code": "REF1", "Material Code": "MAT001",
             "WBS Element": "WBS.001", "Material Description": "Bolt",
             "ZPP7 Quantity": 100.0},
        ]

    def test_single_vendor(self):
        """Single vendor for material → directly assigned."""
        self._build_standard_dbs(
            me2m_rows=[
                ("MAT001", "Bolt", "Vendor A Corp", "WBS.001", "2026-12-01"),
            ],
        )
        records = self._base_records()
        with sqlite3.connect(self.global_db) as conn:
            enrich_with_me2m(records, conn)

        self.assertEqual(records[0]["Vendor Detail"], "Vendor A Corp")
        self.assertEqual(records[0]["_proposed_date"], "2026-12-01")

    def test_multi_vendor_wbs_resolved(self):
        """Multiple vendors but only one matches WBS → resolved."""
        self._build_standard_dbs(
            me2m_rows=[
                ("MAT001", "Bolt", "Vendor A", "WBS.001", "2026-12-01"),
                ("MAT001", "Bolt", "Vendor B", "WBS.002", "2026-11-15"),
            ],
        )
        records = self._base_records()
        with sqlite3.connect(self.global_db) as conn:
            enrich_with_me2m(records, conn)

        # WBS.001 matches only Vendor A
        self.assertEqual(records[0]["Vendor Detail"], "Vendor A")

    def test_multi_vendor_unresolved(self):
        """Multiple vendors for same WBS → ALLOCATION_UNRESOLVED."""
        self._build_standard_dbs(
            me2m_rows=[
                ("MAT001", "Bolt", "Vendor A", "WBS.001", "2026-12-01"),
                ("MAT001", "Bolt", "Vendor B", "WBS.001", "2026-11-15"),
            ],
        )
        records = self._base_records()
        with sqlite3.connect(self.global_db) as conn:
            enrich_with_me2m(records, conn)

        self.assertIn("[Unresolved]", records[0]["Vendor Detail"])
        self.assertIn("Vendor A", records[0]["Vendor Detail"])
        self.assertIn("Vendor B", records[0]["Vendor Detail"])

    def test_missing_vendor(self):
        """No ME2M match → empty Vendor Detail."""
        self._build_standard_dbs(me2m_rows=[])
        records = self._base_records()
        with sqlite3.connect(self.global_db) as conn:
            enrich_with_me2m(records, conn)

        self.assertEqual(records[0]["Vendor Detail"], "")


# =======================================================================
# Test 13: Missing delivery date
# =======================================================================
class TestMissingDeliveryDate(NetShortageTestBase):
    def test_empty_proposed_date(self):
        """ME2M with empty delivery date → empty proposed date."""
        self._build_standard_dbs(
            me2m_rows=[
                ("MAT001", "Bolt", "Vendor A", "WBS.001", ""),
            ],
        )
        records = [
            {"Material Ref Code": "REF1", "Material Code": "MAT001",
             "WBS Element": "WBS.001", "Material Description": "Bolt",
             "ZPP7 Quantity": 100.0},
        ]
        with sqlite3.connect(self.global_db) as conn:
            enrich_with_me2m(records, conn)

        self.assertEqual(records[0]["_proposed_date"], "")
        self.assertEqual(records[0]["Vendor Detail"], "Vendor A")


# =======================================================================
# Test 14: Net Shortage formula
# =======================================================================
class TestNetShortageCalculation(unittest.TestCase):
    """Tests for Step 7 — Net Shortage = max(ZPP7 - Project Stock, 0)."""

    def test_normal_shortage(self):
        records = [{"ZPP7 Quantity": 100.0, "Project Stock Quantity": 30.0}]
        calculate_net_shortage(records)
        self.assertEqual(records[0]["Net Shortage"], 70.0)

    def test_no_shortage(self):
        records = [{"ZPP7 Quantity": 50.0, "Project Stock Quantity": 100.0}]
        calculate_net_shortage(records)
        self.assertEqual(records[0]["Net Shortage"], 0.0)

    def test_negative_clamped_to_zero(self):
        """Excess stock should NOT produce negative Net Shortage."""
        records = [{"ZPP7 Quantity": 10.0, "Project Stock Quantity": 50.0}]
        calculate_net_shortage(records)
        self.assertEqual(records[0]["Net Shortage"], 0.0)

    def test_zero_stock(self):
        records = [{"ZPP7 Quantity": 100.0, "Project Stock Quantity": 0.0}]
        calculate_net_shortage(records)
        self.assertEqual(records[0]["Net Shortage"], 100.0)

    def test_zero_requirement(self):
        records = [{"ZPP7 Quantity": 0.0, "Project Stock Quantity": 50.0}]
        calculate_net_shortage(records)
        self.assertEqual(records[0]["Net Shortage"], 0.0)

    def test_free_stock_not_subtracted(self):
        """Free Stock MUST NOT affect Net Shortage calculation."""
        records = [
            {"ZPP7 Quantity": 100.0, "Project Stock Quantity": 0.0,
             "Free Stock": 999.0},
        ]
        calculate_net_shortage(records)
        # Free Stock is 999 but Net Shortage should be 100, not 0
        self.assertEqual(records[0]["Net Shortage"], 100.0)


# =======================================================================
# Test 15-16: Final Date and Remarks preservation
# =======================================================================
class TestPreserveExistingFields(NetShortageTestBase):
    """Tests for Step 8 — preserving Final Date and Remarks."""

    def _seed_services_db(self, rows: list[tuple]) -> None:
        """Seed services.db with existing net_shortage data."""
        conn = sqlite3.connect(self.services_db)
        cols = ", ".join(f'"{c}"' for c in FULL_DB_COLUMNS)
        placeholders = ", ".join("?" for _ in FULL_DB_COLUMNS)
        conn.execute(
            f'CREATE TABLE "net_shortage" ({cols})'
        )
        conn.executemany(
            f'INSERT INTO "net_shortage" ({cols}) VALUES ({placeholders})',
            rows,
        )
        conn.commit()
        conn.close()

    def _make_row(self, ref, mat, wbs, final_date="", remarks="", project="PROJ1"):
        """Build a full row tuple in FULL_DB_COLUMNS order."""
        vals = {
            "project_id": project,
            "Material Ref Code": ref,
            "Material Code": mat,
            "WBS Element": wbs,
            "Material Description": "Test Material",
            "ZPP7 Quantity": 100.0,
            "Project Stock Quantity": 10.0,
            "Net Shortage": 90.0,
            "PO Quantity": 0.0,
            "Open PO Quantity": 0.0,
            "Final Date": final_date,
            "Free Stock": 0.0,
            "Vendor Detail": "",
            "Scope": "",
            "Remarks": remarks,
            "PR Quantity": 0.0,
        }
        return tuple(vals[c] for c in FULL_DB_COLUMNS)

    def test_final_date_preserved(self):
        """Existing Final Date must survive reprocessing."""
        self._seed_services_db([
            self._make_row("REF1", "MAT001", "WBS.001",
                           final_date="2026-12-15"),
        ])
        records = [
            {"Material Ref Code": "REF1", "Material Code": "MAT001",
             "WBS Element": "WBS.001", "ZPP7 Quantity": 100.0},
        ]
        with sqlite3.connect(self.services_db) as conn:
            preserve_existing_fields(records, "PROJ1", conn)

        self.assertEqual(records[0]["Final Date"], "2026-12-15")

    def test_user_remarks_preserved(self):
        """User-written Remarks must not be overwritten."""
        self._seed_services_db([
            self._make_row("REF1", "MAT001", "WBS.001",
                           remarks="Material delayed — vendor confirmed Q1"),
        ])
        records = [
            {"Material Ref Code": "REF1", "Material Code": "MAT001",
             "WBS Element": "WBS.001", "ZPP7 Quantity": 100.0},
        ]
        with sqlite3.connect(self.services_db) as conn:
            preserve_existing_fields(records, "PROJ1", conn)

        self.assertEqual(records[0]["Remarks"],
                         "Material delayed — vendor confirmed Q1")

    def test_system_remarks_can_be_overwritten(self):
        """System-generated [WARNING:] remarks can be replaced."""
        self._seed_services_db([
            self._make_row("REF1", "MAT001", "WBS.001",
                           remarks="[WARNING: Allocation Unresolved — X]"),
        ])
        records = [
            {"Material Ref Code": "REF1", "Material Code": "MAT001",
             "WBS Element": "WBS.001", "ZPP7 Quantity": 100.0},
        ]
        with sqlite3.connect(self.services_db) as conn:
            preserve_existing_fields(records, "PROJ1", conn)

        # System remark should NOT be preserved (it was a system warning)
        remark = records[0]["Remarks"]
        self.assertNotEqual(remark,
                            "[WARNING: Allocation Unresolved — X]")

    def test_no_existing_data(self):
        """When services.db doesn't exist, fields default to empty."""
        # Don't create services.db
        records = [
            {"Material Ref Code": "REF1", "Material Code": "MAT001",
             "WBS Element": "WBS.001", "ZPP7 Quantity": 100.0},
        ]
        conn = sqlite3.connect(":memory:")
        preserve_existing_fields(records, "PROJ1", conn)
        conn.close()

        self.assertEqual(records[0]["Final Date"], "")
        self.assertEqual(records[0]["Remarks"], "")


# =======================================================================
# Test 17: Project isolation
# =======================================================================
class TestProjectIsolation(NetShortageTestBase):
    """Processing one project must not affect another's data."""

    def test_save_preserves_other_projects(self):
        """Saving project A must not delete/change project B rows."""
        # Pre-seed with project B data
        conn = sqlite3.connect(self.services_db)
        conn.execute(
            f'CREATE TABLE "net_shortage" ({", ".join(f"""{chr(34)}{c}{chr(34)} TEXT""" for c in FULL_DB_COLUMNS)})'
        )
        for i in range(5):
            vals = ["PROJ_B"] + [f"val_{i}_{j}" for j in range(len(FULL_DB_COLUMNS) - 1)]
            conn.execute(
                f'INSERT INTO "net_shortage" VALUES ({", ".join("?" for _ in FULL_DB_COLUMNS)})',
                vals,
            )
        conn.commit()
        conn.close()

        # Save project A data
        project_a_records = [
            {
                "Material Ref Code": "REF1",
                "Material Code": "MAT001",
                "WBS Element": "WBS.001",
                "Material Description": "Bolt",
                "ZPP7 Quantity": 100.0,
                "Project Stock Quantity": 0.0,
                "Net Shortage": 100.0,
                "PO Quantity": 0.0,
                "Open PO Quantity": 0.0,
                "Final Date": "",
                "Free Stock": 0.0,
                "Vendor Detail": "",
                "Scope": "",
                "Remarks": "",
                "PR Quantity": 0.0,
            },
        ]
        save_net_shortage(project_a_records, "PROJ_A", self.services_db)

        # Verify project B data unchanged
        conn = sqlite3.connect(self.services_db)
        proj_b_count = conn.execute(
            'SELECT COUNT(*) FROM "net_shortage" WHERE "project_id" = ?',
            ("PROJ_B",),
        ).fetchone()[0]
        proj_a_count = conn.execute(
            'SELECT COUNT(*) FROM "net_shortage" WHERE "project_id" = ?',
            ("PROJ_A",),
        ).fetchone()[0]
        conn.close()

        self.assertEqual(proj_b_count, 5)
        self.assertEqual(proj_a_count, 1)


# =======================================================================
# Test 18: Idempotent reprocessing
# =======================================================================
class TestIdempotency(NetShortageTestBase):
    """Running the same project twice must produce identical results."""

    def test_reprocess_same_count(self):
        """Re-running should replace, not double, the records."""
        records = [
            {
                "Material Ref Code": "REF1",
                "Material Code": "MAT001",
                "WBS Element": "WBS.001",
                "Material Description": "Bolt",
                "ZPP7 Quantity": 100.0,
                "Project Stock Quantity": 0.0,
                "Net Shortage": 100.0,
                "PO Quantity": 0.0,
                "Open PO Quantity": 0.0,
                "Final Date": "",
                "Free Stock": 0.0,
                "Vendor Detail": "",
                "Scope": "",
                "Remarks": "",
                "PR Quantity": 0.0,
            },
        ]
        # Run twice
        save_net_shortage(records, "PROJ1", self.services_db)
        save_net_shortage(records, "PROJ1", self.services_db)

        conn = sqlite3.connect(self.services_db)
        count = conn.execute(
            'SELECT COUNT(*) FROM "net_shortage" WHERE "project_id" = ?',
            ("PROJ1",),
        ).fetchone()[0]
        conn.close()

        self.assertEqual(count, 1)  # Not 2


# =======================================================================
# Test 19: No duplicate records
# =======================================================================
class TestNoDuplicates(NetShortageTestBase):
    """Validation should detect duplicates."""

    def test_duplicate_detection(self):
        records = [
            {"Material Ref Code": "REF1", "Material Code": "MAT001",
             "WBS Element": "WBS.001", "_mb25_matched": True,
             "ZPP7 Quantity": 100.0, "Net Shortage": 100.0},
            {"Material Ref Code": "REF1", "Material Code": "MAT001",
             "WBS Element": "WBS.001", "_mb25_matched": True,
             "ZPP7 Quantity": 50.0, "Net Shortage": 50.0},
        ]
        diag = validate_material_shortage(records, "PROJ1")
        self.assertFalse(diag["valid"])
        self.assertGreater(len(diag["duplicate_keys"]), 0)


# =======================================================================
# Test 20: Column contract
# =======================================================================
class TestColumnContract(unittest.TestCase):
    """Verify the output column specifications."""

    def test_output_columns_match_spec(self):
        """The 14 specified business columns must be present in order."""
        expected = [
            "Material Ref Code", "Material Code", "WBS Element",
            "Material Description", "ZPP7 Quantity", "Project Stock Quantity",
            "Net Shortage", "PO Quantity", "Open PO Quantity", "Final Date",
            "Free Stock", "Vendor Detail", "Scope", "Remarks",
        ]
        # Note: spec says "Description" but DB has "Material Description"
        # and "Final Scope" is stored as "Scope" for backward compat.
        self.assertEqual(OUTPUT_COLUMNS_ORDERED, expected)

    def test_full_db_columns_includes_pr_quantity(self):
        """PR Quantity should be an additional column for backward compat."""
        self.assertIn("PR Quantity", FULL_DB_COLUMNS)
        self.assertIn("project_id", FULL_DB_COLUMNS)

    def test_identity_columns(self):
        self.assertEqual(
            IDENTITY_COLUMNS,
            ("project_id", "Material Ref Code", "Material Code", "WBS Element"),
        )


# =======================================================================
# Test 21: End-to-end orchestration
# =======================================================================
class TestEndToEnd(NetShortageTestBase):
    """Full pipeline test with generate_material_shortage_register()."""

    def test_full_pipeline(self):
        """Complete end-to-end run with all enrichments."""
        self._build_standard_dbs(
            zpp7_rows=[
                ("1SYN_TEST_A", "MAT001", "Bolt M10", 100.0),
                ("1SYN_TEST_A", "MAT002", "Nut M10", 50.0),
            ],
            mb25_rows=[("1SYN_TEST_A", "WBS.001")],
            mb52_rows=[
                ("MAT001", "Bolt", "25", "WBS.001"),   # Project stock
                ("MAT002", "Nut", "10", None),           # Free stock
            ],
            me2j_rows=[
                ("WBS.001", "MAT001", "Bolt", "30", "10"),
            ],
            pr_rows=[
                ("WBS.001", "MAT001", "Bolt", "15"),
            ],
            scope_rows=[
                ("MAT001", "Bolt", "ABB"),
                ("MAT002", "Nut", "Vendor Scope"),
            ],
            me2m_rows=[
                ("MAT001", "Bolt", "Vendor Corp", "WBS.001", "2026-12-01"),
            ],
        )

        result = generate_material_shortage_register(
            "TEST_PROJECT",
            global_db=self.global_db,
            zpp7_db=self.zpp7_db,
            services_db=self.services_db,
        )

        self.assertEqual(result["project_id"], "TEST_PROJECT")
        self.assertEqual(result["rows_written"], 2)
        self.assertGreater(result["elapsed_seconds"], 0)

        # Verify database content
        conn = sqlite3.connect(self.services_db)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            'SELECT * FROM "net_shortage" WHERE "project_id" = ? '
            'ORDER BY "Material Code"',
            ("TEST_PROJECT",),
        ).fetchall()
        conn.close()

        self.assertEqual(len(rows), 2)

        mat001 = dict(rows[0])
        self.assertEqual(mat001["Material Code"], "MAT001")
        self.assertEqual(mat001["WBS Element"], "WBS.001")
        self.assertEqual(mat001["Project Stock Quantity"], 25.0)
        self.assertEqual(mat001["Net Shortage"], 75.0)   # 100 - 25
        self.assertEqual(mat001["PO Quantity"], 30.0)
        self.assertEqual(mat001["Open PO Quantity"], 10.0)
        self.assertEqual(mat001["PR Quantity"], 15.0)
        self.assertEqual(mat001["Scope"], "ABB")
        self.assertEqual(mat001["Vendor Detail"], "Vendor Corp")

        mat002 = dict(rows[1])
        self.assertEqual(mat002["Material Code"], "MAT002")
        self.assertEqual(mat002["Project Stock Quantity"], 0.0)
        self.assertEqual(mat002["Net Shortage"], 50.0)   # 50 - 0
        self.assertEqual(mat002["Free Stock"], 10.0)
        self.assertEqual(mat002["Scope"], "Vendor Scope")

    def test_reprocess_preserves_final_date(self):
        """End-to-end: Final Date survives a reprocess."""
        self._build_standard_dbs(
            zpp7_rows=[("1SYN_TEST_A", "MAT001", "Bolt", 100.0)],
            mb25_rows=[("1SYN_TEST_A", "WBS.001")],
        )

        # First run
        generate_material_shortage_register(
            "PROJ1",
            global_db=self.global_db,
            zpp7_db=self.zpp7_db,
            services_db=self.services_db,
        )

        # Manually set a Final Date
        conn = sqlite3.connect(self.services_db)
        conn.execute(
            'UPDATE "net_shortage" SET "Final Date" = ? '
            'WHERE "project_id" = ? AND "Material Code" = ?',
            ("2026-12-31", "PROJ1", "MAT001"),
        )
        conn.commit()
        conn.close()

        # Re-run
        generate_material_shortage_register(
            "PROJ1",
            global_db=self.global_db,
            zpp7_db=self.zpp7_db,
            services_db=self.services_db,
        )

        # Verify Final Date preserved
        conn = sqlite3.connect(self.services_db)
        row = conn.execute(
            'SELECT "Final Date" FROM "net_shortage" '
            'WHERE "project_id" = ? AND "Material Code" = ?',
            ("PROJ1", "MAT001"),
        ).fetchone()
        conn.close()

        self.assertEqual(row[0], "2026-12-31")


# =======================================================================
# Test 22: Schema migration
# =======================================================================
class TestSchemaMigration(NetShortageTestBase):
    """Test that schema migration preserves data."""

    def test_migrate_adds_missing_columns(self):
        """If net_shortage exists with fewer columns, migration should handle it."""
        # Create old-style table with fewer columns
        conn = sqlite3.connect(self.services_db)
        conn.execute(
            'CREATE TABLE "net_shortage" ('
            '"project_id" TEXT, '
            '"Material Ref Code" TEXT, '
            '"Material Code" TEXT, '
            '"WBS Element" TEXT, '
            '"Material Description" TEXT, '
            '"ZPP7 Quantity" REAL, '
            '"Net Shortage" REAL'
            ')'
        )
        conn.execute(
            'INSERT INTO "net_shortage" VALUES (?, ?, ?, ?, ?, ?, ?)',
            ("OLD_PROJ", "REF1", "MAT001", "WBS.001", "Bolt", 100.0, 90.0),
        )
        conn.commit()
        conn.close()

        # Save new data (triggers migration)
        records = [
            {
                "Material Ref Code": "REF2",
                "Material Code": "MAT002",
                "WBS Element": "WBS.002",
                "Material Description": "Nut",
                "ZPP7 Quantity": 50.0,
                "Project Stock Quantity": 0.0,
                "Net Shortage": 50.0,
                "PO Quantity": 0.0,
                "Open PO Quantity": 0.0,
                "Final Date": "",
                "Free Stock": 0.0,
                "Vendor Detail": "",
                "Scope": "",
                "Remarks": "",
                "PR Quantity": 0.0,
            },
        ]
        rows = save_net_shortage(records, "NEW_PROJ", self.services_db)
        self.assertEqual(rows, 1)

        # Verify old data preserved
        conn = sqlite3.connect(self.services_db)
        old = conn.execute(
            'SELECT "Material Code", "Net Shortage" FROM "net_shortage" '
            'WHERE "project_id" = ?',
            ("OLD_PROJ",),
        ).fetchone()
        conn.close()

        self.assertIsNotNone(old)
        self.assertEqual(old[0], "MAT001")
        self.assertEqual(old[1], 90.0)


# =======================================================================
# Test 23: VendorResolution class
# =======================================================================
class TestVendorResolution(unittest.TestCase):
    """Tests for the VendorResolution helper and _resolve_vendor."""

    def test_single_vendor_resolution(self):
        records = [
            {"Vendor Detail": "Siemens AG", "WBS Element": "WBS.001",
             "Proposed Date": "2026-12-01"},
        ]
        result = _resolve_vendor(records, "WBS.001")
        self.assertEqual(result.status, "SINGLE_VENDOR")
        self.assertEqual(result.vendor, "Siemens AG")
        self.assertFalse(result.has_conflict)
        self.assertEqual(result.proposed_date, "2026-12-01")

    def test_no_vendor_records(self):
        result = _resolve_vendor([], "WBS.001")
        self.assertEqual(result.status, "VENDOR_UNAVAILABLE")
        self.assertIsNone(result.vendor)

    def test_multiple_vendors_wbs_resolved(self):
        """If WBS narrows to single vendor, it's resolved as SINGLE_VENDOR
        because the WBS filter produces a single-vendor source set."""
        records = [
            {"Vendor Detail": "Vendor A", "WBS Element": "WBS.001", "Proposed Date": ""},
            {"Vendor Detail": "Vendor B", "WBS Element": "WBS.002", "Proposed Date": ""},
        ]
        result = _resolve_vendor(records, "WBS.001")
        # WBS filter narrows source to only Vendor A → SINGLE_VENDOR
        self.assertEqual(result.status, "SINGLE_VENDOR")
        self.assertEqual(result.vendor, "Vendor A")
        self.assertFalse(result.has_conflict)

    def test_multiple_vendors_unresolved(self):
        """Multiple vendors for same WBS → unresolved."""
        records = [
            {"Vendor Detail": "Vendor A", "WBS Element": "WBS.001", "Proposed Date": ""},
            {"Vendor Detail": "Vendor B", "WBS Element": "WBS.001", "Proposed Date": ""},
        ]
        result = _resolve_vendor(records, "WBS.001")
        self.assertEqual(result.status, "ALLOCATION_UNRESOLVED")
        self.assertIsNone(result.vendor)
        self.assertTrue(result.has_conflict)
        self.assertEqual(set(result.vendors), {"Vendor A", "Vendor B"})

    def test_never_silently_picks_first(self):
        """Must never just pick the first vendor when ambiguous."""
        records = [
            {"Vendor Detail": "ACME Corp", "WBS Element": "WBS.001", "Proposed Date": ""},
            {"Vendor Detail": "Beta Ltd", "WBS Element": "WBS.001", "Proposed Date": ""},
            {"Vendor Detail": "Gamma Inc", "WBS Element": "WBS.001", "Proposed Date": ""},
        ]
        result = _resolve_vendor(records, "WBS.001")
        # Should NOT just pick "ACME Corp" (the first)
        self.assertIsNone(result.vendor)
        self.assertEqual(result.status, "ALLOCATION_UNRESOLVED")

    def test_latest_proposed_date(self):
        """Should track the latest proposed date across records."""
        records = [
            {"Vendor Detail": "Vendor A", "WBS Element": "WBS.001",
             "Proposed Date": "2026-10-01"},
            {"Vendor Detail": "Vendor A", "WBS Element": "WBS.001",
             "Proposed Date": "2026-12-01"},
        ]
        result = _resolve_vendor(records, "WBS.001")
        self.assertEqual(result.proposed_date, "2026-12-01")


# =======================================================================
# Test 24: Edge cases and utilities
# =======================================================================
class TestUtilities(unittest.TestCase):
    """Tests for utility functions."""

    def test_clean_none(self):
        self.assertEqual(_clean(None), "")

    def test_clean_none_string(self):
        """The literal string 'None' should become empty."""
        self.assertEqual(_clean("None"), "")

    def test_clean_whitespace(self):
        self.assertEqual(_clean("  hello  "), "hello")

    def test_to_float_none(self):
        self.assertEqual(_to_float(None), 0.0)

    def test_to_float_empty_string(self):
        self.assertEqual(_to_float(""), 0.0)

    def test_to_float_comma_formatted(self):
        self.assertEqual(_to_float("1,234.56"), 1234.56)

    def test_to_float_none_string(self):
        self.assertEqual(_to_float("None"), 0.0)

    def test_to_float_valid_int(self):
        self.assertEqual(_to_float(42), 42.0)

    def test_to_float_invalid_raises(self):
        with self.assertRaises(ValueError):
            _to_float("abc", context="test")


class TestValidation(unittest.TestCase):
    """Tests for validate_material_shortage."""

    def test_empty_dataset_invalid(self):
        diag = validate_material_shortage([], "PROJ1")
        self.assertFalse(diag["valid"])
        self.assertIn("No records to write", diag["errors"][0])

    def test_valid_dataset(self):
        records = [
            {"Material Ref Code": "R1", "Material Code": "M1",
             "WBS Element": "W1", "_mb25_matched": True,
             "ZPP7 Quantity": 100.0, "Net Shortage": 90.0},
        ]
        diag = validate_material_shortage(records, "PROJ1")
        self.assertTrue(diag["valid"])
        self.assertEqual(diag["total_records"], 1)
        self.assertEqual(diag["matched_records"], 1)


# =======================================================================
# Test: DB_COLUMNS compatibility
# =======================================================================
class TestDBColumnsCompatibility(unittest.TestCase):
    """Verify that net_shortage output columns match what data.py expects."""

    def test_all_db_columns_present_in_output(self):
        """Every column in shortage/data.py DB_COLUMNS must be in net_shortage output."""
        from shortage.data import DB_COLUMNS
        for col in DB_COLUMNS:
            self.assertIn(
                col, FULL_DB_COLUMNS,
                f"Column {col!r} from data.py DB_COLUMNS missing in net_shortage FULL_DB_COLUMNS",
            )


if __name__ == "__main__":
    unittest.main()

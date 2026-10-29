import sqlite3
import tempfile
from pathlib import Path

from django.test import Client, SimpleTestCase, override_settings
from django.contrib.staticfiles import finders


class LocalDjangoPagesTests(SimpleTestCase):
    def test_local_pages_render(self):
        client = Client()
        for path in ("/", "/input/", "/vendor-track/", "/vendor-import/", "/health/"):
            with self.subTest(path=path):
                response = client.get(path)
                self.assertEqual(response.status_code, 200)

    def test_static_css_is_registered(self):
        self.assertIsNotNone(finders.find("shortage/app.css"))

    def test_state_changing_actions_require_csrf(self):
        client = Client(enforce_csrf_checks=True)
        response = client.post("/process/IN-10026019/")
        self.assertEqual(response.status_code, 403)

    def test_vendor_track_handles_legacy_table_without_project_id(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "services.sqlite3"
            conn = sqlite3.connect(database)
            try:
                conn.execute('CREATE TABLE vendor_track ("Vendor Detail" TEXT, "WBS Element" TEXT)')
                conn.execute('INSERT INTO vendor_track VALUES (?, ?)', ("Vendor A", "WBS-1"))
                conn.commit()
            finally:
                conn.close()
            with override_settings(SERVICES_DB=database):
                response = Client().get("/vendor-track/")
            self.assertEqual(response.status_code, 200)

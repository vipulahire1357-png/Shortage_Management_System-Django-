from pathlib import Path
import unittest


class ProjectStructureTests(unittest.TestCase):
    def test_core_django_and_business_directories_exist(self):
        root = Path(__file__).resolve().parents[1]
        for relative in ("abbsite", "shortage", "templates", "static", "parsers", "database", "services", "data", "tests"):
            with self.subTest(directory=relative):
                self.assertTrue((root / relative).is_dir())

    def test_legacy_frontend_and_packaging_directories_are_removed(self):
        root = Path(__file__).resolve().parents[1]
        for relative in ("frontend", "backend", "dashboard", "packaging"):
            with self.subTest(directory=relative):
                self.assertFalse((root / relative).exists())

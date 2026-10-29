import sys
import unittest
from unittest.mock import patch

import main


class MainPipelineTests(unittest.TestCase):
    def test_project_vendor_row_count_is_not_treated_as_failure_code(self):
        with (
            patch.object(sys, "argv", ["main.py", "--report", "all", "--project-id", "IN-10026019"]),
            patch.object(main, "available_parsers", return_value=["zpp7"]),
            patch.object(main, "run_parser", return_value=0),
            patch.object(main, "run_shortage_service", return_value=0),
            patch.object(main, "refresh_vendor_track_for_project", return_value=1948) as refresh,
        ):
            self.assertEqual(main.main(), 0)
        refresh.assert_called_once_with("IN-10026019")


if __name__ == "__main__":
    unittest.main()

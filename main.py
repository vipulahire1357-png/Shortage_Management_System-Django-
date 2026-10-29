"""Main entry point for the ABB Shortage System."""
import argparse
import logging

from parsers import available_parsers, run_parser
from services.shortage_service import main as run_shortage_service
from services.vendortrack_service import main as run_vendortrack_service
from services.vendortrack_service import refresh_vendor_track_for_project

def main() -> int:
    parser = argparse.ArgumentParser(description="ABB Shortage System - Report Processing")
    parser.add_argument("--report", choices=[*available_parsers(), "all"], default="all",
                        help="Run a specific report parser or all parsers (default: all)")
    parser.add_argument("--skip-shortage", action="store_true",
                        help="Run parsers only; skip shortage table generation")
    parser.add_argument("--project-id", default="",
                        help="Project ID to tag shortage records with (e.g. IN-10026019)")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")
    logger = logging.getLogger("abb_shortage")
    parser_names = available_parsers() if args.report == "all" else [args.report]
    failed = []
    for name in parser_names:
        try:
            logger.info("Starting %s parser", name.upper())
            result = run_parser(name)
            if isinstance(result, int) and not isinstance(result, bool) and result != 0:
                raise RuntimeError(f"Parser returned failure code {result}")
            logger.info("%s parser completed successfully. Result: %s", name.upper(), result)
        except Exception:
            logger.exception("%s parser failed", name.upper())
            failed.append(name)
    if failed:
        logger.error("Failed parsers: %s", ", ".join(failed))
        logger.error("Shortage table generation skipped to avoid using potentially incomplete report data.")
        return 1
    if args.skip_shortage:
        logger.info("Parser execution complete; shortage generation skipped.")
        return 0
    if args.report != "all":
        logger.info("Only one parser was requested. Run 'python main.py --report all' to process every report and generate output tables.")
        return 0
    try:
        logger.info("Starting ABB net-shortage table generation")
        result = run_shortage_service(project_id=args.project_id)
        if isinstance(result, int) and not isinstance(result, bool) and result != 0:
            raise RuntimeError(f"Shortage service returned failure code {result}")
        logger.info("Shortage table generated successfully.")
        logger.info("Starting vendor tracking table generation")
        # Project-aware runs must preserve the multi-project tracking schema and
        # existing vendor dates. The legacy whole-table refresh is for CLI runs
        # that do not identify a project.
        if args.project_id:
            rows = refresh_vendor_track_for_project(args.project_id)
            logger.info("Vendor tracking refreshed for %s: %s rows.", args.project_id, rows)
        else:
            result = run_vendortrack_service()
            if isinstance(result, int) and not isinstance(result, bool) and result != 0:
                raise RuntimeError(f"Vendor tracking service returned failure code {result}")
        logger.info("Vendor tracking table generated successfully.")
    except Exception:
        logger.exception("Output table generation failed")
        return 1
    logger.info("ABB Shortage System processing completed successfully.")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())

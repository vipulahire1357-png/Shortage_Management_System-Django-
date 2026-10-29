"""Registry and execution helpers for ABB report parsers."""

from importlib import import_module


PARSER_MODULES = {
    "mb25": "mb25_parser",
    "mb52": "mb52_parser",
    "me2j": "me2j_parser",
    "me2m": "me2m_parser",
    "pr": "pr_parser",
    "scopelist": "scopelist_parser",
    "zpp7": "zpp7_parser",
}


def available_parsers() -> list[str]:
    """Return all registered parser names."""
    return list(PARSER_MODULES.keys())


def run_parser(name: str):
    """Import and execute one parser's main() function."""
    normalized = name.strip().lower()

    if normalized not in PARSER_MODULES:
        raise ValueError(
            f"Unknown parser {name!r}. "
            f"Available parsers: {', '.join(available_parsers())}"
        )

    module_name = PARSER_MODULES[normalized]
    module = import_module(f".{module_name}", package=__name__)

    entrypoint = getattr(module, "main", None)

    if not callable(entrypoint):
        raise AttributeError(
            f"Parser module '{module_name}' must define a callable main()."
        )

    result = entrypoint()

    if isinstance(result, int) and not isinstance(result, bool) and result != 0:
        raise RuntimeError(
            f"Parser '{normalized}' returned failure code {result}."
        )

    return result


def run_all_parsers() -> dict:
    """Run every registered parser and continue if one fails."""
    results = {}

    for name in available_parsers():
        print(f"\n{'=' * 50}")
        print(f"Running {name.upper()} parser")
        print("=" * 50)

        try:
            result = run_parser(name)
            results[name] = {"status": "SUCCESS", "result": result}
        except Exception as error:
            results[name] = {"status": "FAILED", "error": str(error)}
            print(f"{name.upper()} parser failed: {error}")

    return results


__all__ = [
    "PARSER_MODULES",
    "available_parsers",
    "run_parser",
    "run_all_parsers",
]

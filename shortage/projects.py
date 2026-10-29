"""Safe report discovery and classification rules for project inputs."""
from pathlib import Path

def is_zpp7(path: Path) -> bool:
    return "zpp7" in path.name.casefold()

def is_mb25(path: Path) -> bool:
    return "mb25" in path.name.casefold()

def is_mb52(path: Path) -> bool:
    return "mb52" in path.name.casefold()

def is_me2j(path: Path) -> bool:
    return "me2j" in path.stem.casefold()

def is_pr(path: Path) -> bool:
    stem = path.stem.strip().casefold()
    return stem in {"pr", "pr 1", "purchase requisition"} or stem.startswith(("pr ", "pr_", "purchase requisition"))

def is_me2m(path: Path) -> bool:
    return "me2m" in path.name.casefold() and path.suffix.lower() in {".xls", ".xlsx", ".xlsm", ".xlsb"}

def is_scopelist(path: Path) -> bool:
    return "material_scope_list" in path.name.casefold()

PROJECT_REPORT_CLASSIFIERS = [
    ("zpp7", is_zpp7), ("mb25", is_mb25), ("me2j", is_me2j),
    ("pr", is_pr), ("me2m", is_me2m),
]
GLOBAL_REPORT_CLASSIFIERS = [("mb52", is_mb52), ("scopelist", is_scopelist)]

def classify_files(folder: Path, classifiers=PROJECT_REPORT_CLASSIFIERS, allowed_extensions=None):
    found = {key: None for key, _ in classifiers}
    if folder.is_dir():
        for path in sorted(folder.iterdir()):
            if not path.is_file() or path.name.startswith("~$"):
                continue
            if allowed_extensions and path.suffix.lower() not in allowed_extensions:
                continue
            for key, classifier in classifiers:
                if found[key] is None and classifier(path):
                    found[key] = path
                    break
    return found

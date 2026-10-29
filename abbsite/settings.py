"""Local-first Django settings; operational files remain in the existing repo folders."""
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
ENV_FILE = BASE_DIR / ".env"
if ENV_FILE.is_file():
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if key and key.replace("_", "").isalnum():
            os.environ.setdefault(key, value)
SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", "local-development-only-change-me")
DEBUG = os.environ.get("DJANGO_DEBUG", "1").lower() in {"1", "true", "yes"}
ALLOWED_HOSTS = ["127.0.0.1", "localhost", "[::1]"]
INSTALLED_APPS = [
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.contenttypes",
    "django.contrib.staticfiles",
    "shortage.apps.ShortageConfig",
]
MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
]
ROOT_URLCONF = "abbsite.urls"
TEMPLATES = [{
    "BACKEND": "django.template.backends.django.DjangoTemplates",
    "DIRS": [BASE_DIR / "templates"],
    "APP_DIRS": True,
    "OPTIONS": {"context_processors": ["django.template.context_processors.request", "django.contrib.messages.context_processors.messages"]},
}]
WSGI_APPLICATION = "abbsite.wsgi.application"
ASGI_APPLICATION = "abbsite.asgi.application"
# Django's own metadata uses a separate SQLite file; SAP/application records remain in
# the existing services.db/global.db/zpp7_breakdown.db and are never converted.
DATABASES = {"default": {
    "ENGINE": "django.db.backends.sqlite3",
    "NAME": Path(os.environ.get("DJANGO_METADATA_DB", BASE_DIR / "data" / "django.sqlite3")),
}}
LANGUAGE_CODE = "en-us"
TIME_ZONE = "Asia/Kolkata"
USE_I18N = True
USE_TZ = True
STATIC_URL = "static/"
STATICFILES_DIRS = [BASE_DIR / "static"]
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
DATA_ROOT = Path(os.environ.get("ABB_DATA_ROOT", BASE_DIR / "data")).resolve()
PROJECTS_DIR = Path(os.environ.get("ABB_PROJECTS_DIR", BASE_DIR / "projects")).resolve()
GLOBAL_DIR = Path(os.environ.get("ABB_GLOBAL_DIR", BASE_DIR / "global")).resolve()
SHORTAGE_REGISTERS_DIR = Path(os.environ.get("SHORTAGE_REGISTERS_DIR", BASE_DIR / "Material Shortage Registers")).resolve()
VENDOR_REGISTERS_DIR = Path(os.environ.get("VENDOR_REGISTERS_DIR", BASE_DIR / "Vendor Registers")).resolve()
VENDOR_DATE_REGISTERS_DIR = Path(os.environ.get("VENDOR_DATE_REGISTERS_DIR", BASE_DIR / "Vendor Date Registers")).resolve()
SERVICES_DB = Path(os.environ.get("ABB_SERVICES_DB", DATA_ROOT / "services.db")).resolve()
RAW_DIR = Path(os.environ.get("ABB_RAW_DIR", DATA_ROOT / "raw")).resolve()
GLOBAL_DB = Path(os.environ.get("ABB_GLOBAL_DB", DATA_ROOT / "global.db")).resolve()
ZPP7_DB = Path(os.environ.get("ABB_ZPP7_DB", DATA_ROOT / "zpp7_breakdown.db")).resolve()

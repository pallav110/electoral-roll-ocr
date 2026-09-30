import os
from pathlib import Path


DATABASE_URL = os.getenv("DATABASE_URL", "postgresql+psycopg://electoral:electoral@localhost:5432/electoral")
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
DOCUMENT_ROOT = Path(os.getenv("DOCUMENT_ROOT", "sample-pdfs")).resolve()
EXTRACTOR_MODE = os.getenv("EXTRACTOR_MODE", "mock")
EXTRACTOR_URL = os.getenv("EXTRACTOR_URL", "http://localhost:8001/extract")
EXTRACTOR_API_KEY = os.getenv("EXTRACTOR_API_KEY", "")
EXTRACTOR_TIMEOUT_SECONDS = int(os.getenv("EXTRACTOR_TIMEOUT_SECONDS", "120"))
SCHEMA_VERSION = os.getenv("EXTRACTION_SCHEMA_VERSION", "electoral_v1")
PAGES_PER_UNIT = max(1, int(os.getenv("PAGES_PER_UNIT", "10")))
MAX_UNIT_ATTEMPTS = max(1, int(os.getenv("MAX_UNIT_ATTEMPTS", "3")))
UNIT_STALE_SECONDS = int(os.getenv("UNIT_STALE_SECONDS", "600"))
DOCUMENT_STALE_SECONDS = int(os.getenv("DOCUMENT_STALE_SECONDS", "600"))
RETRY_BASE_SECONDS = int(os.getenv("RETRY_BASE_SECONDS", "30"))
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "change-me-before-deployment")

import os
import re
import sys
from pathlib import Path
from urllib.parse import urlparse


DATABASE_URL = os.getenv("DATABASE_URL", "postgresql+psycopg://electoral:electoral@localhost:5432/electoral")
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")

# The fallback scan root. It is NOT the only one any more: app/workflow.py
# walks every enabled row in the scan_roots table as well, and only falls
# back to this when that table is empty. It stays a real directory (not a
# bare string) because the tests that run outside a container rely on it
# resolving against the working directory.
DOCUMENT_ROOT = Path(os.getenv("DOCUMENT_ROOT", "sample-pdfs")).resolve()
# The whole host filesystem is bind-mounted by docker-compose.yml so a folder
# path typed in the UI can be any path on the machine, not just one declared as
# a mount.
#
# The mapping differs per host OS, and BOTH sides have to agree or a path typed
# in the UI silently scans nothing:
#
#   Windows  "C:\Users\me\Rolls" -> /host/c/Users/me/Rolls
#            The drive letter becomes a path segment under the mount root,
#            because one fixed mount point can only ever represent one drive.
#            Compose mounts each drive separately (//c/Users -> /host/c/Users).
#
#   Linux    "/srv/rolls"        -> /host/srv/rolls
#            There are no drive letters, so the rule is uniform: the absolute
#            path is appended to the mount root. Mounting the host root at
#            /host is what makes "any path" work for any location, including
#            paths under /home, /mnt and /media.
#
# HOST_MOUNT_STYLE selects which rule _resolve_scan_path applies. It is set by
# the compose override for that host, so the code does not have to guess the OS
# it is running on -- the containers are always Linux, so runtime detection
# would report "linux" on a Windows host and pick the wrong rule.
HOST_MOUNT_ROOT = os.getenv("HOST_MOUNT_ROOT", "/host")
HOST_MOUNT_STYLE = os.getenv("HOST_MOUNT_STYLE", "drive")  # "drive" | "posix"

# The drive the Windows mount serves, kept for display so the UI can say which
# prefix it will rewrite to without duplicating the mapping. Unused on Linux.
HOST_MOUNT_DRIVE = os.getenv("HOST_MOUNT_DRIVE", "c")

# The full prefix a typed path is rewritten onto, for the UI hint only. On Linux
# there is no single drive, so the hint is the mount root itself.
HOST_MOUNT_PREFIX = (
    HOST_MOUNT_ROOT.rstrip("/")
    if HOST_MOUNT_STYLE == "posix"
    else f"{HOST_MOUNT_ROOT.rstrip('/')}/{HOST_MOUNT_DRIVE.lower()}"
)

# Where PDFs from unmapped paths are copied so they become discoverable.
# Must be a container path that IS mounted in docker-compose.yml.
INGEST_ROOT = Path(os.getenv("INGEST_ROOT", "/data/ingest")).resolve()

MAX_UPLOAD_FILE_MB = max(1, int(os.getenv("MAX_UPLOAD_FILE_MB", "100")))
MAX_UPLOAD_BATCH_MB = max(MAX_UPLOAD_FILE_MB, int(os.getenv("MAX_UPLOAD_BATCH_MB", "2000")))
# How many PDFs sit under one row of the /uploads batch table. This is a
# DISPLAY grouping, not a cap: an upload of any size is accepted and split
# into as many of these as it needs. There is deliberately no maximum-file
# count -- the whole point of the page is that you can see how many arrived.
UPLOAD_BATCH_SIZE = max(1, int(os.getenv("UPLOAD_BATCH_SIZE", "100")))
MINIO_ENABLED = os.getenv("MINIO_ENABLED", "false").strip().lower() in {"1", "true", "yes"}
MINIO_ENDPOINT = os.getenv("MINIO_ENDPOINT", "http://minio:9000")
MINIO_ACCESS_KEY = os.getenv("MINIO_ACCESS_KEY", "")
MINIO_SECRET_KEY = os.getenv("MINIO_SECRET_KEY", "")
MINIO_BUCKET = os.getenv("MINIO_BUCKET", "electoral-archives")
MINIO_DERIVED_ASSETS_BUCKET = os.getenv("MINIO_DERIVED_ASSETS_BUCKET", "electoral-derived-assets")
MINIO_PREFIX = os.getenv("MINIO_PREFIX", "incoming/").strip("/")
MINIO_REGION = os.getenv("MINIO_REGION", "us-east-1")
MINIO_DELETE_AFTER_INGEST = os.getenv("MINIO_DELETE_AFTER_INGEST", "false").strip().lower() in {"1", "true", "yes"}
# No size cap on ZIP archives or on what they expand to. Both limits were
# raised to a value no real archive reaches (9,999,999 MB is ~10 TB) purely to
# get them out of the way: the /uploads staging page already shows what
# arrived, and a size cap here only ever produced a "Failed" row whose error
# was the limit itself. Disk, not policy, is the real bound -- and it fails
# loudly on write, which is a better error than a refusal in advance.
MINIO_MAX_ARCHIVE_MB = max(1, int(os.getenv("MINIO_MAX_ARCHIVE_MB", "9999999")))
MINIO_MAX_EXTRACTED_MB = max(1, int(os.getenv("MINIO_MAX_EXTRACTED_MB", "9999999")))
MINIO_CONSOLE_PUBLIC_URL = os.getenv("MINIO_CONSOLE_PUBLIC_URL", "http://localhost:9001")
MINIO_CONSOLE_INTERNAL_URL = os.getenv("MINIO_CONSOLE_INTERNAL_URL", "http://minio:9001")

# Hosts the containers may use to reach MinIO. A public hostname here makes
# every S3 call leave the Docker network and hang.
_MINIO_INTERNAL_HOSTS = frozenset({"minio", "localhost", "127.0.0.1"})


def minio_config_errors(
    enabled: bool | None = None,
    endpoint: str | None = None,
    console_public: str | None = None,
    console_internal: str | None = None,
) -> list[str]:
    """Problems that would hang MinIO or send the browser to a closed port.

    An empty list means this process may serve. The defaults are the values
    already loaded from the environment, so callers can also test other
    addresses without changing the running process.
    """
    if enabled is None:
        enabled = MINIO_ENABLED
    if not enabled:
        return []
    endpoint = MINIO_ENDPOINT if endpoint is None else endpoint
    console_public = MINIO_CONSOLE_PUBLIC_URL if console_public is None else console_public
    console_internal = MINIO_CONSOLE_INTERNAL_URL if console_internal is None else console_internal
    problems: list[str] = []

    parsed = urlparse(endpoint)
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "http" or host not in _MINIO_INTERNAL_HOSTS:
        problems.append(
            f"MINIO_ENDPOINT is {endpoint!r}. Inside the containers it must be "
            "http://minio:9000. MinIO speaks plain HTTP on the Docker network; "
            "a public https address hangs the app."
        )

    internal = urlparse(console_internal)
    internal_host = (internal.hostname or "").lower()
    if internal.scheme != "http" or internal_host not in _MINIO_INTERNAL_HOSTS:
        problems.append(
            f"MINIO_CONSOLE_INTERNAL_URL is {console_internal!r}. The web "
            "container must reach the console at http://minio:9001."
        )

    public = urlparse(console_public)
    public_path = public.path or ""
    proxied = public_path == "/minio" or public_path.startswith("/minio/")
    if public.scheme == "https" and (public.port in {9000, 9001} or not proxied):
        problems.append(
            f"MINIO_CONSOLE_PUBLIC_URL is {console_public!r}. Browsers on the "
            "public site must use the proxied path, for example "
            "https://dex.sarthiconnect.com/minio. Port 9001 is plain HTTP and "
            "is not reachable through the public site."
        )
    return problems


def refuse_bad_minio_config(role: str) -> None:
    """Stop this process before it serves traffic with a MinIO URL that hangs."""
    problems = minio_config_errors()
    if not problems:
        return
    message = f"Refusing to start the {role} process.\n" + "\n".join(problems)
    print(message, file=sys.stderr)
    raise SystemExit(1)

EXTRACTOR_MODE = os.getenv("EXTRACTOR_MODE", "mock")
EXTRACTOR_URL = os.getenv("EXTRACTOR_URL", "http://localhost:8001/extract")
EXTRACTOR_API_KEY = os.getenv("EXTRACTOR_API_KEY", "")
EXTRACTOR_TIMEOUT_SECONDS = int(os.getenv("EXTRACTOR_TIMEOUT_SECONDS", "120"))

# OCR routing. Two stateless services read electoral rolls and they do not
# overlap: EXTRACTOR_URL reads image-scanned rolls, TEXT_EXTRACTOR_URL reads
# born-digital ones that carry a text layer. A scanned roll sent to the
# text-layer service returns no records at all, and vice versa, so every PDF
# is classified before it is sent -- see app/extractor.py::_resolve_ocr_url.
#
# Set OCR_ROUTER=off to send everything to EXTRACTOR_URL, which is the old
# single-service behaviour.
TEXT_EXTRACTOR_URL = os.getenv(
    "TEXT_EXTRACTOR_URL", "http://localhost:8083/ocr/extract")
SCANNED_EXTRACTOR_URL = os.getenv(
    "SCANNED_EXTRACTOR_URL", EXTRACTOR_URL)
OCR_ROUTER = os.getenv("OCR_ROUTER", "on").strip().lower() not in (
    "0", "off", "false", "no")

# Cover-page reader. Page 1 of every roll is read once, independently of the
# card OCR, for the printed header fields and the section-4 counts (male,
# female, third gender, total) that the extracted records are checked
# against. The endpoint lives on the scanned-roll OCR service; the default
# derives it from that service's URL so one setting moves both.
COVER_EXTRACTOR_URL = os.getenv(
    "COVER_EXTRACTOR_URL",
    re.sub(r"/ocr/extract/?$", "/ocr/cover", SCANNED_EXTRACTOR_URL)
    if SCANNED_EXTRACTOR_URL.rstrip("/").endswith("/ocr/extract")
    else SCANNED_EXTRACTOR_URL.rstrip("/").rsplit("/", 1)[0] + "/ocr/cover",
)
COVER_TIMEOUT_SECONDS = int(os.getenv("COVER_TIMEOUT_SECONDS", "180"))
# How many covers one sweep of the beat enqueues. The reader takes 5-10 s per
# cover on CPU, so this bounds how far ahead the queue can run.
COVER_SWEEP_BATCH = max(1, int(os.getenv("COVER_SWEEP_BATCH", "25")))
COVER_MAX_ATTEMPTS = max(1, int(os.getenv("COVER_MAX_ATTEMPTS", "3")))

# Polling-station locations are resolved from already-extracted cover metadata
# by a separate worker.  This must never sit on the voter OCR critical path.
LOCATION_GEOCODER_URL = os.getenv("LOCATION_GEOCODER_URL", "http://osm-geocoder:8080")
LOCATION_GEOCODER_TIMEOUT_SECONDS = max(3, int(os.getenv("LOCATION_GEOCODER_TIMEOUT_SECONDS", "20")))
LOCATION_SWEEP_BATCH = max(1, int(os.getenv("LOCATION_SWEEP_BATCH", "25")))
LOCATION_MAX_ATTEMPTS = max(1, int(os.getenv("LOCATION_MAX_ATTEMPTS", "3")))
# Empty means the official-section catalogue layer is disabled.  It is
# deliberately opt-in so existing OCR processing remains unchanged.
OFFICIAL_SECTION_CATALOG_PATH = os.getenv("OFFICIAL_SECTION_CATALOG_PATH", "").strip()
# Verify an uploaded roll against its public ECI source URL. It is off until
# explicitly enabled because verification makes one additional HTTP request per
# distinct PDF, never because it changes OCR behaviour.
ECI_SOURCE_VERIFY_ENABLED = os.getenv("ECI_SOURCE_VERIFY_ENABLED", "false").strip().lower() in {"1", "true", "yes"}
ECI_ROLL_BASE_URL = os.getenv("ECI_ROLL_BASE_URL", "https://voters.eci.gov.in").rstrip("/")
ECI_SOURCE_VERIFY_TIMEOUT_SECONDS = max(5, int(os.getenv("ECI_SOURCE_VERIFY_TIMEOUT_SECONDS", "45")))
SCHEMA_VERSION = os.getenv("EXTRACTION_SCHEMA_VERSION", "electoral_v1")
PAGES_PER_UNIT = max(1, int(os.getenv("PAGES_PER_UNIT", "10")))
MAX_UNIT_ATTEMPTS = max(1, int(os.getenv("MAX_UNIT_ATTEMPTS", "3")))
UNIT_STALE_SECONDS = int(os.getenv("UNIT_STALE_SECONDS", "600"))
DOCUMENT_STALE_SECONDS = int(os.getenv("DOCUMENT_STALE_SECONDS", "600"))
RETRY_BASE_SECONDS = int(os.getenv("RETRY_BASE_SECONDS", "30"))

# --- Bounded queueing -------------------------------------------------------
#
# Postgres is the queue. Redis is a delivery mechanism, and the two used to be
# confused: every task published to one FIFO that a single --concurrency=1 worker
# drained, so a 582-PDF backlog meant real OCR work sitting behind thousands of
# messages that were themselves doing nothing. The three settings below bound how
# much of that backlog is allowed to exist as broker messages at once.
#
# They clamp rather than raise, like every other numeric here. ADMIN_TOKEN raises
# because it guards a security control; these are performance knobs and a bad
# value should degrade rather than stop the pipeline.

# Units allowed to sit queued+processing at any moment. One is in the worker's
# hand, the rest are waiting behind it, and nothing further is published. Must be
# at least the OCR worker's concurrency or the worker idles between units.
OCR_MAX_INFLIGHT_UNITS = max(1, int(os.getenv("OCR_MAX_INFLIGHT_UNITS", "2")))

# Documents allowed queued+processing at once. Bounded so the /documents page
# shows work that is genuinely progressing rather than a hundred sessions all
# claiming "processing" while their units wait behind the window above.
DOCUMENT_MAX_INFLIGHT = max(1, int(os.getenv("DOCUMENT_MAX_INFLIGHT", "20")))

# How long a queued unit may sit before dispatch_units() treats its message as
# lost and reclaims it. A unit legitimately waits behind the one ahead of it for
# as long as that one runs -- up to EXTRACTOR_TIMEOUT_SECONDS -- so this is twice
# the extractor timeout, not a small number. It used to be a hardcoded 60 seconds
# in recover(), which meant every unit in any real backlog was republished every
# minute, doubling the queue on every beat.
QUEUED_RECOVERY_SECONDS = max(
    60, int(os.getenv("QUEUED_RECOVERY_SECONDS", str(EXTRACTOR_TIMEOUT_SECONDS * 2))))

# Document setup only counts PDF pages and creates database rows on the control
# worker. It does not call OCR, so it must not inherit the multi-hour unit
# recovery threshold. A lost process_document publish should recover promptly.
DOCUMENT_QUEUED_RECOVERY_SECONDS = max(
    60, int(os.getenv("DOCUMENT_QUEUED_RECOVERY_SECONDS", "120")))

# Redis redelivers an unacked message after its visibility timeout. Left at the
# one-hour default it would redeliver a unit still inside a 90-minute OCR call,
# and the duplicate burns a core until process_unit_job rejects it on the status
# check. Derived from the effective extractor timeout so it follows whichever
# value compose supplies -- note the service environment overrides EXTRACTOR_-
# TIMEOUT_SECONDS to 5400, which is what actually applies at runtime.
CELERY_VISIBILITY_TIMEOUT = max(
    3600, int(os.getenv("CELERY_VISIBILITY_TIMEOUT",
                         str(EXTRACTOR_TIMEOUT_SECONDS * 2 + 3600))))

# How many stranded units dispatch_units() may reclaim in one tick. The stranded
# set is often the entire backlog -- after a bad deploy it can be tens of
# thousands of rows -- and scanning all of them under SELECT FOR UPDATE, inside a
# transaction that runs every ten seconds, would make the recovery pass itself
# the outage. A large stranded set is therefore drained over several ticks, which
# is slower to clear and much cheaper to run.
RECLAIM_BATCH_SIZE = max(
    1, int(os.getenv("RECLAIM_BATCH_SIZE", str(max(OCR_MAX_INFLIGHT_UNITS * 50, 100)))))

if DOCUMENT_MAX_INFLIGHT * 3 < OCR_MAX_INFLIGHT_UNITS:
    import warnings

    warnings.warn(
        f"DOCUMENT_MAX_INFLIGHT={DOCUMENT_MAX_INFLIGHT} cannot feed "
        f"OCR_MAX_INFLIGHT_UNITS={OCR_MAX_INFLIGHT_UNITS}: at most "
        f"{DOCUMENT_MAX_INFLIGHT * 3} units can exist across those documents, so "
        "the OCR worker will idle waiting for a window that cannot fill. Raise "
        "DOCUMENT_MAX_INFLIGHT or lower OCR_MAX_INFLIGHT_UNITS.",
        RuntimeWarning,
        stacklevel=2,
    )

# The admin credential for every route carrying Depends(admin) -- which is all
# of them except /health.
#
# There is deliberately no default. This used to fall back to the literal
# 'change-me-before-deployment', which .env.example also ships: if the variable
# were ever absent from a deployment, every route authenticated against a
# password published in the repository. A weak default for a security control
# is worse than none, because it looks configured and survives a deploy.
#
# Refusing to start is the correct failure mode. A missing token is an operator
# error that should surface immediately and unmistakably at boot, not as a
# service that runs all week serving voter records behind a guessable password.
#
# ALLOW_INSECURE_DEFAULT_ADMIN is an explicit escape hatch for local runs that
# need to boot without a token. It is never set in docker-compose.yml, and
# setting it is itself a deliberate act rather than an omission.
_ADMIN_DEFAULT = "change-me-before-deployment"
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "")
if not ADMIN_TOKEN:
    if os.getenv("ALLOW_INSECURE_DEFAULT_ADMIN", "").strip().lower() in ("1", "true", "yes"):
        ADMIN_TOKEN = _ADMIN_DEFAULT
        import warnings

        warnings.warn(
            "ADMIN_TOKEN is unset; falling back to the published placeholder "
            "'change-me-before-deployment'. Every admin route is protected by a "
            "password that is in the public repository. Set ADMIN_TOKEN, or set "
            "ALLOW_INSECURE_DEFAULT_ADMIN=1 to silence this.",
            RuntimeWarning,
            stacklevel=2,
        )
    else:
        raise RuntimeError(
            "ADMIN_TOKEN is not set. Every route except /health requires it. "
            "Generate one with:\n"
            "    python -c \"import secrets; print(secrets.token_urlsafe(24))\"\n"
            "and put it in .env. To run deliberately without one (local only):\n"
            "    ALLOW_INSECURE_DEFAULT_ADMIN=1"
        )
elif ADMIN_TOKEN == _ADMIN_DEFAULT:
    raise RuntimeError(
        "ADMIN_TOKEN is still the published placeholder "
        "'change-me-before-deployment', which is in the public repository. "
        "Replace it in .env with a generated value."
    )

# Human access is session-based.  These values are deployment secrets and are
# intentionally separate from EXTRACTOR_API_KEY, which authenticates services.
SESSION_SECRET = os.getenv("SESSION_SECRET", "")
if len(SESSION_SECRET) < 32:
    raise RuntimeError("SESSION_SECRET must be set to a 32+ character random secret.")
SESSION_COOKIE_SECURE = os.getenv("SESSION_COOKIE_SECURE", "true").lower() in ("1", "true", "yes")
BOOTSTRAP_ADMIN_USERNAME = os.getenv("BOOTSTRAP_ADMIN_USERNAME", "admin").strip()
BOOTSTRAP_ADMIN_PASSWORD = os.getenv("BOOTSTRAP_ADMIN_PASSWORD", "")

# SigNoz is the logs-and-traces screen. The web app signs in with these and
# hands the browser a one-time token, so a person already signed in here does
# not get a second login form. Internal URL is how this container reaches
# SigNoz; public URL is what the browser opens.
SIGNOZ_INTERNAL_URL = os.getenv("SIGNOZ_INTERNAL_URL", "http://signoz-query-service:8080")
SIGNOZ_PUBLIC_URL = os.getenv("SIGNOZ_PUBLIC_URL", "http://127.0.0.1:3301")
# Observability is an operator tool, not part of the public electoral-data
# application.  Only requests arriving through these local/LAN hostnames may
# reveal or open it.  Keep the deployed public hostname out of this list.
SIGNOZ_LOCAL_HOSTS = frozenset(
    host.strip().lower()
    for host in os.getenv(
        "SIGNOZ_LOCAL_HOSTS", "localhost,127.0.0.1,::1,192.168.0.15"
    ).split(",")
    if host.strip()
)
SIGNOZ_ADMIN_EMAIL = os.getenv("SIGNOZ_ADMIN_EMAIL", "admin@electoral.local").strip()
# Same password as the electoral admin already stored in the database. SigNoz
# still asks for an email address, which is SIGNOZ_ADMIN_EMAIL above.
SIGNOZ_ADMIN_PASSWORD = os.getenv("SIGNOZ_ADMIN_PASSWORD", "") or os.getenv("BOOTSTRAP_ADMIN_PASSWORD", "")
SIGNOZ_ORG_NAME = os.getenv("SIGNOZ_ORG_NAME", "Electoral").strip() or "Electoral"

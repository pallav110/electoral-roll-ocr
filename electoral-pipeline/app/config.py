import os
from pathlib import Path


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

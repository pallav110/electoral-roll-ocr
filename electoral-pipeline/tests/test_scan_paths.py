"""How a folder path typed in the UI becomes a path the container opens.

The person typing and the code walking the tree run on different machines, so
_resolve_scan_path accepts both Windows and Linux folder paths. The rewrite
rule is selected by config.HOST_MOUNT_STYLE, which the per-OS compose file
sets -- it is never detected at runtime, because the containers are always
Linux and detection would answer "linux" on a Windows host.

Every conversion is pinned here, because a wrong rewrite is silent: the scan
reports "0 found" and looks like it worked.
"""
import sys

import pytest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from app import config  # noqa: E402
from app.workflow import _resolve_scan_path  # noqa: E402


@pytest.fixture
def as_windows(monkeypatch):
    """Resolve as the Windows deployment does."""
    monkeypatch.setattr(config, "HOST_MOUNT_STYLE", "drive")


@pytest.fixture
def as_linux(monkeypatch):
    """Resolve as the Linux deployment does."""
    monkeypatch.setattr(config, "HOST_MOUNT_STYLE", "posix")


def resolved(typed: str) -> str:
    """The resolved path as the string the database would hold.

    Compared as a string, not as a Path, because Path(expected) on a Windows
    host builds a WindowsPath whose __eq__ is separator- and case-insensitive.
    Asserting against that would let "\\host\\c\\Users" pass as equal to
    "/host/c/Users" -- the exact corruption these tests exist to prevent.
    """
    return str(_resolve_scan_path(typed))


# The two styles cannot both be the default, so the cases below declare which
# one they describe and ask for it. Running the suite with either
# HOST_MOUNT_STYLE set therefore tests the deployment it is configured for.
@pytest.mark.usefixtures("as_windows")
@pytest.mark.parametrize("typed,expected", [
    # The main case: a Windows path, both separators.
    (r"C:\Users\pallav\Rolls", "/host/c/Users/pallav/Rolls"),
    ("c:/Users/pallav/Rolls", "/host/c/Users/pallav/Rolls"),
    # The drive letter is its own path segment under the mount root, so a
    # second drive maps to a second segment rather than nesting under the
    # first. This is what lets one mount root stand for many drives.
    (r"D:\rolls", "/host/d/rolls"),
    # A trailing backslash on a folder is normal and must not survive as a
    # doubled separator. Written as a concatenation because a raw string
    # cannot end in a backslash.
    ("C:\\Users\\pallav\\Rolls\\", "/host/c/Users/pallav/Rolls"),
    # Already a container path -- returned untouched, NOT rewritten again.
    # Rewriting a resolved path would produce /host/c/host/c/... on the second
    # save, which is the classic idempotency bug in this kind of mapping.
    ("/host/c/Users/pallav/Rolls", "/host/c/Users/pallav/Rolls"),
    ("/data/pdfs", "/data/pdfs"),
    # Quotes come from drag-and-drop into the browser's path box.
    ('"C:\\Users\\pallav\\Rolls"', "/host/c/Users/pallav/Rolls"),
])
def test_windows_and_container_paths(typed, expected):
    assert resolved(typed) == expected


@pytest.mark.usefixtures("as_windows")
def test_resolution_is_idempotent():
    """Saving a path, then typing it back, must not nest the prefix."""
    once = resolved(r"C:\Users\pallav\Rolls")
    twice = resolved(once)
    assert once == twice
    assert once.count("/host/") == 1


@pytest.mark.usefixtures("as_linux")
def test_resolution_is_idempotent_on_linux():
    """The same guarantee under the POSIX rule, where it is easier to break.

    The "already under the mount root" guard has to run BEFORE the POSIX
    rewrite, not after. Checked in the wrong order, saving a resolved root
    would produce /host/host/srv/rolls, and the second scan of a folder would
    find nothing while the first had worked.
    """
    once = resolved("/srv/rolls")
    twice = resolved(once)
    assert once == twice == "/host/srv/rolls"
    assert once.count("/host/") == 1


@pytest.mark.usefixtures("as_linux")
@pytest.mark.parametrize("typed,expected", [
    # Any absolute path resolves, not just the ones someone thought to mount.
    # This is what "any path" has to mean on a host with no drive letters.
    ("/srv/rolls", "/host/srv/rolls"),
    ("/home/me/Desktop/xyz", "/host/home/me/Desktop/xyz"),
    ("/mnt/usb/rolls", "/host/mnt/usb/rolls"),
    # The host root itself must not become /host/host.
    ("/", "/host"),
    # A trailing slash is a folder, not a doubled separator.
    ("/srv/rolls/", "/host/srv/rolls"),
    # Container-internal paths are not host paths and must pass through
    # untouched, or the sample-pdfs mount becomes unreachable.
    ("/data/pdfs", "/data/pdfs"),
    ("/data/ingest", "/data/ingest"),
])
def test_linux_paths_resolve_under_the_mount_root(typed, expected):
    assert resolved(typed) == expected


@pytest.mark.usefixtures("as_linux")
def test_a_windows_path_on_a_linux_host_is_still_usable():
    """A path copied off a Windows machine, or typed by habit.

    There is no C: drive here, so this cannot resolve to anything real. It
    degrades to a plain relative miss under the fallback root -- not to a
    "/host/C:" segment, which would look like a real location and is not.
    """
    assert resolved(r"C:\Users\me\Rolls") == f"{config.DOCUMENT_ROOT.as_posix()}/C:/Users/me/Rolls"


@pytest.mark.usefixtures("as_linux")
def test_a_bare_name_on_linux_is_relative_to_the_fallback_root():
    """With no leading slash there is no host path to rewrite, so it is
    relative to the sample folder -- the same rule the Windows style uses."""
    assert resolved("rolls") == f"{config.DOCUMENT_ROOT.as_posix()}/rolls"


@pytest.mark.usefixtures("as_windows")
def test_the_result_is_always_posix_whatever_the_host():
    """The value written to the database is read by the Linux worker.

    Built with Path() on a Windows host it stringifies to backslashes and the
    worker cannot open it -- a failure that only appears after a restart, in
    a different container, on a different OS.
    """
    for typed in (r"C:\Users\pallav\Rolls", "/data/pdfs", "rolls"):
        assert "\\" not in resolved(typed), typed


@pytest.mark.usefixtures("as_windows")
def test_a_unc_path_is_not_rewritten():
    """\\\\server\\share is not under a drive letter and has no correct rewrite.

    Passing it through means it fails with a clear "does not exist" rather
    than being turned into a plausible-looking path that finds nothing.
    """
    assert resolved(r"\\server\share\rolls") == "//server/share/rolls"


@pytest.mark.usefixtures("as_windows")
def test_a_bare_name_is_relative_to_the_fallback_root():
    assert resolved("rolls") == f"{config.DOCUMENT_ROOT.as_posix()}/rolls"


@pytest.mark.usefixtures("as_windows")
def test_a_windows_path_is_rewritten_without_touching_the_disk():
    """The conversion is string work only.

    It runs on Linux inside the container, where Path.is_absolute() cannot
    distinguish "C:\\..." from a relative name and where a filesystem probe
    for the C: drive would be meaningless. Rewriting first and asking about
    the result is the only order that is correct in both places.
    """
    assert resolved(r"C:\Users\pallav\Rolls").startswith(config.HOST_MOUNT_ROOT)
    assert "\\" not in resolved(r"C:\Users\pallav\Rolls")


@pytest.mark.parametrize("blank", ["", "   ", '""', None])
def test_a_blank_path_is_refused_rather_than_scanned(blank):
    """An empty root must raise, not become the filesystem root.

    Scanning "/" would be the worst available outcome: it walks the entire
    container and registers every PDF in it.
    """
    with pytest.raises(ValueError):
        _resolve_scan_path(blank)
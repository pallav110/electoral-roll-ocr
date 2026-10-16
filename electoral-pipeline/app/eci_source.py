"""Verify that an uploaded roll is byte-identical to its public ECI source.

This is provenance only.  It never downloads data into the database, never
changes an OCR value and, importantly, does not bypass captcha or protected
ECI endpoints.  It uses the already-public direct PDF URL encoded by ECI in a
recognised EROLLGEN filename.
"""
from __future__ import annotations

import hashlib
import re
from functools import lru_cache
from pathlib import Path

import httpx

from app import config

_FILENAME = re.compile(
    r"^(?P<year>\d{4})-EROLLGEN-(?P<state>S\d+)-(?P<ac>\d+)-"
    r"SIR-(?P<kind>FinalRoll|DraftRoll)-Revision(?P<revision>\d+)-"
    r"(?P<language>[A-Z]+)-(?P<part>\d+)-WI\.pdf$",
    re.IGNORECASE,
)


def official_url_for_filename(filename: str) -> str | None:
    """Derive an ECI public PDF URL only for the known SIR naming scheme."""
    name = Path(filename).name
    match = _FILENAME.fullmatch(name)
    if not match:
        return None
    values = match.groupdict()
    roll_kind = "sir-finalroll" if values["kind"].casefold() == "finalroll" else "sir-draftroll"
    return (
        f"{config.ECI_ROLL_BASE_URL}/eroll/{values['year']}/"
        f"{values['state'].lower()}/{roll_kind}/{values['ac']}/{name}"
    )


@lru_cache(maxsize=2048)
def _local_hash(path_text: str, mtime_ns: int, size: int) -> str:
    del mtime_ns, size
    digest = hashlib.sha256()
    with open(path_text, "rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


@lru_cache(maxsize=2048)
def _verify(path_text: str, mtime_ns: int, size: int) -> dict[str, str]:
    path = Path(path_text)
    url = official_url_for_filename(path.name)
    if not url:
        return {"status": "not_applicable", "reason": "filename_not_recognised"}
    local_sha256 = _local_hash(path_text, mtime_ns, size)
    try:
        remote = hashlib.sha256()
        with httpx.stream(
            "GET", url,
            timeout=config.ECI_SOURCE_VERIFY_TIMEOUT_SECONDS,
            headers={"User-Agent": "Electoral-Roll-Provenance/1.0", "Referer": "https://voters.eci.gov.in/"},
            follow_redirects=True,
        ) as response:
            if response.status_code != 200:
                return {"status": "unverified", "reason": f"official_http_{response.status_code}", "url": url,
                        "local_sha256": local_sha256}
            if "pdf" not in response.headers.get("content-type", "").casefold():
                return {"status": "unverified", "reason": "official_response_not_pdf", "url": url,
                        "local_sha256": local_sha256}
            for block in response.iter_bytes():
                remote.update(block)
    except (httpx.HTTPError, OSError) as exc:
        return {"status": "unverified", "reason": f"official_request_failed:{type(exc).__name__}", "url": url,
                "local_sha256": local_sha256}
    remote_sha256 = remote.hexdigest()
    return {"status": "verified" if remote_sha256 == local_sha256 else "hash_mismatch",
            "url": url, "local_sha256": local_sha256, "official_sha256": remote_sha256}


def verify_uploaded_roll(path_text: str) -> dict[str, str]:
    """Return a non-throwing verification result, cached per unchanged file."""
    path = Path(path_text)
    try:
        stat = path.stat()
        return _verify(str(path), stat.st_mtime_ns, stat.st_size)
    except OSError as exc:
        return {"status": "unverified", "reason": f"local_file_failed:{type(exc).__name__}"}

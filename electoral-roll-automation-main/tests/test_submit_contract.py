"""Pin the recovered submit contract against the bundle it was read from.

The nine fields below were transcribed from the minified SPA, not from the API
docs, and the two disagree: the docs describe a form, the client sends a JSON
body with a hardcoded misKey and a captchaId that appears in no form field. So
the contract is asserted here rather than left as a comment.

Run:  .venv-test/bin/python tests/test_submit_contract.py
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from eci_downloader import MIS_KEY, EROLL_MEDIA_BASE, ECIDownloader as E  # noqa: E402

CHUNK = Path("/tmp/eci_chunks/6838.fe8807b3.chunk.js")

# The body the client builds, as recovered from the chunk.
CONTRACT_FIELDS = [
    "stateCd", "acNumber", "partNumberList", "districtCd",
    "captcha", "captchaId", "langCd", "publishedRollId", "misKey",
]


def check_bundle():
    """Every field must still be in the chunk we read them from.

    Guard against the recovery silently going stale: if someone re-downloads the
    SPA and the endpoint renames a field, this fails before a live run does.
    Skipped, not failed, when the chunk is absent -- /tmp does not survive a
    reboot and that is not a contract failure.
    """
    if not CHUNK.exists():
        print(f"skip  bundle not present at {CHUNK} (recovery source not cached)")
        return True
    src = CHUNK.read_text(encoding="utf-8", errors="replace")
    i = src.find("partNumberList")
    if i < 0:
        print("FAIL  'partNumberList' no longer appears in the chunk at all")
        return False
    # Wide enough to hold both the body literal and the `ut` construction that
    # feeds it -- `var ut=[];for(var vt in Ae)ut.push(Object.values(ye[Ae[vt]])[2])`
    # sits a little before the body that uses it.
    window = src[max(0, i - 900): i + 400]
    missing = [f for f in CONTRACT_FIELDS if f not in window]
    if missing:
        print(f"FAIL  fields absent from the submit-body window: {missing}")
        return False
    ok = True
    if MIS_KEY not in window:
        print(f"FAIL  misKey {MIS_KEY!r} not in the chunk window")
        ok = False
    else:
        print(f"ok    misKey {MIS_KEY!r} matches the chunk")
    # partNumberList must be built from col3, i.e. the part number.
    if "[2]" not in window or "Object.values" not in window:
        print("FAIL  partNumberList is no longer built from Object.values(...)[2]")
        ok = False
    else:
        print("ok    partNumberList built from Object.values(row)[2] (col3)")
    return ok


def check_constants():
    ok = True
    if EROLL_MEDIA_BASE != "https://voters.eci.gov.in":
        print(f"FAIL  EROLL_MEDIA_BASE={EROLL_MEDIA_BASE!r}")
        ok = False
    else:
        print("ok    EROLL_MEDIA_BASE is the SPA's own base")
    return ok


def check_part_numbers():
    """The parser must take partNumber from the API payload, not from names."""
    captured = [{"json": {"statusCode": 200, "payload": [
        {"partNumber": 1, "partName": "PART ONE"},
        {"partNumber": 12, "partName": "PART TWELVE"},
        {"partNumber": 7, "partName": "PART SEVEN"},
    ]}}]
    got = E._part_numbers_from_responses(captured)
    if got != ["1", "12", "7"]:
        print(f"FAIL  part numbers {got} != ['1', '12', '7']")
        return False
    print("ok    part numbers read as numbers, in payload order")
    # Names must not leak in: "PART ONE" would be an invalid submission.
    if any("PART" in n.upper() for n in got):
        print("FAIL  a part name leaked into partNumberList")
        return False
    print("ok    no part names in partNumberList")
    return True


def check_captcha_id():
    d = E(vision_api_key="x")
    d._captcha_responses = [{"json": {"payload": {"id": 42}}}]
    if d._fetch_captcha_id(None) != "42":
        print("FAIL  captcha id not read from the getCaptcha payload")
        return False
    print("ok    captchaId read from getCaptcha payload.id")
    # The newest response wins: a refresh issues a new id, and pairing a stale
    # id with a fresh image fails as a wrong code rather than as a stale id.
    d._captcha_responses = [{"json": {"payload": {"id": 42}}},
                            {"json": {"payload": {"id": 99}}}]
    if d._fetch_captcha_id(None) != "99":
        print("FAIL  captcha id did not come from the most recent response")
        return False
    print("ok    most recent captcha id wins")
    # A captcha response with no id must not yield a stale fallback.
    d._captcha_responses = [{"json": {"payload": {}}}]
    if d._fetch_captcha_id(None) is not None:
        print("FAIL  returned an id from a payload that has none")
        return False
    print("ok    no id available reports None rather than a stale value")
    return True


def check_captcha_gating():
    """The submit must not proceed without an id -- that was the 400."""
    src = (ROOT / "src" / "eci_downloader.py").read_text()
    i = src.find("generate-published-pdfs")
    window = src[max(0, i - 3000): i + 300]
    if "captcha_id" not in window:
        print("FAIL  submit block does not reference captcha_id")
        return False
    print("ok    submit block carries captcha_id")
    if "def _download_result" not in src:
        print("FAIL  no second-request fetch helper")
        return False
    print("ok    second-request fetch helper present")
    return True


def main():
    results = [check_constants(), check_part_numbers(), check_captcha_id(),
               check_captcha_gating(), check_bundle()]
    failed = results.count(False)
    print()
    if failed:
        print(f"{failed} of {len(results)} contract checks FAILED")
        return 1
    print(f"all {len(results)} contract checks pass")
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Check the captcha normaliser against every reply shape actually observed.

Each expectation below comes from a real model reply during live runs, not a
guess. Run:  .venv-test/bin/python tests/test_captcha_normalize.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from eci_downloader import ECIDownloader as E  # noqa: E402

# (raw reply, expected code or None, where the shape came from)
CASES = [
    ("The 6 alphanumeric characters in the image are: d, x, h, u, s,",
     None, "enumerated but only 5 chars - must NOT be guessed at"),
    ("d, x, h, u, s", None, "5 chars, same failure"),
    ("The captcha code in the image is 784mn.", None,
     "labelled but only 5 chars - abstain rather than submit a short code"),
    ("zgz76g", "zgz76g", "bare code"),
    ("gay2fk", "gay2fk", "bare code"),
    ("vCegkf", "vCegkf", "letters only, no digit"),
    ("mps4yt", "mps4yt", "bare code"),
    ("image", None, "model describing the image, not a code"),
    ("The code is: **aB3x9**", None, "markdown-wrapped but 5 chars"),
    ("The code is: **aB3x9Z**", "aB3x9Z", "markdown-wrapped, 6 chars"),
    ("12-34", None, "4 chars, too short"),
    ("abc123", "abc123", "mixed alnum"),
    ("", None, "empty"),
    ("The code is ab12, not cd34", None, "two 4-char codes, neither is 6 - abstain"),
    ("a, b, c, d, e, f", "abcdef", "enumerated 6 chars"),
    ("1, 2, 3, 4, 5, 6", "123456", "enumerated digits"),
    ("A. x B. y C. z D. w E. v F. u", None, "lettered enum with dots, no commas"),
    ("The code is ab12, and also cd34", None, "two 4-char codes"),
]


def main():
    failures = []
    for raw, expected, why in CASES:
        got = E._normalize_captcha(raw)
        ok = got == expected
        mark = "ok  " if ok else "FAIL"
        print(f"{mark} {got!r:10} expected {expected!r:10}  # {why}")
        if not ok:
            failures.append((raw, expected, got))

    print()
    if failures:
        print(f"{len(failures)} of {len(CASES)} failed:")
        for raw, expected, got in failures:
            print(f"  {raw!r}\n    expected {expected!r}, got {got!r}")
        return 1
    print(f"all {len(CASES)} cases pass")
    return 0


if __name__ == "__main__":
    sys.exit(main())
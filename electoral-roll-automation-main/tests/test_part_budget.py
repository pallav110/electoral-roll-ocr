"""Local checks for the part-selection budget and captcha normalisation.

Neither test touches the network: both are pure logic that previously shipped
broken, and the site being intermittently unavailable makes it expensive to
rediscover a logic bug through a live run.

Run: .venv-test/bin/python tests/test_part_budget.py
"""
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.eci_downloader import (  # noqa: E402
    ECIDownloader,
    MAX_PARTS_PER_DOWNLOAD,
    HEADLESS,
)


class FakeLocator:
    """Stand-in for a Playwright Locator, resolving just enough of the API."""

    def __init__(self, rows=None, n=1, text=""):
        self.rows = rows if rows is not None else []
        self.n = n
        self.text = text

    @property
    def first(self):
        return self

    def count(self):
        return self.n

    def locator(self, selector):
        # Only "tbody tr" yields rows; everything else resolves to one element.
        if "tr" in selector:
            return FakeLocator(rows=self.rows)
        return FakeLocator(n=1, text=self.text)

    def nth(self, index):
        return FakeLocator(n=1, text=self.text)

    def all(self):
        return self.rows

    def check(self, timeout=None):
        return None

    def uncheck(self, timeout=None):
        return None

    def is_checked(self):
        return False

    def inner_text(self):
        return self.text


class FakeRow:
    def __init__(self, index):
        self.index = index

    def locator(self, selector):
        return FakeLocator(text=f"Part {self.index}")


class FakePage:
    def __init__(self, nrows, table_present=True):
        self.table = FakeLocator(rows=[FakeRow(i) for i in range(nrows)],
                                n=1 if table_present else 0)

    def locator(self, selector):
        return self.table


class RefusingRow(FakeRow):
    """A row whose checkbox click raises, as the site's cap makes it do."""

    def locator(self, selector):
        loc = FakeLocator(text=f"Part {self.index}")

        if "checkbox" in selector:
            class Refuse(FakeLocator):
                def check(self, timeout=None):
                    raise TimeoutError("Maximum 10 Parts are allowd once at a time")
            return Refuse()
        return loc


def run_checks():
    d = ECIDownloader.__new__(ECIDownloader)
    failures = []

    def check(name, actual, expected):
        ok = actual == expected
        print(f"  [{'ok' if ok else 'FAIL'}] {name}: {actual!r}"
              + ("" if ok else f" (expected {expected!r})"))
        if not ok:
            failures.append(name)

    print("Part budget is spent once across the whole download:")
    for budget in (0, 1, 2, 5, 10):
        got = d._check_parts_on_current_page(FakePage(10), budget)
        check(f"budget={budget} on a 10-row page yields {budget}", len(got), budget)

    used = d._check_parts_on_current_page(FakePage(10), MAX_PARTS_PER_DOWNLOAD)
    leftover = d._check_parts_on_current_page(
        FakePage(10), MAX_PARTS_PER_DOWNLOAD - len(used))
    check("a second page after the cap is spent yields nothing", leftover, [])

    absent = d._check_parts_on_current_page(FakePage(0, table_present=False), 10)
    check("no parts table yields no parts", absent, [])

    print("\nThe site's own refusal is survived, not raised:")
    page = FakePage(10)
    page.table = FakeLocator(rows=[RefusingRow(i) for i in range(10)])
    got = d._check_parts_on_current_page(page, 10)
    check("a refusing checkbox stops the pass instead of raising", len(got), 0)

    print("\nCaptcha normalisation strips prose down to a code:")
    for raw, expected in [("ybw5tz", "ybw5tz"),
                           ("The code is: **aB3x9**", "aB3x9"),
                           ("12-34", "1234"),
                           ("ab", None),
                           ("", None)]:
        check(f"normalize({raw!r})", ECIDownloader._normalize_captcha(raw), expected)

    long_refusal = ECIDownloader._normalize_captcha("Sorry, I cannot help with that")
    check("a long refusal is not treated as a code",
          long_refusal is None or len(long_refusal) < 20, True)

    print(f"\nHEADLESS defaults to {HEADLESS}")
    print("-" * 46)
    if failures:
        print(f"{len(failures)} FAILED: {failures}")
        return 1
    print("all local checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(run_checks())

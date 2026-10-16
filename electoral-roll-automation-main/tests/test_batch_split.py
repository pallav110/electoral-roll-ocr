"""Checks that a whole parts table is split into site-sized batches.

The site serves at most MAX_PARTS_PER_DOWNLOAD parts per submission. The user's
own capture of NCT OF Delhi / 2025 / FINAL ROLL / Central / 23 shows ten rows a
page with arrows at the bottom-right, so a single combination is ~20 pages of
parts. Everything that decides WHAT gets submitted and in HOW MANY submissions
lives here, and none of it needs the network -- the site's 503s make it far too
expensive to rediscover a batching bug through a live run.

Run: .venv-test/bin/python tests/test_batch_split.py
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.eci_downloader import (  # noqa: E402
    ECIDownloader,
    MAX_PARTS_PER_DOWNLOAD,
)

FAILURES = []


def check(name, got, expected):
    if got == expected:
        print(f"ok    {name}: {got}")
    else:
        print(f"FAIL  {name}: {got!r} (expected {expected!r})")
        FAILURES.append(name)


class FakePage:
    """Enough of a page for the batch loop, with no table and no network.

    Every hook the loop calls is overridden below, so this only has to exist.
    """

    def locator(self, *_a, **_k):
        raise AssertionError("the batch loop should not touch the table here")

    def wait_for_load_state(self, *_a, **_k):
        pass

    def wait_for_timeout(self, *_a, **_k):
        pass


def batches_of(numbers):
    """Run the real batching loop over `numbers`, recording each submit."""
    d = ECIDownloader.__new__(ECIDownloader)
    d.downloaded_files = []

    submitted = []
    errors = []

    d._select_combination = lambda page, params: (list(numbers), len(numbers))
    d._collect_all_part_numbers = lambda page, expected: list(numbers)
    d._uncheck_all_parts = lambda page: submitted.append(("uncheck",))

    def fake_submit(page, params, part_ids, batch_no, labels):
        submitted.append((batch_no, tuple(part_ids)))
        return True, f"bundle_{batch_no}.zip"

    d._submit_batch = fake_submit

    import src.eci_downloader as ed
    real_sleep = ed.time.sleep
    ed.time.sleep = lambda *_a, **_k: None
    try:
        ok, result = d.download_all_parts_for_combination(
            FakePage(),
            {"state": "U05", "year": "2025", "rollType": "FINAL ROLL 2025",
             "district": "Central", "constituency": "23 - KAROL BAGH",
             "language": "ENGLISH"},
            "NCT OF DELHI", "2025", "FINAL ROLL 2025", "Central",
            "23 - KAROL BAGH", "ENGLISH")
    finally:
        ed.time.sleep = real_sleep

    return ok, result, submitted, errors


print("-" * 70)
print("batch splitting")
print("-" * 70)

# The user's case: ten rows a page, ~20 pages behind the arrows.
ok, result, submitted, _ = batches_of([str(n) for n in range(1, 201)])
check("200 parts succeed", ok, True)
batches = [b for b in submitted if b[0] != "uncheck"]
check("200 parts -> 20 submissions", len(batches), 20)
check("every batch is at most 10 parts",
      max(len(p) for _, p in batches), MAX_PARTS_PER_DOWNLOAD)
check("no submission is empty", min(len(p) for _, p in batches), 10)
check("batch numbers run 1..20", [b[0] for b in batches], list(range(1, 21)))
check("one bundle returned per batch", len(result), 20)
# Cleared before EVERY batch, the first included: the table arrives with
# whatever the previous combination left on it, so a first batch that trusted a
# clean table would carry a stale selection into its own submit.
check("selections cleared before each batch",
      submitted.count(("uncheck",)), 20)

# Every part submitted exactly once, in order, nothing dropped or duplicated.
flat = [n for _, parts in batches for n in parts]
check("all 200 parts submitted", len(flat), 200)
check("no part submitted twice", len(set(flat)), 200)
check("parts submitted in table order", flat, [str(n) for n in range(1, 201)])

# The boundary cases. A combination of exactly N parts must make exactly
# ceil(N/10) submissions -- one short batch is a silent under-download.
for n, want in ((1, 1), (9, 1), (10, 1), (11, 2), (20, 2), (21, 3), (173, 18)):
    ok, result, submitted, _ = batches_of([str(i) for i in range(1, n + 1)])
    batches = [b for b in submitted if b[0] != "uncheck"]
    check(f"{n} parts -> {want} submission(s)", len(batches), want)
    check(f"{n} parts all submitted",
          len([x for _, p in batches for x in p]), n)
    check(f"{n} parts bundles returned", len(result), want)

# Zero parts must fail loudly rather than reporting an empty success.
d = ECIDownloader.__new__(ECIDownloader)
d.downloaded_files = []
d._select_combination = lambda page, params: ([], 0)
d._collect_all_part_numbers = lambda page, expected: []
ok, result = d.download_all_parts_for_combination(
    FakePage(), {}, "S", "2025", "R", "D", "C", "L")
check("zero parts fails", ok, False)
check("zero parts names the cause", result, "No parts found")

# A dropdown that cannot be resolved must surface ITS reason. _select_combination
# returns (None, reason) on failure, and the caller used to unpack that as
# (numbers, total) -- `len("District not found")` then made the run die on
# `str / int`, naming arithmetic instead of the missing dropdown.
for reason in ("District not found: Central",
               "Roll type not found: FINAL ROLL 2025",
               "Language not found: ENGLISH",
               "Parts table not found"):
    d = ECIDownloader.__new__(ECIDownloader)
    d.downloaded_files = []
    d._select_combination = lambda page, params, r=reason: (None, r)

    def must_not_run(*_a, **_k):
        raise AssertionError("no batch may run when selection failed")

    d._collect_all_part_numbers = must_not_run
    d._submit_batch = must_not_run
    ok, result = d.download_all_parts_for_combination(
        FakePage(), {}, "S", "2025", "R", "D", "C", "L")
    check(f"failed selection fails: {reason[:24]}", ok, False)
    check(f"failed selection reports its reason: {reason[:24]}",
          result, reason)

# The fallback the live run needed. get-publish-part-list returned nothing while
# the table rendered ten rows, so the count came back 0 and the whole
# combination failed with "No parts found" -- the table walk was dead code,
# because the count that gated it was itself derived from the API list.
class _RowLoc:
    def __init__(self, number):
        self.number = number

    def nth(self, _i):
        return self

    def count(self):
        return 1

    def inner_text(self):
        return f"{self.number} - PART NAME"

    def check(self, **_k):
        pass

    def is_checked(self):
        return False

    def uncheck(self, **_k):
        pass


class _Row:
    def __init__(self, number):
        self.number = number

    def locator(self, _sel):
        return _RowLoc(self.number)


class PagedTable:
    """A parts table of `pages` pages of `per_page` rows, with the arrows."""

    def __init__(self, pages, per_page=10):
        self.pages, self.per_page, self.page_no = pages, per_page, 0
        self.next_clicks = 0
        self.first_clicks = 0

    def locator(self, selector="", *_a, **_k):
        return _TableLoc(self, controls="control-btn" in selector)

    def rows_here(self):
        start = self.page_no * self.per_page
        end = min(start + self.per_page, self.pages * self.per_page)
        return [str(n) for n in range(start + 1, end + 1)]

    def wait_for_load_state(self, *_a, **_k):
        pass

    def wait_for_selector(self, *_a, **_k):
        pass


class _Btn:
    """One pagination control: its label, whether it is live, what it does."""

    def __init__(self, tbl, label, enabled):
        self.tbl, self.label, self.enabled = tbl, label, enabled

    def inner_text(self):
        return self.label

    def is_enabled(self):
        return self.enabled

    def click(self):
        if not self.enabled:
            raise AssertionError(f"clicked a disabled '{self.label}'")
        if self.label == ">>":
            self.tbl.page_no = self.tbl.pages - 1
        elif self.label == "<<":
            self.tbl.page_no = 0
        elif self.label == "<":
            self.tbl.page_no -= 1
        else:
            self.tbl.page_no += 1
            self.tbl.next_clicks += 1


class _TableLoc:
    """Mimics the Playwright chain: locator() -> .first -> the element.

    `first` is a PROPERTY here, not a method: the production code writes
    `.locator(...).first` with no parentheses, which is how Playwright exposes
    it. A fake that defines it as a method returns the bound method itself, and
    the next `.count()` fails on a function -- which is exactly what happened.
    """

    def __init__(self, tbl, first=False, controls=False):
        self.tbl, self.is_first, self.controls = tbl, first, controls

    @property
    def first(self):
        return _TableLoc(self.tbl, first=True)

    def count(self):
        return 1

    def locator(self, _sel):
        return self

    def nth(self, _i):
        return self

    def all(self):
        if self.controls:
            back = self.tbl.page_no > 0
            fwd = self.tbl.page_no < self.tbl.pages - 1
            return [_Btn(self.tbl, "<<", back), _Btn(self.tbl, "<", back),
                    _Btn(self.tbl, ">", fwd), _Btn(self.tbl, ">>", fwd)]
        if not self.is_first:
            raise AssertionError("rows were read without .first")
        return [_Row(n) for n in self.tbl.rows_here()]

    def inner_text(self):
        return ", ".join(self.tbl.rows_here())

    def is_enabled(self):
        return True

    def click(self):
        self.tbl.page_no = 0

    def wait_for_selector(self, *_a, **_k):
        pass

    def wait_for_load_state(self, *_a, **_k):
        pass


def walk(pages, per_page=10):
    """Run the real forward table walk over a fake paginated table."""
    d = ECIDownloader.__new__(ECIDownloader)
    tbl = PagedTable(pages, per_page)
    import src.eci_downloader as ed
    real_sleep = ed.time.sleep
    ed.time.sleep = lambda *_a, **_k: None
    try:
        nums = d._collect_all_part_numbers_from_table(tbl)
    finally:
        ed.time.sleep = real_sleep
    return nums, tbl


check("walk finds only page 1's ten rows", len(walk(1)[0]), 10)
check("walk finds 20 rows over 2 pages", len(walk(2)[0]), 20)
nums, tbl = walk(20)
check("walk finds 200 rows over 20 pages", len(nums), 200)
check("walk found every row exactly once", len(set(nums)), 200)
check("walk found them in ascending order", nums,
      [str(n) for n in range(1, 201)])
check("walk clicked next once per page boundary", tbl.next_clicks, 19)

print("-" * 70)
if FAILURES:
    print(f"\n{len(FAILURES)} FAILED: {FAILURES}")
    sys.exit(1)
print("\nall batch-splitting checks pass")

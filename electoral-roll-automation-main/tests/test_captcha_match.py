"""Checks for the captcha fixes: label matching and the vote rule.

Both were live failures. The site's dropdown says `Final Roll - 2025` while the
requested label is `FINAL ROLL 2025`, and the old exact-match comparison meant a
published roll reported "not found". The vote rule discarded a verified-correct
read because it wanted a fixed 5 of 12 views instead of simply beating the
runner-up.

No network: every case here is a string or a tally, and the site's 503s make it
far too expensive to rediscover either through a live run.

Run: PYTHONPATH="$HOME/.local/lib/python3.12/site-packages:." python3 tests/test_captcha_match.py
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.eci_downloader import ECIDownloader  # noqa: E402

FAILURES = []
M = ECIDownloader._labels_match


def check(name, got, expected):
    if got == expected:
        print(f"ok    {name}: {got}")
    else:
        print(f"FAIL  {name}: {got!r} (expected {expected!r})")
        FAILURES.append(name)


print("-" * 70)
print("dropdown label matching")
print("-" * 70)

# The exact live failure: requested 'FINAL ROLL 2025', site offers this.
check("FINAL ROLL 2025 matches the site's Final Roll - 2025",
      M("Final Roll - 2025", "FINAL ROLL 2025"), True)
check("and matches 'Final Roll - 2025' the other way round",
      M("FINAL ROLL 2025", "Final Roll - 2025"), True)
check("but DraftRoll is not a Final Roll",
      M("DraftRoll - 2025", "FINAL ROLL 2025"), False)
# NOT forgiven: `DraftRoll` is one word on the site, so it stays one word here.
# Forgiving that would make a draft roll satisfy a request for the final roll,
# which is the worst kind of match to get wrong -- both are published every
# year and both appear in the same dropdown.
check("a draft roll does not satisfy DRAFT ROLL 2025",
      M("DraftRoll - 2025", "DRAFT ROLL 2025"), False)

# Case, padding, separator and spacing must all be forgiven.
for cand in ("final roll - 2025", "  Final Roll - 2025  ", "Final_Roll_2025",
             "Final  Roll  2025", "FINAL ROLL - 2025"):
    check(f"tolerates {cand!r}", M(cand, "FINAL ROLL 2025"), True)

# A year is a real difference. Last year's roll must not satisfy this year's
# request -- matching loosely enough to download the wrong roll would be worse
# than the "not found" this replaced.
check("Final Roll - 2024 rejected for a 2025 request",
      M("Final Roll - 2024", "FINAL ROLL 2025"), False)
check("Final Roll - 2026 rejected for a 2025 request",
      M("Final Roll - 2026", "FINAL ROLL 2025"), False)

# Non-roll labels: districts and languages keep working.
check("district Central matches", M("Central", "Central"), True)
check("district case forgiven", M("CENTRAL", "central"), True)
check("district 05 is not Central", M("North East Delhi", "Central"), False)
check("language matches", M("ENGLISH", "English"), True)

# DIRECTION MATTERS. A longer candidate may satisfy a shorter request, but a
# shorter candidate may not satisfy a longer one -- `Central North` is a
# different district from `Central`, and both sit in the same dropdown. This
# was a live failure: asking for Central selected Central North and came back
# with ACs 14-18 instead of 19-23.
# NOTE the argument order: M(site_option, requested) -- exactly how
# _find_option_value calls it.
check("Central does not swallow Central North",
      M("Central North", "Central"), False)
check("asking for Central North gets Central North",
      M("Central North", "Central North"), True)
check("nor does North West satisfy a request for North",
      M("North West", "North"), False)
check("asking for West does not get West Delhi",
      M("West", "West Delhi"), False)

# Constituencies are requested by number and offered by number and name, so a
# whole-word prefix has to match -- but only on whole words.
check("constituency number matches its labelled option",
      M("23 - KAROL BAGH", "23"), True)
check("prefix must end on a word boundary",
      M("123 - KAROL BAGH", "23"), False)
# Name-only requests are NOT supported: the number is the only stable part of a
# constituency label, and the names change between rolls. Matching on a name
# would break the moment the commission renames one.
check("a name-only constituency request does not match",
      M("23 - KAROL BAGH", "KAROL BAGH"), False)

# Placeholders and empties must never match anything.
for cand in ("", "   ", None):
    check(f"empty candidate {cand!r} never matches", M(cand, "Central"), False)
check("empty wanted never matches", M("Central", ""), False)
check("placeholder rejected", M("Select Roll", "FINAL ROLL 2025"), False)
check("placeholder state rejected", M("Select State", "NCT OF DELHI"), False)

print("-" * 70)
print("captcha vote rule")
print("-" * 70)


def winner(votes):
    """Reproduce the decision the solver makes, returning the accepted code.

    The real method prints and talks to the vision API, so the arithmetic is
    mirrored here rather than driven through the network. What is under test is
    the rule, and the rule is three lines of it.
    """
    if not votes:
        return "MANUAL"
    ranked = sorted(votes.values(), reverse=True)
    best = max(votes, key=lambda c: votes[c])
    runner_up = ranked[1] if len(ranked) > 1 else 0
    if votes[best] <= runner_up:
        return "MANUAL"
    return best


# The measured live tally, ground truth 'zduem4' read by a human. The correct
# code won by exactly one vote and the old fixed floor of 5 threw it away --
# which cost the run a full batch.
live = {"zduem4": 4, "zduemA": 3, "zdue4m": 2, "d11emt": 1}
check("live tally accepts the verified-correct code", winner(live), "zduem4")

# A confident read still passes comfortably.
check("8/12 agreement accepted", winner({"3g6tp2": 8, "396tp2": 1}), "3g6tp2")
check("12/12 unanimity accepted", winner({"abc123": 12}), "abc123")

# A three-way split must NOT pass: that is a confused read, and the old
# 'majority' wording would have let a dict-order accident decide it.
check("4-4-4 split rejected", winner({"aaaaaa": 4, "bbbbbb": 4, "cccccc": 4}),
      "MANUAL")
check("2-2-2-2 split rejected",
      winner({"aaaaaa": 2, "bbbbbb": 2, "cccccc": 2, "dddddd": 2}), "MANUAL")
check("two-way tie rejected", winner({"aaaaaa": 3, "bbbbbb": 3}), "MANUAL")

# A winner by one vote is accepted; by zero is not. That single boundary is
# the whole difference between this rule and the old fixed floor.
check("one clear vote ahead accepted", winner({"aaaaaa": 3, "bbbbbb": 2}),
      "aaaaaa")
check("single read is a clear winner over zero", winner({"aaaaaa": 1}),
      "aaaaaa")
check("no votes at all rejected", winner({}), "MANUAL")

# Correlated noise is the failure this guards: every view reading the same
# wrong string still passes, because it SHOULD -- the answer then comes back
# from the server as a rejection. What must never pass is indecision.
check("8 wrong-but-unanimous still submitted",
      winner({"f4em4.": 8, "zdem4": 1}), "f4em4.")

print("-" * 70)
if FAILURES:
    print(f"\n{len(FAILURES)} FAILED: {FAILURES}")
    sys.exit(1)
print("\nall captcha-matching checks pass")
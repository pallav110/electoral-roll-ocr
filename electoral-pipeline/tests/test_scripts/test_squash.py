"""The spelling key used to accept Layer 1 entries.

squash() collapses the two differences that are conventions rather than
errors: a doubled letter, and the ee/ii vowel. Its ordering was wrong for a
while and nothing failed, because the Levenshtein fallback below it happened to
rescue every real case. That is the worst kind of bug -- the safety net hides
the broken net -- so the ordering is pinned here directly.

Collapsing doubled letters FIRST consumes the "ee" in "Sandeep" as a repeated
character, so the vowel fold never sees it:

    Sanjiv  ->  sanjiv      (no doubled letter, nothing to collapse)
    Sanjeev ->  sanjev      ("ee" already eaten by the doubling pass)

...and the two never meet, even though recognising them as the same pair is
what the key is for.

Note what the key does NOT do on its own: merging a pair does not accept it.
looks_like_a_spelling still refuses any model output longer than the rules, so
Sanjiv/Sanjeev is merged by the key and then rejected by Filter 3. The two are
separate decisions, and these tests keep them separate.
"""
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "tests" / "test_scripts"))

from build_layer1_map import looks_like_a_spelling, squash  # noqa: E402


@pytest.mark.parametrize("left,right", [
    # The long-vowel convention. These are the pairs the key exists for.
    ("Sanjiv", "Sanjeev"),
    ("Sandip", "Sandeep"),
    ("Pradip", "Pradeep"),
    ("Nitu", "Neetu"),
    ("Niraj", "Neeraj"),
    ("Gita", "Geeta"),
])
def test_long_vowel_pairs_meet(left, right):
    assert squash(left) == squash(right)


@pytest.mark.parametrize("value,expected", [
    # Trace the fix explicitly, so the ordering is documented by the test.
    ("Sandeep", "sandip"),
    ("Sandip", "sandip"),
    ("Sanjiv", "sanjiv"),
    ("Sanjeev", "sanjiv"),
    # Doubled letters collapse, including a genuine triple.
    ("Ram", "ram"),
    ("Ramm", "ram"),
    ("Bhaaggg", "bhag"),
    # Punctuation and case are not part of the key.
    ("V.K. Tomar", "vktomar"),
    ("V.K.Tomar", "vktomar"),
])
def test_squash_values(value, expected):
    assert squash(value) == expected


def test_a_distinct_name_is_not_pulled_onto_a_neighbour_key():
    """Folding ee->i is one-way, so the two spellings of a long vowel land on
    different keys rather than merging: Bineesh -> binish, Binesh -> binesh.

    Asserted because it is the asymmetry that makes phonetic-key retrieval
    unsafe on this corpus -- a retrieval layer keyed on squash() would not
    connect them, and the same one-way fold means any key built from it cannot
    distinguish "the model lengthened this vowel" from "this name was always
    spelled long".
    """
    assert squash("Bineesh") != squash("Binesh")
    assert (squash("Bineesh"), squash("Binesh")) == ("binish", "binesh")


@pytest.mark.parametrize("rules,model", [
    # Merged by the key, then correctly refused by Filter 3: a longer model
    # output means it invented a vowel. This is the Shiv/Shiva rule.
    ("Sanjiv", "Sanjeev"),
    ("Sandip", "Sandeep"),
    ("Nitu", "Neetu"),
])
def test_merging_does_not_bypass_the_length_rule(rules, model):
    """The key is not a way around Filter 3."""
    assert squash(rules) == squash(model)
    assert not looks_like_a_spelling(rules, model)


@pytest.mark.parametrize("rules,model", [
    # Genuinely accepted: shorter or equal, same shape, same name.
    ("Pala", "Pal"),
    ("Rama", "Ram"),
    ("Varma", "Verma"),
])
def test_real_entries_are_still_accepted(rules, model):
    assert looks_like_a_spelling(rules, model)


@pytest.mark.parametrize("rules,model", [
    # Still rejected: different words entirely.
    ("Chandra", "the moon"),
    ("Prince", "Rajakumar"),
    ("Hero", "Veer"),
])
def test_key_does_not_admit_meaning_drift(rules, model):
    assert not looks_like_a_spelling(rules, model)
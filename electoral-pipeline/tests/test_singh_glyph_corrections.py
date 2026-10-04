"""Regression net for the सिंह glyph corrections added on 2026-10-03.

The full-roll run (531 records) found exactly two misreads of सिंह, both in
voter_husband_last_name:

    record  29:  कुसुम     / पति रणबीर सिंड
    record 482:  मंजू देवी  / पति जगत सिंय

Both bad tokens were checked against the verified ground-truth corpus before
being added (सिंह = 28, सिंड = 0, सिंय = 0), so neither rule can rewrite a name
a human confirmed. This test pins that reasoning.

Note on where the rules live: the built-in table is _OCR_NAME_VARIANTS, not
NAME_TOKEN_CORRECTIONS. NAME_TOKEN_CORRECTIONS is populated from the
OCR_NAME_TOKEN_CORRECTIONS_JSON env var and is empty by default; line 388
canonicalises both. Reading the wrong dict here would silently test nothing.
"""
import ast
import json
import re
import unicodedata
from collections import Counter
from pathlib import Path

import pytest

OCR_PY = Path(__file__).resolve().parents[1] / "ocr_pdf_api.py"
GT_GLOB = "ground_truth/whole_pdf_6-8_results.json"


def _literal_dict(name: str) -> dict:
    """Read a module-level dict literal by parsing the file.

    The file cannot simply be imported on a test runner -- it pulls in
    tesseract and paddle. ast lets Python's own tokenizer handle comments and
    nesting, which a hand-rolled brace scan plus regex comment-stripping does
    not.
    """
    tree = ast.parse(OCR_PY.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            if node.target.id == name:
                return ast.literal_eval(node.value)
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id == name:
                    return ast.literal_eval(node.value)
    raise AssertionError("%s not found in %s" % (name, OCR_PY))


def _harvest(obj, acc: Counter) -> None:
    if isinstance(obj, str):
        if any("ऀ" <= c <= "ॿ" for c in obj):
            for tok in re.split(r"\s+", obj):
                tok = tok.strip(" ,.:;।|")
                if tok:
                    acc[tok] += 1
    elif isinstance(obj, dict):
        for v in obj.values():
            _harvest(v, acc)
    elif isinstance(obj, list):
        for v in obj:
            _harvest(v, acc)


@pytest.fixture(scope="module")
def built_in_rules() -> dict:
    return _literal_dict("_OCR_NAME_VARIANTS")


@pytest.fixture(scope="module")
def by_page_rules() -> dict:
    return _literal_dict("_OCR_NAME_VARIANTS_BY_PAGE")


@pytest.fixture(scope="module")
def ground_truth_tokens() -> Counter:
    files = list(OCR_PY.parent.glob(GT_GLOB))
    if not files:
        pytest.skip("verified ground-truth corpus not present")
    acc: Counter = Counter()
    for p in files:
        with open(p, encoding="utf-8") as fh:
            _harvest(json.load(fh), acc)
    return acc


@pytest.mark.parametrize("bad,good", [("सिंड", "सिंह"), ("सिंय", "सिंह")])
def test_singh_glyph_rules_present(built_in_rules, bad, good):
    assert built_in_rules.get(bad) == good


@pytest.mark.parametrize("bad", ["सिंड", "सिंय"])
def test_bad_token_absent_from_ground_truth(built_in_rules, ground_truth_tokens, bad):
    """The shadowing guard: a rule must not rewrite a name a human verified."""
    assert built_in_rules.get(bad), "rule under test is missing"
    assert ground_truth_tokens.get(bad, 0) == 0, (
        "%r appears in the verified ground truth; a global rewrite would "
        "corrupt a correct record" % bad)


def test_canonical_form_is_the_established_one(ground_truth_tokens):
    """The corrections point at the real surname, not at a rarer variant."""
    assert ground_truth_tokens.get("सिंह", 0) > 0
    for variant in ("सिंध", "सिंद"):
        assert ground_truth_tokens.get(variant, 0) == 0


def test_no_builtin_rule_shadows_a_verified_name(built_in_rules, by_page_rules,
                                                ground_truth_tokens):
    """General invariant: no built-in rule may map a ground-truth name onto a
    different string. The new entries must not break this."""
    offenders = [
        (bad, good) for bad, good in built_in_rules.items()
        if bad in ground_truth_tokens and bad != good
    ]
    for page_map in by_page_rules.values():
        offenders += [
            (bad, good) for bad, good in page_map.items()
            if bad in ground_truth_tokens and bad != good
        ]
    assert not offenders, "rules would rewrite verified names: %r" % (offenders,)


def test_canon_is_idempotent_for_singh(built_in_rules):
    """_canon_deva reorders combining marks inside a cluster. The two new keys
    carry no nukta, so they must pass through unchanged -- otherwise the table
    key would never match the canonicalised incoming name at the lookup.

    This deliberately does NOT cover the older "सिंद्" rule: its trailing
    virama makes it a different cluster, and _canon_deva leaves it alone,
    which is correct because सिंद् is meant to match that exact key.
    """
    src = OCR_PY.read_text(encoding="utf-8")
    # _canon_deva closes over unicodedata, which the exec'd function body
    # expects in its globals.
    ns = {"unicodedata": unicodedata}
    fn = next(n for n in ast.walk(ast.parse(src))
              if isinstance(n, ast.FunctionDef) and n.name == "_canon_deva")
    exec(compile(ast.Module(body=[fn], type_ignores=[]), "<f>", "exec"), ns)
    canon = ns["_canon_deva"]
    for bad in ("सिंड", "सिंय"):
        assert canon(bad) == bad, "%r changes under canonicalisation" % bad
        assert canon(bad) in built_in_rules

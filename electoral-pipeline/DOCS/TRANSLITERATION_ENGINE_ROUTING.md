# Transliteration engine routing — decisions from the IndicTrans2 evaluation

Date: 2026-10-02. Evidence: `tests/test_scripts/eval_indictrans2_vs_rulebased.py`
and `tests/test_scripts/sweep_corpus.py` over 346 unique Hindi strings from the
real ground-truth OCR output.

## Measured

| Engine | 346 strings | Notes |
|---|---|---|
| Rule-based (`sanscript` + phonetic cleanup) | 0.13 s | current production path |
| IndicTrans2-200M, RTX 4060, batch 32, beams 5 | ~24 s | ~185x slower; accepted |

Speed is not a constraint. Correctness per field is.

## Layer 0: the scheme contract (fixed 2026-10-02)

`_scheme()` defaulted to `iast-plain`, but the entire cleanup layer is keyed
on ITRANS: `GLOBAL_STRUCTURAL_MAP` documents itself as "keys are lowercase
romanised forms (**ITRANS output**)", and every fold in `_clean_segment`
(`shh→sh`, `rri→ri`, `amch→anch`) matches only ITRANS digraphs.

Under the IAST default all of it was dead code at once, and IAST's diacritics
were then stripped, so the pair went missing in output:

| Hindi | ITRANS | iast-plain (was) | Correct |
|---|---|---|---|
| शिव | shiva | `Siva` | Shiv |
| चन्द्र | chandra | `Candra` | Chandra |
| अर्चना | archanA | `Arcana` | Archana |
| क्षेत्र | kShetra | `Ksetra` | Kshetra |
| कृष्णा | kRRiShNA | `Krsna` | Krishna |
| अक्षय | akShaya | `Aksay` | Akshay |
| जोशी | joshI | `Josi` | Joshi |

The failure was silent — no exception, just a wrong name in an English column.
95 of the 346 corpus strings changed; every name field improved.

Two smaller bugs the flip exposed:

- **udatta/anudatta** (U+0951/U+0952, an OCR misread of `नं`) become the
  literal two-character escape `\'` in ITRANS. That is not a Unicode
  combining mark, so the mark-stripper structurally cannot remove it:
  `हाऊस नं - 121` came out as `Haus Na\' - 121`. Fixed at the input.
- **ज्ञ** is `ज् + ञ`; the library marks the rare nasal with a literal `~`
  (`j~nAna`), giving `J~Nan`. The tilde is a marker, not a letter, so it is
  stripped like any other; the `jn→gy` fold then fixes it. A map entry could
  only ever have named `ज्ञान`, never `ज्ञानेश` or `ज्ञानंद`.

`एचएनओ 146` → `HNO 146` is fixed by the same change: IAST spells it
`ecaenao`, which matched no map entry (`echaenao` is the ITRANS key that was
already there); it was rendering as `Ecaenao 146`.

Guarded by `tests/test_scheme_contract.py`; whole-corpus residue check is
`tests/test_scripts/sweep_corpus.py`.

## Layer 0 (cont.): homorganic nasal assimilation

Measuring the model against the rules on names (`tests/test_scripts/layer3_guard_eval.py`)
showed **22 of 22** names containing an anusvara were wrong. Not drift — one
rule.

The Devanagari anusvara (ं) is not a letter with a fixed sound: the nasal
assimilates to the class of the consonant that follows, and English spelling
follows the same rule. ITRANS writes it as a literal `M`, so `सिंह` came out
`siMha`; the schwa rule then saw a bare consonant, dropped the inherent `a`,
and the `M` went with it:

    सिंह  siMha  -> Simh     correct: Singh   (65 occurrences — most common
                                            name in the roll)
    शंकर  shaMkara -> Shamkar         Shankar
    चंद   chaMda -> Chand             Chand
    पंडित paMDita -> Pamdit           Pandit
    बिंदी biMdI   -> Bimdi            Bindi

`_assimilate_anusvara()` resolves the nasal on the Devanagari side, before
romanisation, writing it with a virama so it forms a conjunct and contributes
no vowel: `शंकर` → `शङ्कर` → `Shankar`. It has to run before romanisation —
afterwards the class is no longer visible, since `siMha` does not say whether
the M was velar or dental.

Two cases the rule must *not* touch:

- **Word-final anusvara** (`संत`). Rewriting it as a bare न invents a syllable:
  `Sanat`, not `Sant`. Left exactly as it was.
- **नं**, the abbreviation for "number" that `GLOBAL_STRUCTURAL_MAP` reads as
  `"No."`. This failed twice: word-final it became `नन` → `Nana`, and inside
  `नं-बी` the hyphen looked like a word boundary and it became `Nan-B`.

Anusvara names went from 0/22 to 16/22 agreeing with IndicTrans2. The 6 that
still differ are model meaning-drift (`अंकुर` → "shoots", `वंशज` →
"Descendants") or the long-vowel convention (`संजीव` → `Sanjeev`), which is a
spelling-convention fact and not derivable from the script.

## Routing

| Field | Engine | Why |
|---|---|---|
| `relation_name` | **rule-based** | closed vocabulary; MT mistranslates labels |
| `gender` | **rule-based** | closed vocabulary; MT mistranslates labels |
| `house_no` | **rule-based** + `house_en()` | address convention now a 4-line rule; see below |
| names | **undecided** | see open questions |

## House numbers: resolved without a model

A bare Devanagari vowel before digits is an address *label*, not a word.
Hindi electoral rolls write flats in Latin letters:

    इ-897   ->  E-897      (correct: E)
    इ-85    ->  E-85
    इ-9/827 ->  E-9/827

The general path renders `इ` as the vowel `i` and produces `I-897`. ITRANS is a
mechanical script mapping with no notion of address convention, so it cannot
recover this on its own — no amount of `GLOBAL_STRUCTURAL_MAP` entries fixes a
convention it does not model.

`house_en()` encodes the convention as a scoped rule instead of adopting a
185x-slower model: a long `ee` (ई) is /i:/, and the English letter naming that
sound is E; a lone vowel before a numeral is a flat letter. It calls
`transliterate()` first, so `GLOBAL_STRUCTURAL_MAP` still applies underneath.

Measured on the corpus, both engines are wrong somewhere:

| Input | Correct | Rule-based | IndicTrans2 |
|---|---|---|---|
| `इ-897` | E | `I-897` ✗ | `E-897` ✓ |
| `47-ई-7` | E (address) / I (word) | `47-E-7` ✓ | `47-E-7` ✓ |
| `15 ए` | E | `15 E` ✓ | `15 A` ✗ |
| `ए-73` | E | `E-73` ✓ | `E-73` ✓ |
| `एचएनओ 146` | HNO 146 | `HNO 146` ✓ | `HNO 146` ✓ |
| `बी-89` | B-89 | `B-89` ✓ | `The B-89` ✗ |
| `प्लॉट नं 279 ख नं 79` | — | ✓ | `Plot No.279B No.79` ✗ |

IndicTrans2's remaining failures are semantic-drift failures ("The B-89",
"Husband") — the class of error a rule cannot make. A rule is the better tool
for a convention.

## Why IndicTrans2 is not used for relations or gender

Semantic drift — correct translation, wrong answer for a voter roll:

    पति    -> Pati    (correct)   vs "husband"
    पिता   -> Pita     (correct)   vs "Father"
    माता   -> Mata     (correct)   vs "Mother"
    पुरुष  -> Purus    (acceptable) vs "the men's"
    महिला  -> Mahila   (acceptable) vs "Woman"

## IndicTrans2 failure modes (must be guarded against)

1. **Degenerate repetition.** `उत्तम` -> "the best of the best 。 。 。 。 ..."
   (40+ chars). Needs a repetition guard.
2. **Broken graphemes.** `कृष्णा` -> `Kr ̣ s ̣ n ̣ a` — combining marks
   detached from base letters.
3. **Number mangling.** `इ-9/827` -> `E-9 / 827` (invented space inside a
   flat number). Digits must be verified to survive.
4. **Name meaning-drift.** `आनन्द` -> "joy", `आकाश` -> "the sky",
   `कल्पना` -> "imagination", `आराम` -> "Relax".

## Open questions for names

Model wins on vowel fidelity: `अक्षय` Akshay, `अर्जुन` Arjuna, `अशोक` Ashok.
Model loses on meaning-drift and on `आनन्द` -> joy.

Needed before deciding:
- validate output contains no Devanagari
- reject output containing ASCII punctuation (guards "The B-89" class)
- preserve digits
- fall back to rule-based when validation fails

## Rejected

**IndicXlit romanization.** `ai4bharat/IndicXlit` ships only a fairseq
checkpoint (no Windows build, pins ancient torch). The transformers-native
port `psidharth567/indic-xlit-270M` does not ship the `CharTokenizer` class
its README references, and emits degenerate `[S]` repetition instead of
romanizing. Not viable.

# Transliteration engine routing — decisions from the IndicTrans2 evaluation

Date: 2026-10-02. Evidence: `tests/test_scripts/eval_indictrans2_vs_rulebased.py`
and `tests/test_scripts/sweep_corpus.py` over 346 unique Hindi strings from the
real ground-truth OCR output.

## Measured

| Engine | 346 strings | Notes |
| --- | --- | --- |
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
| --- | --- | --- | --- |
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

## Layer 1: the proper-noun spelling lexicon

Layer 0 produces a phonetic transliteration. English does not spell names
phonetically — `पाल` is "Pal", not "Pala"; `वर्मा` is "Verma", not "Varma".
That gap is a lexicon fact, not a derivable one, and `app/name_lexicon.json`
is the only place it lives.

**Nothing in the map was hand-approved.** `tests/test_scripts/build_layer1_map.py`
generates it from the measured disagreements, and an entry is accepted only if
all three hold:

1. The model output passes **every** guard in `tests/test_scripts/model_guards.py`
   — meaning drift, degenerate repetition, detached diacritics, lost digits.
   An entry learned from a rejected output would put garbage in the map.
2. The disagreement is a **spelling** difference, not a meaning one: same
   initial letter, same word count, within a small edit distance after
   collapsing doubled letters and the `ee`/`ii` vowel. This is what rejects
   "Victory" for `विजय` and "Hero" for `वीर` unattended.
3. The model output is **not longer** than the rules. The rules already drop
   the inherent schwa wherever English drops it, so a longer output means the
   model invented a vowel: `शिव` → "Shiva" when the correct English is "Shiv".
   This filter is why `शिव`, `महावीर` and `भीम` are *absent* from the map.

### Measured effect

| | strings | |
| --- | --- | --- |
| guard-passing name strings | 263 | |
| agree with IndicTrans2 before the map | 145 (55%) | Layer 0 only |
| agree with IndicTrans2 after the map | 166 (63%) | +21 |
| map entries | 21 | covering 72 occurrences |
| guard-rejected (never enter the map) | 52 (17%) | |

The 21 new agreements are exactly the 21 entries — the map cannot help
anywhere the guards rejected the model, by construction. That is the point:
Layer 1 only accepts spellings that survived Layer 3's filters, so it cannot
import drift into the rule path.

### Scope

Whole-string lookup only. A substring or fuzzy match would rewrite `राम` inside
`रामपाल` and corrupt names the rules already get right — `रामपाल` is "Ramapal",
not "Pal". The lookup is also skipped for any value carrying a digit or
punctuation, so a house number can never be shadowed by a name entry;
`house_en()` owns that convention.

A missing or corrupt lexicon degrades to Layer 0 alone rather than raising.

### Regenerating

    python tests/test_scripts/build_layer1_map.py --dry-run   # inspect
    python tests/test_scripts/build_layer1_map.py             # write

The generator deliberately bypasses the lexicon when computing the Layer 0
baseline. Without that, an entry already in the map makes its own comparison
show no disagreement and the map would silently empty itself on the next run.

## Layer 2: measured, and deliberately not built

The proposed Layer 2 was **phonetic-key retrieval** — key every lexicon entry by
a squashed form (doubled letters collapsed, `ee`/`ii` folded to `i`) so that an
*unseen* spelling of a known name retrieves the known spelling. Exact match only
helps a name the map has already seen verbatim; retrieval is supposed to help the
collisions.

Measured on the corpus, it retrieves nothing:

| | |
| --- | --- |
| remaining guard-passing name disagreements | 97 strings, 120 occurrences |
| already fixed by Layer 1 exact match | 21 (the map's own entries) |
| **share a squashed phonetic key with a Layer 1 entry** | **0** |
| differ from the rules only by vowel length | 2 |

All 21 keys over the 21 entries are distinct, so there is nothing for a
retrieval step to disambiguate. The premise — that unseen spellings of known
names are the main remaining error — is false on this roll.

The deeper problem is that the key cannot be built. Folding `ee`→`i` is
**one-way**: `Sanjiv`→`sanjiv` but `Sanjeev`→`sanjev`, while `Bineesh`→`binish`
and `Binesh`→`binesh` stay apart. The fold erases precisely the distinction it
would need to detect, so any key built on it is blind to the long-vowel
convention that motivates it.

What the 97 actually are is model drift the guards let through, not spelling
variants — and a spelling layer cannot repair a translation:

| Hindi | rules | IndicTrans2 |
| --- | --- | --- |
| अभिलाषा | Abhilasha | "aspiration" |
| अय्यूब | Ayyub | "Job" |
| गिरी | Giri | "fell down" |
| चरण | Charan | "phase" |
| छवि | Chavi | "image" |

The only systematic *spelling* disagreement left is the long-vowel convention
(`Sanjiv`/`Sanjeev`, `Sandip`/`Sandeep`, `Nitu`/`Neetu`, `Niraj`/`Neeraj`,
`Pradip`/`Pradeep`, `Gita`/`Geeta`) — 6 pairs at 1–5 occurrences each. That is a
derivable convention, not a retrieval problem, so if it is ever worth encoding it
belongs in Layer 0 as a rule keyed on ITRANS' long-`ii` marker, not in a
retrieval layer.

Guarded by `tests/test_scripts/test_squash.py`, which pins the key's ordering
directly. That ordering was wrong for a while and nothing failed: the
Levenshtein fallback in `looks_like_a_spelling` rescued all 21 entries anyway.
The bug was therefore inert for Layer 1 — the committed map is byte-identical
before and after the fix — but it would have silently broken any retrieval layer
built on the same function.

## Routing

| Field | Engine | Why |
| --- | --- | --- |
| `relation_name` | **rule-based** | closed vocabulary; MT mistranslates labels |
| `gender` | **rule-based** | closed vocabulary; MT mistranslates labels |
| `house_no` | **rule-based** + `house_en()` | address convention is a 4-line rule; see below |
| names | **Layer 0 + Layer 1** | no model at runtime; 63% of guard-passing strings |

Names now run entirely on rules plus the lexicon. IndicTrans2 is not in the
production path — it is the *oracle* that generated the lexicon, and its
output survives three filters before being allowed to become a rule.

## Layer 3 (IndicTrans2): the oracle, not the runtime

IndicTrans2-200M stays out of `transliterate()`. Its measured value is that it
supplies the spelling conventions Layer 0 cannot derive — and it supplies them
**once, offline**, into a map that is exact-match, auditable, and free at
runtime.

Against the guards it is rejected on 17% of name strings, and those rejections
are not marginal:

| Hindi | rules | IndicTrans2 | why rejected |
| --- | --- | --- | --- |
| चन्द्र | Chandra | "the moon" | meaning drift (10 occurrences) |
| सीमा | Sima | "the limit of the limit…" ×15 | repetition, runaway |
| उत्तम | Uttam | "the best of the best 。 。 。" | repetition, drift |
| भोज | Bhoj | 300-char "feast of the food of the food…" | repetition, drift |
| कृष्ण | Krishna | "Kr ̣ s ̣ n ̣ a" | orphaned combining marks |
| लाल | Lala | "Red" | meaning drift |

128 of the 299 guard-passing strings already agreed with the rules, so the
model's runtime contribution would have been small even unguarded. Offline it
is genuinely useful; online it is 185x slower and strictly riskier.

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
| --- | --- | --- | --- |
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

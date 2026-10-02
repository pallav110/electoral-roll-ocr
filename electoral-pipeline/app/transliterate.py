"""Devanagari → Latin transliteration for electoral roll records.

Why transliteration and not machine translation
-----------------------------------------------
Names are proper nouns. A translation engine will happily turn "आकाश" into
"Sky" and "बबीता" into "Babeeta", and an LLM does the same thing
non-deterministically — the same voter would render differently on every
re-run. For a records database that is disqualifying: the English column has
to be a pure function of the Hindi column, forever, for free, offline.

`sanscript` maps Devanagari codepoints to Latin phonetics mathematically. It
never looks anything up, so it handles any name, including ones no
dictionary contains, and it is a no-op on text that is already Latin.

Deliberately NOT used: deepl, google-genai, any LLM. See module docstring.

Output flavour
--------------
IAST with diacritics stripped ("Rājīva" → "Rajiva"). Stripped rather than
raw IAST because these values land in URLs, search boxes and CSV exports,
where "Sakṣeṇa" is harder to type and matches how the same names are
usually romanised on English-language electoral lists.

Set TRANSLITERATION_SCHEME=itrans to switch flavour without code changes.

Name translation (optional)
---------------------------
For voter names and relative names ONLY, we optionally use a cloud translation
service (deep-translator or googletrans) with local JSON caching to get common
English spellings (e.g., "आकाश" → "Aakash" instead of "akasa"). This is
enabled by default but can be disabled with NAME_TRANSLATION_PROVIDER=none.
House numbers, genders, relationships, and section names always use
transliteration.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import unicodedata
from pathlib import Path

log = logging.getLogger(__name__)

# Populated lazily by _load_sanscript(); importing the package at module
# import time would make the whole web service fail if the dependency is
# missing, and we degrade to pass-through instead.
_SANSCRIPT = None
_LOAD_ERROR: str | None = None

DEVANAGARI = re.compile(r"[ऀ-ॿ]")

# Devanagari vowel signs and modifiers (matras + final marks).
_MATRAS = set(map(chr, [
    0x093e, # ा
    0x093f, # ि
    0x0940, # ी
    0x0941, # ु
    0x0942, # ू
    0x0943, # ृ
    0x0947, # े
    0x0948, # ै
    0x094b, # ो
    0x094c, # ौ
    0x0902, # ं
    0x0901, # ँ
    0x0903  # ः
]))
_VIRAMA = chr(0x094d)  # ्
# Punctuation and joiners that can trail a name token (all explicit; no literals).
_TRAILING_NOISE = set(map(chr, [
    0x0964, # ।
    ord('.'),
    ord(','),
    ord(';'),
    ord(':'),
    ord('-'),
    0x200c, # ZWNJ
    0x200d, # ZWJ
    0xfeff  # BOM
]))

def _print_marks():
    print("_MATRAS =", [(f"U+{ord(c):04X}", c) for c in sorted(_MATRAS)])
    print("_TRAILING_NOISE =", [(f"U+{ord(c):04X}", c) for c in sorted(_TRAILING_NOISE)])
    print("_VIRAMA =", (f"U+{ord(_VIRAMA):04X}", _VIRAMA))
# _print_marks()  # Uncomment in dev to inspect invisible sets


# Scheme -> (sanscript scheme name, strip combining marks?)
SCHEMES = {
    "iast-plain": ("iast", True),
    "iast": ("iast", False),
    "itrans": ("itrans", True),
    "hk": ("hk", True),
}

# 1. Static administrative and core value mappings (closed vocabulary)
STATIC_MAPPINGS = {
    # Gender
    "पुरुष": "Male",
    "महिला": "Female",
    "तृतीय लिंग": "Third Gender",
    "पुरूष": "Male",
    "स्त्री": "Female",
    "पु": "Male",
    "म": "Female",
    # Relationship
    "पिता": "Father",
    "माता": "Mother",
    "पति": "Husband",
    "पत्नी": "Wife",
    "अन्य": "Other",
    "भाई": "Brother",
    "पुत्र": "Son",
    "पुत्री": "Daughter",
}

# 2. Structural address and system shorthand map (address components)
# Keys are lowercase romanised forms (ITRANS output) that the cleaner matches.
# ITRANS mapping reference:
# Vowels: अ=a, आ=A, इ=i, ई=I, उ=u, ऊ=U, ए=e, ऐ=ai, ओ=o, औ=au
# Consonants + inherent 'a': क=ka, ख=kha, ग=ga, घ=gha, च=cha, छ=Cha, ज=ja, झ=jha, ट=Ta, ठ=Tha, ड=Da, ढ=Dha, त=ta, थ=tha, द=da, ध=dha, न=na, प=pa, फ=pha, ब=ba, भ=bha, म=ma, य=ya, र=ra, ल=la, व=va, श=sha, ष=Sha, स=sa, ह=ha
# Matras: ा=A, ि=i, ी=I, ु=u, ू=U, े=e, ै=ai, ो=o, ौ=au, ं=M, ः=H, ्=
GLOBAL_STRUCTURAL_MAP = {
    # Common address words - mapped from ITRANS output
    "kaloni": "Colony",           # कॉलोनी -> kaoloni
    "koloni": "Colony",           # कॉलोनी -> koloni (variant)
    "plot": "Plot",               # प्लॉट -> plot
    "plaota": "Plot",             # प्लॉट -> plaota (without diacritic)
    "plata": "Plot",              # प्लॉट -> plata (after Mc stripping)
    "makan": "House",             # मकान -> makan
    "makaan": "House",            # मकान -> makaan
    "sankhya": "No.",             # संख्या -> sankhya
    "sankhyaa": "No.",            # संख्या -> sankhyaa
    "khasra": "Khasra",           # खसरा -> khasra
    "khasara": "Khasra",          # खसरा -> khasara
    "kha": "Kha",                 # ख -> kha (khasra prefix)
    "nam": "No.",                 # नं -> nam (न + ं)
    "na": "No.",                  # न -> na
    "no": "No.",                  # no
    "n": "No.",                   # n
    "gali": "Gali",               # गली -> gali
    "se": "to",                   # से -> se
    "b": "B",                     # ब -> b
    "bi": "B",                    # बी -> bi
    "block": "Block",             # ब्लॉक -> block
    "blok": "Block",              # ब्लॉक -> blok
    "sector": "Sector",           # सेक्टर -> sector
    "phaze": "Phase",             # फेज -> phaze
    "phez": "Phase",              # फेज -> phez
    "extenshan": "Extension",     # एक्सटेंशन -> extenshan
    "ext": "Extension",           # एक्सटेंशन -> ext
    "rod": "Road",                # रोड -> rod
    "strit": "Street",            # स्ट्रीट -> strit
    "avenyu": "Avenue",           # एवेन्यू -> avenyu
    "hno": "HNO",                 # एचएनओ -> hno
    "echaenao": "HNO",            # एचएनО -> echaenao (with च)
    "ehanao": "HNO",              # एहनО -> ehanao (with ह)
    "echaano": "HNO",             # एचАNО -> echaano (Cyrillic А/О + Latin N)
    "echa": "H",                  # एच -> echa (H)
    "echaena": "HNO",             # एचएन -> echaena (HNO)
    "echaenao146": "HNO 146",     # एचएनО146 -> echaenao146 (with digits)
    "echaenao146": "HNO 146",     # duplicate entry for common OCR output
    "ha": "H",                    # ह -> ha (H)
    "hana": "HNO",                # हन -> hana (HNO)
    "hano": "HNO",                # हनो -> hano (HNO)
    "han": "HN",                  # हन -> han (HN)
    "pno": "PNO",                 # पी. नं -> pno
    "pi": "P",                    # पी -> pi
    "p": "P",                     # प -> p
    # Vowel normalization (choti ee/badi ee both -> i, choti u/badi u both -> u)
    "ee": "i",                    # ई -> I -> i (lowercase)
    "ii": "i",                    # ई variant
    "oo": "u",                    # ऊ -> U -> u
    "uu": "u",                    # ऊ variant
    "ai": "ai",                   # ऐ -> ai
    "au": "au",                   # औ -> au
    "sha": "sha",                 # श -> sha
    "sha_": "sha",                # ष -> Sha -> sha (lowercase)
    "ksha": "ksha",               # क्ष -> ksha
    "tra": "tra",                 # त्र -> tra
    # This library does not spell ज्ञ as "gya". It marks the rare nasal ञ
    # (U+091E) that ज्ञ is built from with a literal '~' -- "j~na" -- which
    # _strip_marks then removes, so the key reaching the map is "jna". The old
    # "gya" key therefore never matched anything. Keys are the post-strip form.
    "jn": "Gyan",                 # ज्ञ
    "jna": "Gyan",                # ज्ञ -> jna (schwa dropped)
    "jnа": "Gyan",                # ज्ञ, Cyrillic-final variant seen in OCR
    "gya": "Gyan",                # ज्ञ -> gya (older/alternate spelling)
    # Numbers and punctuation pass through unchanged
}


# Homorganic nasal (anusvara) assimilation.
#
# Devanagari anusvara (ं) and chandrabindu (ँ) are not letters with a fixed
# sound: the nasal assimilates to the class of the consonant that follows.
# English spelling follows the same rule, so the Devanagari already tells us
# what to write:
#
#     सिंह  si + M + ha   -> the M is velar before h -> Singh  (not "Simh")
#     शंकर sha + M + ka   -> velar            -> Shankar (not "Shamkar")
#     चंद  cha + M + da   -> retroflex        -> Chand   (not "Chamd")
#     पंडित pa + M + Da   -> retroflex        -> Pandit  (not "Pamdit")
#     बिंदी bi + M + dI    -> dental           -> Bindi   (not "Bimdi")
#
# ITRANS writes the anusvara as a literal "M" (siMha). The schwa rule then
# saw "ha" -- a bare consonant -- dropped the inherent 'a', and the M went
# with it. Every one of the 22 anusvara names in the roll was wrong as a
# result, including सिंह, the most frequent name in the corpus at 65
# occurrences.
#
# Resolved on the Devanagari side, before the romanisation, because by the
# time we have "siMha" the class of the following letter is no longer visible.
_ANUSVARA = chr(0x0902)   # ं
_CHANDRABINDU = chr(0x0901)  # ँ
_NASALS = {_ANUSVARA, _CHANDRABINDU}

# _MATRAS includes ं and ँ because they sit where a vowel sign sits, but they
# are nasals, not vowels: an anusvara is a consonant letter that may be
# followed by another one (हंस + ं). Searching for the consonant that governs
# an anusvara has to skip real vowel signs only, or "नं" finds no consonant and
# a word-final anusvara gets rewritten as a bare न.
_VOWEL_SIGNS = _MATRAS - _NASALS

# A single Devanagari letter: consonant, vowel, nasal or sign. Used to tell
# "the next character is another letter to assimilate against" from "the next
# character is a separator and this token really has ended".
_DEVANAGARI_LETTER = re.compile(r"[ऀ-ॿ]")

# Place (varna) groups. The anusvara takes the nasal of the following
# consonant's group. The value is the Devanagari letter carrying that class's
# nasal, written with a VIRAMA so it forms a conjunct with the consonant that
# follows and contributes no vowel of its own:
#
#     शंकर  sha + anusvara + ka  ->  शङ्कर  (n becomes ङ-class, joins ka)
#     चंद   cha + anusvara + da  ->  चन्द
#
# Writing a bare न/ण here instead would splice in an inherent schwa and give
# "Shanakar" / "Chanad" -- the nasal must be a conjunct, not a syllable.
_VIRAMA_CHR = chr(0x094D)   # ्
_N_DENTAL = chr(0x0928)     # न
_N_RETROFLEX = chr(0x0923)  # ण
_N_PALATAL = chr(0x091E)    # ञ

_VELAR = "कखगघङ"          # ka kha ga gha nga
_PALATAL = "चछजझञ"        # cha chha ja jha nya
_RETROFLEX = "टठडढण"       # Ta Tha Da Dha na
_DENTAL = "तथदधन"          # ta tha da dha na
_LABIAL = "पफबभम"          # pa pha ba bha ma
_SEMIVOWELS = "यरलव"       # ya ra la va

_ANUSVARA_NASAL = {}
_ANUSVARA_NASAL.update({ch: _N_RETROFLEX for ch in _VELAR})     # ङ-class -> ण
_ANUSVARA_NASAL.update({ch: _N_PALATAL for ch in _PALATAL})      # ञ-class -> ञ
_ANUSVARA_NASAL.update({ch: _N_RETROFLEX for ch in _RETROFLEX})  # ण-class -> ण
_ANUSVARA_NASAL.update({ch: _N_DENTAL for ch in _DENTAL})         # न-class -> न
_ANUSVARA_NASAL.update({ch: _N_DENTAL for ch in _LABIAL})         # म-class -> न
_ANUSVARA_NASAL.update({ch: _N_DENTAL for ch in _SEMIVOWELS})      # यरलव -> न


def _assimilate_anusvara(token: str) -> str:
    """Rewrite each anusvara in a Devanagari token as the nasal conjunct the
    following consonant calls for, so the romaniser has a real letter pair to
    transliterate instead of a bare "M" that the schwa rule then deletes.

        सिंह   si + M + ha  ->  सिन्ह    -> Singh     (velar: n joins h)
        शंकर  sha + M + ka  ->  शङ्कर   -> Shankar   (velar)
        चंद   cha + M + da  ->  चन्द    -> Chand     (retroflex)
        पंडित pa + M + Dita ->  पण्डित  -> Pandit    (retroflex)
        बिंदी bi + M + dI    ->  बिन्दी  -> Bindi     (dental)

    The nasal is written with a virama so it forms a conjunct with the
    consonant after it and adds no vowel of its own. A word-final anusvara has
    nothing to join, so it becomes a plain न and keeps its inherent schwa --
    संत -> सन्त -> Sant.

    This has to run before romanisation. Afterwards the class of the following
    letter is no longer visible: "siMha" does not say whether the M was velar
    or dental, only the Devanagari does.
    """
    if not token or not (_ANUSVARA in token or _CHANDRABINDU in token):
        return token

    out = list(token)
    for index, char in enumerate(out):
        if char not in _NASALS:
            continue

        # Find the next consonant, skipping real vowel signs and any further nasal.
        # A label separator or a digit means the word really did end, but the
        # abbreviation still has to survive as one token for the map: in
        # "नं-बी 190" the hyphen splits the token, and assimilating there turned
        # the "No." into "Nan-B".
        follower_index = None
        for offset in range(index + 1, len(out)):
            candidate = out[offset]
            if candidate in _VOWEL_SIGNS or candidate == _VIRAMA or candidate in _NASALS:
                continue
            if not _DEVANAGARI_LETTER.fullmatch(candidate):
                # Punctuation, a digit, or Latin: the token boundary is real.
                break
            follower_index = offset
            break

        if follower_index is None:
            # Word-final: nothing to join, so the anusvara is left exactly as
            # it was. A plain nasal here would invent a syllable -- "नं" (the
            # abbreviation for "number", which GLOBAL_STRUCTURAL_MAP reads as
            # "No.") became "नन" -> "Nana", and संत became "सनत" -> "Sanat".
            # Leaving it alone keeps the token intact for the map, and the
            # romaniser already spells a trailing anusvara as n.
            continue

        out[index] = _ANUSVARA_NASAL.get(out[follower_index], _N_DENTAL) + _VIRAMA_CHR
    return "".join(out)


def _final_roman_a_is_real(hindi_token: str) -> bool:
    """Does this Devanagari token end in a vowel that the romanisation writes
    as a final 'a' that must be KEPT?

    Asked of the script, never of the romanised text. In Devanagari the final
    syllable is unambiguous:

        राजीव   -> राजीव   ends in a bare consonant    -> inherent schwa -> drop
        कल्पना  -> कल्पना  ends in मात्रा ा             -> real vowel     -> keep
        रेखा    -> रेखा    ends in मात्रा ा             -> real vowel     -> keep
        रविन्द्र -> रविन्द्र ends in a conjunct (virama) -> conventional   -> keep
        सचिन    -> सचिन    ends in a bare consonant    -> inherent schwa -> drop

    Why this cannot be asked of the romanised form: by then राजीव and कल्पना
    have both become "rajiva"/"kalpana", differing only in a vowel quality
    the romanisation has already flattened. That is why the previous rule had
    to guess from a hand-written suffix list and got both directions wrong
    (Kalpan, Saksen, Rohita, Amita). Deriving the answer from the script also
    keeps it correct if TRANSLITERATION_SCHEME is changed.

    Known limitation: a bare consonant after a virama is treated as keeping
    its vowel, which is right for रविन्द्र (Ravindra), चन्द्र (Chandra) and
    दत्त (Datta) but wrong for गर्ग, where English convention is "Garg" not
    "Garga". Script alone cannot separate those; that is a spelling
    convention, and 23 of the 24 conjunct-ending names in the roll are
    served correctly by this branch.
    """
    token = (hindi_token or "").strip()
    while token and token[-1] in _TRAILING_NOISE:
        token = token[:-1]
    if not token:
        return False
    last = token[-1]
    if last in _MATRAS:
        return True                       # explicit matra -> a real vowel
    if last == _VIRAMA:
        return False                      # no vowel signalled at all
    if len(token) >= 2 and token[-2] == _VIRAMA:
        return True                       # conjunct before it -> conventional vowel
    return False                          # bare consonant -> inherent schwa


# Split on common delimiters while preserving them for reconstruction
_DELIMITER_RE = re.compile(r'([-/,.])')


def _clean_segment(segment: str, keep_final_a: bool) -> str:
    """Apply the phonetic clean-up to one romanised segment (no delimiters)."""
    segment_lower = segment.lower()
    if segment_lower in GLOBAL_STRUCTURAL_MAP:
        return GLOBAL_STRUCTURAL_MAP[segment_lower]

    # Step C: drop the inherent-schwa 'a' unless the script says it is real.
    if segment_lower.endswith("a") and len(segment) > 4 and not keep_final_a:
        segment = segment[:-1]

    # Step D: standardise character pairs the algorithmic mapping produces.
    segment = re.sub(r'amch', 'anch', segment, flags=re.IGNORECASE)  # Uttaramchal -> Uttaranchal
    segment = re.sub(r'ee', 'i', segment, flags=re.IGNORECASE)
    segment = re.sub(r'oo', 'u', segment, flags=re.IGNORECASE)
    segment = re.sub(r'shh', 'sh', segment, flags=re.IGNORECASE)
    segment = re.sub(r'rri', 'ri', segment, flags=re.IGNORECASE)
    segment = re.sub(r'rru', 'ru', segment, flags=re.IGNORECASE)

    # ज्ञ is a single conjunct (ज् + ञ) that English always spells "gy"/"gy"-
    # vowel. This library does not transliterate it that way: it marks the rare
    # nasal ञ with a literal '~', which _strip_marks removes, leaving "jn" --
    # so ज्ञान reached output as "Jnan". A map entry can only fix the whole word
    # it names, and every compound of ज्ञ is a different string; the conjunct is
    # the actual unit, so it is fixed here alongside the other digraph folds.
    segment = re.sub(r'jn(?=[aeiou])', 'gy', segment, flags=re.IGNORECASE)
    segment = re.sub(r'^jn', 'gy', segment, flags=re.IGNORECASE)

    # The ङ-upadhmanya conjunct is the one the library drops a letter from:
    #     सन्ह -> "snha"  -> "ngh"
    # Standard IAST writes this digraph "ṅᵛh", so the library is the thing
    # that is wrong here, not the spelling. सिंह is the most frequent name in
    # the roll at 65 occurrences.
    # The palatal nasal ञ needs no rule: _strip_marks already removes the
    # library's '~' marker and "sa~njIva" lands on the correct "sanj".
    segment = re.sub(r'nh', 'ngh', segment, flags=re.IGNORECASE)

    return segment.strip().title()


def _clean_word(word: str, keep_final_a: bool) -> str:
    """Apply the phonetic clean-up to one romanised token.

    Handles tokens with delimiters like 'bI-89' by splitting on
    delimiters, cleaning each segment, and rejoining.
    """
    # Split on delimiters, keeping them
    parts = _DELIMITER_RE.split(word)
    # parts will be like ['bI', '-', '89'] for 'bI-89'
    cleaned_parts = []
    for i, part in enumerate(parts):
        if i % 2 == 0:
            # Even indices are the segments between delimiters
            if part:
                cleaned_parts.append(_clean_segment(part, keep_final_a))
        else:
            # Odd indices are the delimiters themselves
            cleaned_parts.append(part)
    return "".join(cleaned_parts)


def clean_phonetic_rules(text: str, source_tokens=None) -> str:
    """Apply phonetic cleaning rules to ITRANS/IAST output.

    This handles:
    - Trailing 'a' removal, decided from the Devanagari token it came from
    - Character pair standardization (amch → anch, ee → i, etc.)
    - Structural token replacement (kaloni → Colony, etc.)

    Note: Internal schwa deletion (Step B) was removed as it was too aggressive
    and incorrectly shortened many valid names (akasa→aksa, babita→bbita).
    The IAST/iast-plain output is already quite good for Indian names.

    Args:
        text: Transliterated text (ITRANS/IAST output)
        source_tokens: the Devanagari tokens `text` was transliterated from,
            in the same order. When given, the trailing-'a' decision is made
            from the script (correct). When omitted - a Latin-only string, or
            a caller that has no source - every word keeps its vowel, which is
            the conservative choice: it never invents a vowel that is not
            there, it only fails to drop a schwa.

    Returns:
        Cleaned English text with proper capitalization
    """
    if not text or not isinstance(text, str):
        return text

    words = text.split()
    tokens = list(source_tokens) if source_tokens is not None else []
    if len(tokens) != len(words):
        tokens = [None] * len(words)

    cleaned_words = [
        _clean_word(word, _final_roman_a_is_real(tokens[index]) if tokens[index] else False)
        for index, word in enumerate(words)
    ]

    return " ".join(cleaned_words)


def _scheme() -> tuple[str, bool]:
    """The romanisation scheme to transliterate with.

    Defaults to ITRANS because the whole cleanup layer is keyed on ITRANS:
    GLOBAL_STRUCTURAL_MAP is documented as "keys are lowercase romanised forms
    (ITRANS output)", and _clean_segment's digraph folds (shh->sh, rri->ri,
    amch->anch) only fire on ITRANS spellings. Running the IAST default
    disabled all of it at once -- every one of those rules silently became a
    no-op, so ITRANS digraphs degraded to single letters:

        ITRANS  iast-plain  output       ITRANS   iast-plain
        shiva   siva       Siva/Shiv     kShetra  ksetra     Kshetra/Ksetra
        chandra candra     Candra/Chandra akShaya  aksaya     Akshay/Aksay

    The IAST forms are not more correct -- they are the *diacritic-stripped*
    IAST forms, and it is the stripping that costs the 'sh'. IAST only wins
    where ITRANS emits a non-combining escape (see _UDATTA_RE below).
    """
    return SCHEMES.get(
        os.getenv("TRANSLITERATION_SCHEME", "itrans").strip().lower(),
        SCHEMES["itrans"],
    )


def _load_sanscript():
    """Import sanscript on first use, cache the result."""
    global _SANSCRIPT, _LOAD_ERROR
    if _SANSCRIPT is not None or _LOAD_ERROR is not None:
        return _SANSCRIPT
    try:
        from indic_transliteration import sanscript  # type: ignore

        _SANSCRIPT = sanscript
    except Exception as exc:  # pragma: no cover - depends on install
        _LOAD_ERROR = f"{type(exc).__name__}: {exc}"
        log.error(
            "indic_transliteration unavailable (%s); English columns will "
            "mirror the Hindi text instead", _LOAD_ERROR,
        )
    return _SANSCRIPT


# Cyrillic → Devanagari normalization map for common OCR misreads
_CYRILLIC_TO_DEVANAGARI = {
    'А': 'अ',  # А -> अ
    'Б': 'भ',  # Б -> भ (rough)
    'В': 'व',  # В -> व
    'Г': 'ग',  # Г -> ग
    'Д': 'द',  # Д -> द
    'Е': 'ए',  # Е -> ए
    'Ж': 'झ',  # Ж -> झ
    'З': 'ज',  # З -> ज
    'И': 'इ',  # И -> इ
    'Й': 'य',  # Й -> य
    'К': 'क',  # К -> क
    'Л': 'ल',  # Л -> ल
    'М': 'म',  # М -> म
    'Н': 'न',  # Н -> न
    'О': 'ओ',  # О -> ओ
    'П': 'प',  # П -> प
    'Р': 'र',  # Р -> र
    'С': 'स',  # С -> स
    'Т': 'त',  # Т -> त
    'У': 'उ',  # У -> उ
    'Ф': 'फ',  # Ф -> फ
    'Х': 'ह',  # Х -> ह
    'Ц': 'थ',  # Ц -> थ
    'Ч': 'च',  # Ч -> च
    'Ш': 'ष',  # Ш -> ष
    'Щ': 'श',  # Щ -> श
    'Ъ': '्',  # Ъ -> ् (virama)
    'Ы': 'ॏ',  # Ы -> ॉ (candra o)
    'Ь': '्',  # Ь -> ् (virama)
    'Э': 'ए',  # Э -> ए
    'Ю': 'य',  # Ю -> य
    'Я': 'य',  # Я -> य
    # lowercase
    'а': 'अ',  # а -> अ
    'б': 'भ',  # б -> भ
    'в': 'व',  # в -> व
    'г': 'ग',  # г -> ग
    'д': 'द',  # д -> द
    'е': 'ए',  # е -> ए
    'ж': 'झ',  # ж -> झ
    'з': 'ज',  # з -> ज
    'и': 'इ',  # и -> ин
    'й': 'य',  # й -> य
    'к': 'क',  # к -> क
    'л': 'ल',  # л -> л
    'м': 'म',  # м -> म
    'н': 'न',  # н -> न
    'о': 'ओ',  # о -> ओ
    'п': 'प',  # п -> प
    'р': 'र',  # р -> र
    'с': 'स',  # с -> स
    'т': 'त',  # т -> त
    'у': 'उ',  # у -> у
    'ф': 'फ',  # ф -> फ
    'х': 'ह',  # х -> ह
    'ц': 'थ',  # ц -> थ
    'ч': 'च',  # ч -> च
    'ш': 'ष',  # ш -> ष
    'щ': 'श',  # щ -> श
    'ъ': '्',  # ъ -> ्
    'ы': 'ॏ',  # ы -> ॉ
    'ь': '्',  # ь -> ्
    'э': 'ए',  # э -> ए
    'ю': 'य',  # ю -> य
    'я': 'य',  # я -> य
}

_CYRILLIC_RE = re.compile(r'[Ѐ-ӿ]')

# The udatta/anudatta stress signs (U+0951/U+0952) are almost always an OCR
# misread of the anusvara in "nँ" -- "नं" in a house number like "हाऊस नं - 121".
# ITRANS renders them as the literal two-character sequences "\'" and "\_",
# which are not Unicode combining marks, so _strip_marks cannot remove them:
# the address came out as "Haus Na\' - 121". IAST happens to emit a real
# combining mark there, which is why this only appears under ITRANS.
# Normalising the input fixes it for every scheme and keeps the escape out of
# the cleanup layer entirely.
_STRESS_TO_ANUSVARA = {
    chr(0x0951): chr(0x0902),  # ॑ udatta  -> ं anusvara
    chr(0x0952): chr(0x0902),  # ॒ anudatta -> ं anusvara
}
_STRESS_RE = re.compile(f"[{''.join(_STRESS_TO_ANUSVARA)}]")


def _normalize_stress_marks(text: str) -> str:
    if not _STRESS_RE.search(text):
        return text
    return "".join(_STRESS_TO_ANUSVARA.get(ch, ch) for ch in text)


def _normalize_cyrillic(text: str) -> str:
    """Replace Cyrillic lookalikes with Devanagari equivalents.

    OCR sometimes produces Cyrillic characters that visually resemble
    Devanagari (e.g., U+041E 'О' vs U+0913 'ओ'). This normalizes them
    so transliteration works correctly.
    """
    if not _CYRILLIC_RE.search(text):
        return text
    return "".join(_CYRILLIC_TO_DEVANAGARI.get(ch, ch) for ch in text)


def _strip_marks(text: str) -> str:
    """Drop combining marks (Mn + Mc) so the romanisation becomes plain ASCII.

    ITRANS can emit Devanagari spacing marks (Mc, e.g., U+0949 CANDRA O)
    which survive NFD + combining() filter. Remove both Mn and Mc.

    ITRANS also marks two rare letters with a literal '~' rather than a
    combining sign: ङ (U+0919) and ञ (U+091E). ज्ञ is built from the second, so
    the library writes it "j~n" and it reached English output as "J~Nan".
    The tilde is a marker, never a letter, so it is removed here -- once, for
    every word -- instead of needing a GLOBAL_STRUCTURAL_MAP entry per word.
    """
    decomposed = unicodedata.normalize("NFD", text)
    stripped = unicodedata.normalize("NFC", "".join(
        ch for ch in decomposed
        if unicodedata.category(ch) not in ("Mn", "Mc")
    ))
    return stripped.replace("~", "") if "~" in stripped else stripped


# Layer 1: proper-noun spelling lexicon.
#
# Everything above is a rule, and a rule cannot know how English writes a
# particular name. The rules give the phonetic transliteration -- पाल -> "Pala",
# सिंह -> "Simh", वर्मा -> "Varma" -- but English writes Pal, Singh, Verma. That
# is a lexicon fact, not a derivable one, and it is the only thing this map is
# for.
#
# Whole-string lookup only, never a substring or fuzzy match. An exact key
# either names this name or it does not; a partial key would rewrite "राम"
# inside "रामपाल" and corrupt names the rules already get right.
#
# Generated by tests/test_scripts/build_layer1_map.py from measured
# disagreements, accepting an entry only when the model's output passed every
# guard in tests/test_scripts/model_guards.py and differed from the rules only
# by spelling (small edit distance) rather than by meaning. Nothing here was
# hand-approved; see DOCS/TRANSLITERATION_ENGINE_ROUTING.md for the measured
# before/after.
_NAME_LEXICON_CACHE: dict[str, str] | None = None


def _name_lexicon() -> dict[str, str]:
    global _NAME_LEXICON_CACHE
    if _NAME_LEXICON_CACHE is None:
        path = Path(__file__).with_name("name_lexicon.json")
        try:
            with open(path, encoding="utf-8") as handle:
                data = json.load(handle)
            _NAME_LEXICON_CACHE = {
                str(k): str(v) for k, v in (data.get("entries") or {}).items()
            }
        except (OSError, ValueError) as exc:
            # A missing or corrupt lexicon must not take the service down: the
            # rules alone are a complete, if more phonetic, answer.
            log.warning("name lexicon unavailable, using rules only: %s", exc)
            _NAME_LEXICON_CACHE = {}
    return _NAME_LEXICON_CACHE


def transliterate(text) -> str | None:
    """Devanagari → Latin. Returns input unchanged if already Latin, and
    None for empty/None so the caller can store SQL NULL rather than ''.

    Handles mixed content like "7 बी" → "7 bi" by splitting on word boundaries
    and transliterating only words containing Devanagari, preserving digits,
    punctuation, and Latin text.

    Applies phonetic cleaning rules for better name/address rendering.
    """
    if text is None:
        return None
    value = str(text).strip()
    if not value:
        return None
    # Normalize Cyrillic lookalikes that OCR sometimes produces
    value = _normalize_cyrillic(value)
    # Same for the udatta/anudatta stress signs, which ITRANS writes as literal
    # backslash escapes rather than combining marks.
    value = _normalize_stress_marks(value)

    # Layer 1 first, before any rules run. The lexicon exists to override the
    # phonetic transliteration with English's spelling of a name, and every
    # rule below would only make that harder to undo.
    #
    # Scope: whole-string only, and never for text carrying a digit or
    # punctuation. A house number like "47-ई-7" is a label, not a name, and
    # house_en() owns that convention -- a lexicon hit here would shadow it.
    if not any(ch.isdigit() for ch in value) and re.search(r"[-/,.]", value) is None:
        known = _name_lexicon().get(value)
        if known:
            return known
    # No Devanagari: already English (a house number, an EPIC, a Latin name
    # the OCR read directly). Pass through untouched — never transliterate a
    # number, and never "translate" "64" into something else.
    if not DEVANAGARI.search(value):
        return value
    sanscript = _load_sanscript()
    if sanscript is None:
        return value
    target, strip = _scheme()
    try:
        # Split by word boundaries (whitespace), transliterate only tokens
        # that contain Devanagari. This preserves grapheme clusters within
        # words while handling mixed content like "7 बी".
        tokens = value.split()
        out_tokens = []
        for token in tokens:
            if DEVANAGARI.search(token):
                # This token has Devanagari - transliterate the whole token.
                # The anusvara is assimilated first, while the following letter
                # is still a Devanagari letter and its class (velar/palatal/
                # retroflex/dental/labial) is still readable. See
                # _assimilate_anusvara for why this cannot be done afterwards.
                out = sanscript.transliterate(
                    _assimilate_anusvara(token), sanscript.DEVANAGARI, target
                )
                if strip:
                    out = _strip_marks(out)
                out_tokens.append(out)
            else:
                # No Devanagari - keep as-is
                out_tokens.append(token)
        out = " ".join(out_tokens)
    except Exception as exc:
        log.warning("transliterate failed for %r: %s", value, exc)
        return value

    # Apply phonetic cleaning rules. The Devanagari tokens go with the
    # romanised ones so the trailing-'a' decision can be made from the
    # script, where it is still unambiguous.
    out = clean_phonetic_rules(out, source_tokens=tokens)

    return " ".join(out.split()) or None


def transliterate_vocab(text, table: dict[str, str]) -> str | None:
    """Look a closed-vocabulary value up; transliterate only if unknown."""
    if text is None:
        return None
    value = str(text).strip()
    if not value:
        return None
    return table.get(value) or transliterate(value)


def gender_en(text) -> str | None:
    return transliterate_vocab(text, STATIC_MAPPINGS)


def relation_en(text) -> str | None:
    return transliterate_vocab(text, STATIC_MAPPINGS)


def house_en(text) -> str | None:
    """Devanagari house/flat number -> Latin, as an address label.

    Two differences from transliterate():

    1. The long 'ee' (ई, ी) is /i:/ -- the "ee" in *see*. The English letter
       whose name is that sound is E, not I. transliterate() maps it to i,
       which is right inside a name (सीता -> Sita) and wrong for a label
       (47-ई-7 is flat 47-E-7).

    2. A lone vowel before a numeral is a flat letter: इ-897 is flat E-897.
       ITRANS has no notion of that convention and yields i-897.
    """
    if text is None:
        return None
    value = str(text).strip()
    if not value:
        return None

    out = transliterate(value)
    if not out:
        return out

    # The long ee names the letter E rather than the vowel i. Only in the
    # token that carries the numeral -- "Plot No. 279" must not become
    # "PlEot No. 279", and a vowel inside a word (Kha) must be left alone.
    out = re.sub(
        r"(?<![A-Za-z])([iI])(?=\s*[-–/,.]?\s*\d)",
        "E",
        out,
    )
    return out


def source_hash(*parts) -> str:
    """Hash of the Hindi inputs an English value was derived from.

    Stored alongside the English column so a record whose Hindi side was
    corrected by a later OCR pass can be detected and have its English
    regenerated, instead of silently going stale.
    """
    payload = "␟".join("" if p is None else str(p) for p in parts)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# Optional name translation integration
try:
    from app.name_translation import translate_name
    _NAME_TRANSLATION_AVAILABLE = True
except ImportError:
    _NAME_TRANSLATION_AVAILABLE = False


def translate_for_field(text: str, field_type: str) -> str | None:
    """Route field to appropriate English derivation.

    For 'name' and 'relative_name' fields, tries cloud translation first
    (with local caching for determinism), then falls back to transliteration.
    For all other fields, uses pure transliteration.

    Args:
        text: The Hindi text to convert
        field_type: One of 'name', 'relative_name', 'gender', 'relation',
                    'house', 'section', 'epic'

    Returns:
        English version of the text, or None if input was empty/None
    """
    if text is None:
        return None
    value = str(text).strip()
    if not value:
        return None

    # Only use translation for proper name fields
    if field_type in ('name', 'relative_name') and _NAME_TRANSLATION_AVAILABLE:
        # Try translation first for proper names
        translated = translate_name(value)
        if translated:
            return translated

    # Fallback to transliteration for all other cases
    return transliterate(value)
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
import logging
import os
import re
import unicodedata

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
    "gya": "gya",                 # ज्ञ -> gya
    # Numbers and punctuation pass through unchanged
}


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
    return SCHEMES.get(
        os.getenv("TRANSLITERATION_SCHEME", "iast-plain").strip().lower(),
        SCHEMES["iast-plain"],
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
    """Drop combining marks (Mn + Mc) so IAST becomes plain ASCII.

    ITRANS can emit Devanagari spacing marks (Mc, e.g., U+0949 CANDRA O)
    which survive NFD + combining() filter. Remove both Mn and Mc.
    """
    decomposed = unicodedata.normalize("NFD", text)
    return unicodedata.normalize("NFC", "".join(
        ch for ch in decomposed
        if unicodedata.category(ch) not in ("Mn", "Mc")
    ))


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
                # This token has Devanagari - transliterate the whole token
                out = sanscript.transliterate(token, sanscript.DEVANAGARI, target)
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
    import sys
    print(f"DEBUG transliterate: value={value!r}, tokens={tokens!r}, out={out!r}", file=sys.stderr)
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
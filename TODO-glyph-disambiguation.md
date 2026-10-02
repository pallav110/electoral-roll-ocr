# TODO: Glyph Disambiguation for Devanagari Transliteration

## Problem
Many Devanagari characters look visually similar but have different transliterations. The current ITRANS-based pipeline may confuse them, leading to incorrect English output.

## Similar-looking character pairs/groups to disambiguate

### Halant / Virama related
- **Halant (्)** vs **Anusvara (ं)** - halant suppresses vowel, anusvara adds nasal
- **Halant + consonant** (conjunct) vs standalone consonant with implicit 'a'

### Consonant pairs that look similar

| Character | Unicode | Transliteration | Confusable with |
|-----------|---------|-----------------|-----------------|
| ह (ha) | U+0939 | ha | त (ta), ल (la) |
| त (ta) | U+0924 | ta | ह (ha), ल (la) |
| ल (la) | U+0932 | la | ह (ha), त (ta) |
| द (da) | U+0926 | da | ह (ha) |
| भ (bha) | U+092D | bha | म (ma) |
| म (ma) | U+092E | ma | भ (bha) |
| घ (gha) | U+0918 | gha | ध (dha) |
| ध (dha) | U+0927 | dha | घ (gha) |
| ज (ja) | U+091C | ja | य (ya) |
| य (ya) | U+092F | ya | ज (ja) |
| ढ (dha) | U+0922 | ḍha | ड (ḍa) |
| ड (ḍa) | U+0921 | ḍa | ढ (ḍha) |
| ठ (ṭha) | U+0920 | ṭha | ट (ṭa) |
| ट (ṭa) | U+091F | ṭa | ठ (ṭha) |
| थ (tha) | U+0925 | tha | त (ta) |
| फ (pha) | U+092B | pha | ब (ba) |
| ब (ba) | U+092C | ba | फ (pha) |
| स (sa) | U+0938 | sa | श (śa), ष (ṣa) |
| श (śa) | U+0936 | śa | स (sa), ष (ṣa) |
| ष (ṣa) | U+0937 | ṣa | स (sa), श (śa) |
| ङ (ṅa) | U+0919 | ṅa | ञ (ña) |
| ञ (ña) | U+091E | ña | ङ (ṅa) |

### Vowel signs (matras) that look similar
- **ा (ā - U+093E)** vs **ि (i - U+093F)** vs **ी (ī - U+0940)** - different vertical positioning
- **ु (u - U+0941)** vs **ू (ū - U+0942)** - short vs long u
- **े (e - U+0947)** vs **ै (ai - U+0948)** - e vs ai
- **ो (o - U+094B)** vs **ौ (au - U+094C)** - o vs au
- **ृ (ṛ - U+0943)** vs **ॄ (ṝ - U+0944)** - rare, vocalic r

### Special combinations
- **क् + ष (kṣa - क्ष)** vs **क् + स (ksa - क्स)** - conjunct formation
- **ज् + ञ (jña - ज्ञ)** vs **ज् + न (jna - ज्न)**
- **द् + द (dda - द्द)** vs **द् + ध (ddha - द्ध)**
- **त् + त (tta - त्त)** vs **त् + थ (ttha - त्थ)**

## Testing approach
1. Create test cases for each confusable pair in context (names, addresses)
2. Verify ITRANS output distinguishes them correctly
3. Add to `GLOBAL_STRUCTURAL_MAP` in `transliterate.py` if needed for common words
4. Test with actual OCR output (which may have recognition errors)

## Files to check/update
- `electoral-pipeline/app/transliterate.py` - main transliteration logic
- `electoral-pipeline/tests/test_transliterate.py` - add test cases

## Priority
High - these directly affect voter name and address accuracy in English output
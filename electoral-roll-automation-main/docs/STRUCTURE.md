# Folder Structure & Naming Convention

This document describes the complete folder structure and naming convention used by the ECI Electoral Roll Downloader.

## Overview

The downloader creates a hierarchical folder structure that mirrors the ECI website's dropdown hierarchy:

```
downloaded_pdfs/
└── {STATE_NAME}/
    └── {YEAR}/
        └── {ROLL_TYPE}/
            └── {DISTRICT}/
                └── {CONSTITUENCY}/
                    └── {LANGUAGE}/
                        └── {FILENAME_FROM_SITE}.zip
```

## Sanitization Rules

All folder names are sanitized by replacing special characters:

| Character | Replacement |
|-----------|-------------|
| Space (` `) | Underscore (`_`) |
| Ampersand (`&`) | `and` |
| Forward slash (`/`) | Underscore (`_`) |
| Hyphen (`-`) | Underscore (`_`) |

**Examples:**

| Original | Sanitized |
| :--- | :--- |
| `NCT OF Delhi` | `NCT_OF_Delhi` |
| `Final Roll - 2025` | `Final_Roll_-_2025` |
| `23 - KAROL BAGH` | `23_-_KAROL_BAGH` |
| `Central North` | `Central_North` |
| `ANDAMAN & NICOBAR ISLANDS` | `ANDAMAN_and_NICOBAR_ISLANDS` |
| `DADRA & NAGAR HAVELI AND DAMAN & DIU` | `DADRA_and_NAGAR_HAVELI_AND_DAMAN_and_DIU` |

## Complete Example: NCT OF Delhi

### Path Construction

For selection:
- **State**: NCT OF Delhi
- **Year**: 2025
- **Roll Type**: Final Roll - 2025
- **District**: Central
- **Constituency**: 23 - KAROL BAGH
- **Language**: ENGLISH

The folder path becomes:

```
downloaded_pdfs/
└── NCT_OF_Delhi/
    └── 2025/
        └── Final_Roll_-_2025/
            └── Central/
                └── 23_-_KAROL_BAGH/
                    └── ENGLISH/
                        └── eroll_23_KAROL_BAGH_ENGLISH.zip
```

### What the ZIP Contains

The downloaded ZIP file (from the site's "Download Selected PDFs" button) contains all individual part PDFs:

```
eroll_23_KAROL_BAGH_ENGLISH.zip
├── Part_1.pdf
├── Part_2.pdf
├── Part_3.pdf
...
└── Part_N.pdf
```

## Other Examples

### Example: Maharashtra - Mumbai

```
downloaded_pdfs/
└── MAHARASHTRA/
    └── 2025/
        └── Final_Roll_-_2025/
            └── Mumbai_City/
                └── 183_-_MALBAR_HILL/
                    └── ENGLISH/
                        └── eroll_183_MALBAR_HILL_ENGLISH.zip
                    └── MARATHI/
                        └── eroll_183_MALBAR_HILL_MARATHI.zip
```

### Example: West Bengal - Kolkata

```
downloaded_pdfs/
└── WEST_BENGAL/
    └── 2026/
        └── DraftRoll_-_2026/
            └── Kolkata/
                └── 160_-_BHABANIPUR/
                    └── ENGLISH/
                        └── eroll_160_BHABANIPUR_ENGLISH.zip
                    └── BENGALI/
                        └── eroll_160_BHABANIPUR_BENGALI.zip
```

### Example: Union Territory with Ampersand

```
downloaded_pdfs/
└── ANDAMAN_and_NICOBAR_ISLANDS/
    └── 2025/
        └── Final_Roll_-_2025/
            └── SOUTH_ANDAMAN/
                └── 1_-_A_and_N_ISLANDS_PARLIAMENTARY_CONSTITUENCY/
                    └── ENGLISH/
                        └── eroll_1_A_and_N_ISLANDS_PARLIAMENTARY_CONSTITUENCY_ENGLISH.zip
                    └── HINDI/
                        └── eroll_1_A_and_N_ISLANDS_PARLIAMENTARY_CONSTITUENCY_HINDI.zip
```

## ZIP Archives Created After Download

After all downloads complete, the `create_organized_zips()` method creates:

### Per-State ZIPs
- `electoral_rolls_NCT_OF_Delhi.zip`
- `electoral_rolls_MAHARASHTRA.zip`
- `electoral_rolls_ANDAMAN_and_NICOBAR_ISLANDS.zip`
- etc.

Each contains all folders for that state:
```
electoral_rolls_NCT_OF_Delhi.zip
├── NCT_OF_Delhi/
│   ├── 2025/
│   │   ├── Final_Roll_-_2025/
│   │   │   ├── Central/
│   │   │   │   ├── 23_-_KAROL_BAGH/
│   │   │   │   │   ├── ENGLISH/
│   │   │   │   │   │   └── eroll_23_KAROL_BAGH_ENGLISH.zip
│   │   │   │   │   └── HINDI/
│   │   │   │   │       └── eroll_23_KAROL_BAGH_HINDI.zip
│   │   │   │   └── ...
```

### Master ZIP
- `electoral_rolls_ALL.zip` - Contains everything from all states

## Download Log (`download_log.json`)

Each download entry contains:

```json
{
  "path": "downloaded_pdfs/NCT_OF_Delhi/2025/Final_Roll_-_2025/Central/23_-_KAROL_BAGH/ENGLISH/eroll_23_KAROL_BAGH_ENGLISH.zip",
  "relative": "NCT_OF_Delhi/2025/Final_Roll_-_2025/Central/23_-_KAROL_BAGH/ENGLISH/eroll_23_KAROL_BAGH_ENGLISH.zip",
  "state": "NCT OF Delhi",
  "year": "2025",
  "roll_type": "Final Roll - 2025",
  "district": "Central",
  "constituency": "23 - KAROL BAGH",
  "language": "ENGLISH",
  "parts_count": 15,
  "parts": ["Part 1", "Part 2", "Part 3", "Part 4", "Part 5", "Part 6", "Part 7", "Part 8", "Part 9", "Part 10", "Part 11", "Part 12", "Part 13", "Part 14", "Part 15"]
}
```

## Scale Estimates

With full coverage:
- **36** States/UTs
- **3** Years (2024, 2025, 2026) - though 2024 often unavailable
- **~4** Roll Types per state/year
- **~13** Districts per state
- **~70** Constituencies per state
- **2-3** Languages per constituency

**Total combinations**: ~740 for Delhi alone; ~25,000+ for all India

The hierarchical structure + ZIP archives make this manageable.
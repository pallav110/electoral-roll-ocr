"""Upload a batch of realistic constituency archives into the pipeline's MinIO.

Simulates what `download_all_parts_for_combination` produces -- the same six-level
folder path, the same `Part_N.pdf` contents inside -- so the MinIO half of the
download tool can be exercised without a browser or a captcha key.

Every ZIP produced here is a distinct constituency, matching
`discovered_structure.json`. Run it, then watch the pipeline ingest them:

    MINIO console   http://localhost:9001   bucket electoral-archives
    pipeline UI     http://localhost:8088

Usage:
    python scripts/demo_upload_loop.py --count 20
"""
import argparse
import importlib.util
import shutil
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Loaded by path rather than as `src.minio_client`: the package __init__ imports
# eci_downloader, which needs playwright and requests just to reach the client.
# This demo exercises the MinIO half only and should not need a browser stack.
_spec = importlib.util.spec_from_file_location(
    "minio_client", Path(__file__).resolve().parent.parent / "src" / "minio_client.py"
)
_mc = importlib.util.module_from_spec(_spec)
sys.modules["minio_client"] = _mc
_spec.loader.exec_module(_mc)
create_minio_client_from_env = _mc.create_minio_client_from_env

SAMPLE = Path(
    "/home/spxlpt133/Desktop/Electoral-Roll/electoral-pipeline/sample-pdfs"
    "/2026-EROLLGEN-S24-53-SIR-FinalRoll-Revision1-HIN-300-WI.pdf"
)

# Real combinations lifted from discovered_structure.json, so the demo exercises
# the same key shapes production would produce: nested districts, a three-language
# constituency, and names needing sanitisation (spaces, hyphens, ampersands).
COMBINATIONS = [
    ("NCT OF Delhi", "2025", "Final Roll - 2025", "Central", "23 - KAROL BAGH", "ENGLISH"),
    ("NCT OF Delhi", "2025", "Final Roll - 2025", "Central", "24 - PATEL NAGAR", "ENGLISH"),
    ("NCT OF Delhi", "2025", "Final Roll - 2025", "Central", "25 - MOTI NAGAR", "HINDI"),
    ("NCT OF Delhi", "2025", "Final Roll - 2025", "Central", "39 - RAJINDER NAGAR", "ENGLISH"),
    ("NCT OF Delhi", "2025", "Final Roll - 2025", "Central North", "14 - SHALIMAR BAGH", "ENGLISH"),
    ("NCT OF Delhi", "2025", "Final Roll - 2025", "Central North", "15 - SHAKUR BASTI", "HINDI"),
    ("NCT OF Delhi", "2025", "Final Roll - 2025", "East", "55 - TRILOKPURI", "ENGLISH"),
    ("NCT OF Delhi", "2025", "Final Roll - 2025", "East", "56 - KONDLI", "ENGLISH"),
    ("NCT OF Delhi", "2025", "Final Roll - 2025", "East", "57 - PATPARGANJ", "HINDI"),
    ("NCT OF Delhi", "2025", "Final Roll - 2025", "East", "58 - LAXMI NAGAR", "ENGLISH"),
    ("NCT OF Delhi", "2025", "Final Roll - 2025", "New Delhi", "38 - DELHI CANTT", "ENGLISH"),
    ("NCT OF Delhi", "2025", "Final Roll - 2025", "New Delhi", "40 - NEW DELHI", "ENGLISH"),
    ("NCT OF Delhi", "2025", "Final Roll - 2025", "North East", "63 - SEEMA PURI", "ENGLISH"),
    ("NCT OF Delhi", "2025", "Final Roll - 2025", "North East", "64 - ROHTAS NAGAR", "URDU"),
    ("NCT OF Delhi", "2025", "Final Roll - 2025", "North West", "6 - RITHALA", "ENGLISH"),
    ("NCT OF Delhi", "2025", "Final Roll - 2025", "Old Delhi", "19 - SADAR BAZAR", "ENGLISH"),
    ("NCT OF Delhi", "2025", "Final Roll - 2025", "Old Delhi", "20 - CHANDNI CHOWK", "HINDI"),
    ("NCT OF Delhi", "2025", "Final Roll - 2025", "South East", "50 - GREATER KAILASH", "ENGLISH"),
    ("NCT OF Delhi", "2026", "SIR DraftRoll - 2026", "West", "31 - VIKASPURI", "ENGLISH"),
    ("NCT OF Delhi", "2026", "SIR DraftRoll - 2026", "West", "32 - UTTAM NAGAR", "ENGLISH"),
]


def sanitize(value):
    """Same replacements the downloader applies to folder names."""
    return (value.replace(" ", "_").replace("&", "and")
                 .replace("/", "_").replace("-", "_"))


def build_archive(state, year, roll, district, constituency, language, root, parts=2):
    folder = (root / sanitize(state) / sanitize(year) / sanitize(roll)
              / sanitize(district) / sanitize(constituency) / sanitize(language))
    folder.mkdir(parents=True, exist_ok=True)

    number = constituency.split(" - ")[0]
    name = f"eroll_{number}_{constituency.split(' - ', 1)[1].replace(' ', '_')}_{language}.zip"
    archive = folder / name

    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zf:
        for part in range(1, parts + 1):
            zf.write(SAMPLE, f"Part_{part}.pdf")
    return archive


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--count", type=int, default=20,
                        help="how many combinations to upload")
    parser.add_argument("--root", default="downloaded_pdfs",
                        help="local folder to build the archives under")
    parser.add_argument("--parts", type=int, default=2,
                        help="PDFs inside each constituency archive")
    args = parser.parse_args()

    if not SAMPLE.exists():
        raise SystemExit(f"Sample PDF not found: {SAMPLE}")

    chosen = COMBINATIONS[: args.count]
    if args.count > len(COMBINATIONS):
        print(f"[!] Only {len(COMBINATIONS)} combinations defined; using all.")

    root = Path(args.root)
    for f in root.rglob("*.zip"):
        f.unlink()

    client = create_minio_client_from_env()
    if not client.is_configured():
        raise SystemExit("MinIO not configured. Copy electoral-pipeline/.env into a local .env.")

    print(f"[*] Building {len(chosen)} constituency archives "
          f"({args.parts} parts each) in {root}/")
    archives = [build_archive(*combo, root, args.parts) for combo in chosen]

    print(f"[*] Uploading to {client.endpoint} bucket={client.bucket}\n")
    ok = 0
    for archive in archives:
        if client.upload_file(archive):
            ok += 1

    print(f"\n[+] {ok}/{len(archives)} uploaded")
    print(f"    MinIO console  http://localhost:9001  (bucket {client.bucket})")
    print(f"    pipeline UI    http://localhost:8088")


if __name__ == "__main__":
    main()
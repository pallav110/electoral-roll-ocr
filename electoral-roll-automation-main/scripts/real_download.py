"""Download real constituency archives from voters.eci.gov.in and upload to MinIO.

Unlike `demo_upload_loop.py`, nothing here is simulated: it drives the live
site, solves the captcha with the NVIDIA vision model, saves the ZIP the site
returns, and pushes it into the pipeline's bucket.

    python scripts/real_download.py --count 3
    python scripts/real_download.py --count 1 --no-upload   # download only

Requires VISION_API_KEY in .env. Without one, every attempt returns MANUAL and
the run aborts before the site is touched.
"""
import argparse
import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Loaded by path so the client's factory is available without importing the
# package __init__, which eagerly pulls in the downloader module.
_spec = importlib.util.spec_from_file_location(
    "minio_client", ROOT / "src" / "minio_client.py"
)
_mc = importlib.util.module_from_spec(_spec)
sys.modules["minio_client"] = _mc
_spec.loader.exec_module(_mc)
create_minio_client_from_env = _mc.create_minio_client_from_env

from src.eci_downloader import ECIDownloader  # noqa: E402


def select_structure(limit):
    """Rebuild discovered_structure.json holding only the first `limit` combos.

    download_all() walks this exact nested shape, so trimming it here limits the
    run without forking the browser lifecycle or the retry logic.
    """
    full = json.loads((ROOT / "discovered_structure.json").read_text(encoding="utf-8"))

    # State code mapping (from live site)
    state_codes = {
        "Andaman & Nicobar Islands": "U01",
        "Andhra Pradesh": "S01",
        "Arunachal Pradesh": "S02",
        "Assam": "S03",
        "Bihar": "S04",
        "Chandigarh": "U02",
        "Chhattisgarh": "S26",
        "Dadra & Nagar Haveli and Daman & Diu": "U03",
        "Goa": "S05",
        "Gujarat": "S06",
        "Haryana": "S07",
        "Himachal Pradesh": "S08",
        "Jammu and Kashmir": "U08",
        "Jharkhand": "S27",
        "Karnataka": "S10",
        "Kerala": "S11",
        "Ladakh": "U09",
        "Lakshadweep": "U06",
        "Madhya Pradesh": "S12",
        "Maharashtra": "S13",
        "Manipur": "S14",
        "Meghalaya": "S15",
        "Mizoram": "S16",
        "Nagaland": "S17",
        "NCT OF Delhi": "U05",
        "Odisha": "S18",
        "Puducherry": "U07",
        "Punjab": "S19",
        "Rajasthan": "S20",
        "Sikkim": "S21",
        "Tamil Nadu": "S22",
        "Telangana": "S29",
        "Tripura": "S23",
        "Uttar Pradesh": "S24",
        "Uttarakhand": "S28",
        "West Bengal": "S25",
    }

    trimmed = {}
    taken = 0

    for state_label, state_data in full.items():
        if taken >= limit:
            break
        state_value = state_codes.get(state_label, state_label)
        state = {"value": state_value, "years": {}}

        for year_label, year_data in state_data.get("years", {}).items():
            for roll_label, roll_data in year_data.get("roll_types", {}).items():
                for dist_label, dist_data in roll_data.get("districts", {}).items():
                    if taken >= limit:
                        break
                    langs = dist_data.get("languages", [])
                    consts = dist_data.get("constituencies", [])
                    if not consts or not langs:
                        continue

                    # Fill this district with as many language variants as the
                    # remaining budget allows, then move on.
                    budget = min(limit - taken, len(consts) * len(langs))
                    kept_consts = consts[: max(1, budget // len(langs))]
                    kept_langs = langs[:budget]
                    if not kept_consts or not kept_langs:
                        continue

                    state["years"].setdefault(year_label, {"roll_types": {}})
                    state["years"][year_label]["roll_types"].setdefault(roll_label, {"districts": {}})
                    state["years"][year_label]["roll_types"][roll_label]["districts"][dist_label] = {
                        "constituencies": kept_consts,
                        "languages": kept_langs,
                    }
                    taken += len(kept_consts) * len(kept_langs)

        if state["years"]:
            trimmed[state_label] = state

    return trimmed


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--count", type=int, default=1,
                        help="how many constituency/language combinations to download")
    parser.add_argument("--no-upload", action="store_true",
                        help="skip the MinIO upload step")
    args = parser.parse_args()

    downloader = ECIDownloader()
    if not downloader.vision_api_key:
        raise SystemExit("[!] No VISION_API_KEY in .env — captcha cannot be solved.")

    structure = select_structure(args.count)
    planned = sum(
        len(d.get("constituencies", [])) * len(d.get("languages", []))
        for s in structure.values()
        for y in s["years"].values()
        for r in y["roll_types"].values()
        for d in r["districts"].values()
    )
    print(f"[*] Queued {planned} combination(s) against the live site")

    # download_all prints its own attempted/successful/failed summary.
    downloader.download_all(structure)

    if downloader.downloaded_files:
        if args.no_upload:
            print("[*] --no-upload given, skipping MinIO")
        else:
            client = create_minio_client_from_env()
            if not client.is_configured():
                print("[!] MinIO not configured; ZIPs are on disk under downloaded_pdfs/")
            else:
                client.upload_downloads(ROOT / "downloaded_pdfs")
    else:
        print("[!] Nothing downloaded — nothing to upload.")


if __name__ == "__main__":
    main()
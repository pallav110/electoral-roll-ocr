"""Identify and remove the simulated archives in the MinIO bucket.

`demo_upload_loop.py` built every ZIP it uploaded from one local sample PDF --
the same two pages, twice, in each archive. The pipeline's ingest polls the
bucket for `.zip` files, so those archives were extracted and turned into
"voters" like any real download would be. They are indistinguishable from real
data by the time they reach the database, which is why they have to go before
the first genuine download lands.

Detection is by content, not by name or path: an archive is treated as
simulated when every PDF inside it hashes to the same value. Real constituency
parts differ from one another, so a genuine archive cannot match this. The
known demo sample is checked too, which catches the case of an archive holding
several copies under different filenames.

Nothing is deleted without --delete. The default is a dry run.

    python scripts/purge_demo_archives.py                    # list what matches
    python scripts/purge_demo_archives.py --delete           # remove them
    python scripts/purge_demo_archives.py --keep-pipeline-drop
"""
import argparse
import hashlib
import importlib.util
import io
import sys
import zipfile
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_spec = importlib.util.spec_from_file_location(
    "minio_client", ROOT / "src" / "minio_client.py"
)
_mc = importlib.util.module_from_spec(_spec)
sys.modules["minio_client"] = _mc
_spec.loader.exec_module(_mc)
create_minio_client_from_env = _mc.create_minio_client_from_env

# The sample the demo loop packed. Comparing against it lets the same archive
# be recognised even when its parts are stored under different filenames.
DEMO_SAMPLE = (
    ROOT.parent / "electoral-pipeline" / "sample-pdfs"
    / "2026-EROLLGEN-S24-53-SIR-FinalRoll-Revision1-HIN-300-WI.pdf"
)


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def classify(key: str, data: bytes):
    """Return ('simulated', reason) / ('real', reason) / ('skip', reason)."""
    if not zipfile.is_zipfile(io.BytesIO(data)):
        return "skip", "not a ZIP"

    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except Exception as exc:
        return "skip", f"unreadable ZIP ({type(exc).__name__})"

    pdfs = [n for n in archive.namelist() if n.lower().endswith(".pdf")]
    if not pdfs:
        return "skip", "no PDFs inside"

    digests = {}
    try:
        for name in pdfs:
            digests[name] = sha256(archive.read(name))
    except Exception as exc:
        return "skip", f"unreadable entry ({type(exc).__name__})"

    sample_hash = sha256(DEMO_SAMPLE.read_bytes()) if DEMO_SAMPLE.exists() else None

    if sample_hash and all(d == sample_hash for d in digests.values()):
        return "simulated", f"every part is the demo sample ({len(pdfs)} copies)"

    if len(set(digests.values())) == 1:
        return "simulated", f"all {len(pdfs)} parts byte-identical"

    if len(set(digests.values())) != len(digests):
        dupes = [h for h, c in Counter(digests.values()).items() if c > 1]
        return "real", f"{len(pdfs)} parts, {len(dupes)} repeated hash(es)"

    return "real", f"{len(pdfs)} distinct parts"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--delete", action="store_true",
                        help="actually remove the simulated archives")
    parser.add_argument("--prefix", default="",
                        help="only consider objects under this key prefix")
    args = parser.parse_args()

    client = create_minio_client_from_env()
    if not client.is_configured():
        raise SystemExit("MinIO not configured.")

    objects = client.list_objects(prefix=args.prefix)
    if not objects:
        print(f"[*] No objects under {client.bucket}/{args.prefix}")
        return

    print(f"[*] Scanning {len(objects)} object(s) in {client.bucket}\n")

    simulated, real, skipped = [], [], []
    for obj in objects:
        if not obj["name"].lower().endswith(".zip"):
            # 3-tuples throughout: a row without a size printed with the wrong
            # columns, and the crash happened on the LAST section -- after the
            # whole verdict had been computed -- so the report looked complete
            # right up to the point it died.
            skipped.append((obj["name"], obj.get("size", 0) / 1024 / 1024,
                            "not a ZIP"))
            continue
        try:
            response = client.client.get_object(client.bucket, obj["name"])
            data = response.read()
            response.close()
            response.release_conn()
        except Exception as exc:
            skipped.append((obj["name"], 0.0,
                            f"unreadable ({type(exc).__name__})"))
            continue

        verdict, reason = classify(obj["name"], data)
        size_mib = len(data) / 1024 / 1024
        row = (obj["name"], size_mib, reason)
        {"simulated": simulated, "real": real, "skip": skipped}[verdict].append(row)

    for label, rows in (("SIMULATED (demo data)", simulated),
                        ("GENUINE (left alone)", real),
                        ("SKIPPED", skipped)):
        if not rows:
            continue
        print(f"--- {label} ---")
        for name, size_mib, reason in rows:
            print(f"  {size_mib:8.2f} MiB  {name}")
            print(f"                          {reason}")
        print()

    freed = sum(size for _, size, _ in simulated)
    print(f"[*] {len(simulated)} simulated, {len(real)} genuine, "
          f"{len(skipped)} skipped; {freed:.1f} MiB would be freed")

    if not simulated:
        print("[*] Nothing to remove.")
        return

    if not args.delete:
        print("[*] Dry run. Re-run with --delete to remove them.")
        return

    print()
    removed = failed = 0
    for name, _, _ in simulated:
        try:
            client.client.remove_object(client.bucket, name)
            print(f"  [-] removed {name}")
            removed += 1
        except Exception as exc:
            print(f"  [!] failed {name}: {type(exc).__name__}")
            failed += 1
    print(f"\n[+] {removed} removed, {failed} failed")


if __name__ == "__main__":
    main()

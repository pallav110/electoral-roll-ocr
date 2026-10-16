"""
MinIO client for the electoral-roll pipeline's archive bucket.

This uploads into the same bucket the pipeline already polls
(`electoral-pipeline/app/archive_ingest.py`): every `.zip` under the bucket
prefix is picked up, extracted, and turned into documents. So the ECI site's own
ZIP is uploaded as-is -- re-packing it only costs time, and the pipeline's
extractor preserves the directory hierarchy the ZIP already has.
"""
import os
from pathlib import Path
from typing import Optional, Dict, Any

from minio import Minio
from minio.error import S3Error
from dotenv import load_dotenv

load_dotenv()


def _endpoint_host_port(raw: str) -> str:
    """Reduce a MinIO endpoint to the ``host:port`` the SDK expects.

    The SDK takes no scheme. The pipeline's config uses ``http://minio:9000``
    while a bare downloader host usually reads ``localhost:9000`` -- both have to
    work, since this runs on the host while the pipeline runs in Docker.
    """
    value = raw.strip()
    for scheme in ("http://", "https://"):
        if value.startswith(scheme):
            value = value[len(scheme):]
            break
    return value.rstrip("/")


class MinIOClient:
    """Upload electoral-roll archives into the pipeline's MinIO bucket."""

    def __init__(
        self,
        endpoint: Optional[str] = None,
        access_key: Optional[str] = None,
        secret_key: Optional[str] = None,
        bucket: Optional[str] = None,
        secure: Optional[bool] = None,
    ):
        # Defaults mirror electoral-pipeline/app/config.py so an unattended run
        # lands in the bucket the pipeline is already watching.
        self.endpoint = _endpoint_host_port(
            endpoint or os.environ.get("MINIO_ENDPOINT", "http://localhost:9000")
        )
        self.access_key = access_key or os.environ.get("MINIO_ACCESS_KEY") or os.environ.get("MINIO_ROOT_USER")
        self.secret_key = secret_key or os.environ.get("MINIO_SECRET_KEY") or os.environ.get("MINIO_ROOT_PASSWORD")
        self.bucket = bucket or os.environ.get("MINIO_BUCKET", "electoral-archives")
        self.prefix = (os.environ.get("MINIO_PREFIX", "") or "").strip("/")

        if secure is None:
            # TLS follows the endpoint the pipeline was given: an `https://`
            # endpoint in MINIO_ENDPOINT means TLS even if this strips the scheme.
            secure = os.environ.get("MINIO_SECURE", "").lower() in {"1", "true", "yes"}
            if secure is False:
                secure = os.environ.get("MINIO_ENDPOINT", "").startswith("https://")
        self.secure = secure

        self.client: Optional[Minio] = None
        if self.access_key and self.secret_key:
            self._initialize_client()
        else:
            print("[!] MinIO credentials not set (MINIO_ACCESS_KEY / MINIO_SECRET_KEY).")

    def _initialize_client(self):
        try:
            self.client = Minio(
                self.endpoint,
                access_key=self.access_key,
                secret_key=self.secret_key,
                secure=self.secure,
            )
            if not self.client.bucket_exists(self.bucket):
                self.client.make_bucket(self.bucket)
                print(f"[+] Created bucket: {self.bucket}")
            else:
                print(f"[+] Using existing bucket: {self.bucket}")
        except S3Error as exc:
            print(f"[!] MinIO initialization error: {exc}")
            self.client = None
        except Exception as exc:  # unreachable host, bad DNS, wrong scheme
            print(f"[!] Could not reach MinIO at {self.endpoint}: {exc}")
            self.client = None

    def is_configured(self) -> bool:
        return self.client is not None

    def object_name_for(self, file_path: Path) -> str:
        """Map a downloaded ZIP to its key in the bucket.

        The ZIP lives at ``downloaded_pdfs/<STATE>/<YEAR>/<ROLL>/<DIST>/<CONST>/<LANG>/<file>.zip``;
        the hierarchy is kept under the bucket prefix so a constituency's parts
        stay identifiable after extraction. Falls back to the bare filename for
        anything outside that tree.
        """
        parts = list(file_path.parts)
        if OUTPUT_FOLDER in parts:
            parts = parts[parts.index(OUTPUT_FOLDER) + 1:]
        else:
            parts = [file_path.name]
        # The state folder varies (S01 -> NCT_OF_Delhi was the only shape ever
        # exercised), so it is taken from the path rather than re-spelled here.
        # Spelling it out meant an Andhra Pradesh roll uploaded under Delhi's
        # prefix and overwrote a real object with the same constituency number.
        return "/".join(([self.prefix] if self.prefix else []) + parts)

    def upload_file(self, file_path: Path, object_name: Optional[str] = None, bucket: Optional[str] = None) -> bool:
        if not self.is_configured():
            print("[!] MinIO client not configured")
            return False
        if not file_path.exists():
            print(f"[!] File not found: {file_path}")
            return False

        bucket = bucket or self.bucket
        object_name = object_name or self.object_name_for(file_path)
        try:
            self.client.fput_object(
                bucket,
                object_name,
                str(file_path),
                content_type=self._get_content_type(file_path),
            )
            print(f"[+] Uploaded: {file_path.name} -> s3://{bucket}/{object_name}")
            return True
        except S3Error as exc:
            print(f"[!] Upload failed for {file_path.name}: {exc}")
            return False

    def upload_directory(
        self,
        directory: Path,
        bucket: Optional[str] = None,
        extensions: Optional[list] = None,
    ) -> Dict[str, int]:
        """Upload every matching file under ``directory``, hierarchy preserved."""
        if not self.is_configured():
            return {"uploaded": 0, "failed": 0}
        if not directory.exists():
            print(f"[!] Directory not found: {directory}")
            return {"uploaded": 0, "failed": 0}

        bucket = bucket or self.bucket
        uploaded = failed = 0
        for file_path in sorted(directory.rglob("*")):
            if not file_path.is_file():
                continue
            if extensions and file_path.suffix.lower() not in extensions:
                continue
            if self.upload_file(file_path, bucket=bucket):
                uploaded += 1
            else:
                failed += 1
        print(f"[+] Directory upload complete: {uploaded} uploaded, {failed} failed")
        return {"uploaded": uploaded, "failed": failed}

    def upload_download_log(self, log_path: Path, bucket: Optional[str] = None) -> bool:
        return self.upload_file(log_path, "download_log.json", bucket or self.bucket)

    def upload_downloads(self, output_folder: Optional[Path] = None, include_log: bool = True) -> Dict[str, Any]:
        """Push everything this downloader produced into the pipeline's bucket.

        Only ZIPs are uploaded. The pipeline's extractor wants ZIPs, and the
        per-state / master archives built by ``create_organized_zips`` would
        duplicate every part a second time inside nested ZIPs.
        """
        folder = Path(output_folder or OUTPUT_FOLDER)
        results = {"uploaded": 0, "failed": 0, "bucket": self.bucket, "endpoint": self.endpoint}
        if not self.is_configured():
            return results
        results.update(self.upload_directory(folder, extensions=[".zip"]))
        if include_log and Path(DOWNLOAD_LOG).exists():
            results["log"] = self.upload_download_log(Path(DOWNLOAD_LOG))
        return results

    def list_objects(self, prefix: str = "", bucket: Optional[str] = None) -> list:
        if not self.is_configured():
            return []
        objects = []
        try:
            for obj in self.client.list_objects(bucket or self.bucket, prefix=prefix, recursive=True):
                objects.append(
                    {
                        "name": obj.object_name,
                        "size": obj.size,
                        "last_modified": obj.last_modified.isoformat() if obj.last_modified else None,
                        "etag": obj.etag,
                    }
                )
        except S3Error as exc:
            print(f"[!] List objects failed: {exc}")
        return objects

    def _get_content_type(self, file_path: Path) -> str:
        return {
            ".pdf": "application/pdf",
            ".zip": "application/zip",
            ".json": "application/json",
        }.get(file_path.suffix.lower(), "application/octet-stream")


OUTPUT_FOLDER = "downloaded_pdfs"
DOWNLOAD_LOG = "download_log.json"


def create_minio_client_from_env() -> MinIOClient:
    return MinIOClient()


if __name__ == "__main__":
    client = create_minio_client_from_env()
    if client.is_configured():
        print(f"[+] Connected to {client.endpoint} (bucket={client.bucket})")
        objects = client.list_objects()
        print(f"[*] Objects in bucket: {len(objects)}")
        for obj in objects[:10]:
            print(f"  - {obj['name']} ({obj['size']} bytes)")
    else:
        print("[!] MinIO not configured. Set MINIO_ENDPOINT, MINIO_ACCESS_KEY, MINIO_SECRET_KEY.")
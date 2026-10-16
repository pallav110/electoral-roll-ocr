# MinIO Integration Guide

This document describes how to configure and use MinIO object storage with the ECI Electoral Roll Downloader.

## Overview

The MinIO client (`src/minio_client.py`) provides functionality to upload downloaded PDFs, ZIP archives, and the download log to a MinIO (S3-compatible) object storage bucket.

## Prerequisites

- Running MinIO instance (local or remote)
- MinIO credentials (access key + secret key)
- Bucket created (or auto-create enabled)

## Configuration

### Environment Variables (.env)

```bash
# MinIO Configuration
MINIO_ENDPOINT=localhost:9000          # MinIO server endpoint
MINIO_ACCESS_KEY=your_access_key       # Access key (username)
MINIO_SECRET_KEY=your_secret_key       # Secret key (password)
MINIO_BUCKET=electoral-rolls           # Bucket name
MINIO_SECURE=false                     # Use HTTPS (true) or HTTP (false)
```

### Example .env

```bash
# ECI Electoral Roll Downloader Environment Configuration

# NVIDIA Vision API Key for captcha solving
# Get from: https://ngc.nvidia.com/
VISION_API_KEY=your_nvidia_api_key_here

# MinIO Configuration
# Set these if you want to upload downloads to MinIO object storage
MINIO_ENDPOINT=localhost:9000
MINIO_ACCESS_KEY=minioadmin
MINIO_SECRET_KEY=minioadmin
MINIO_BUCKET=electoral-rolls
MINIO_SECURE=false  # Set to true for HTTPS
```

## Quick Start with Docker

```bash
# Start MinIO with Docker
docker run -d \
  --name minio \
  -p 9000:9000 \
  -p 9001:9001 \
  -e "MINIO_ROOT_USER=minioadmin" \
  -e "MINIO_ROOT_PASSWORD=minioadmin" \
  minio/minio server /data --console-address ":9001"

# Access MinIO Console: http://localhost:9001
# Username: minioadmin
# Password: minioadmin
```

## Usage

### 1. Single Command: Download + Upload

```bash
# Download and upload to MinIO in one command
python src/eci_downloader.py --download --upload-to-minio
```

### 2. Separate Steps: Download Then Upload

```bash
# Step 1: Download
python src/eci_downloader.py --download --state "NCT OF Delhi" --year 2025

# Step 2: Upload existing downloads
python src/eci_downloader.py --upload-to-minio
```

### 3. Programmatic Usage

```python
from src.minio_client import MinIOClient
from pathlib import Path

# Create client from environment
client = MinIOClient()

if client.is_configured():
    # Upload everything in organized structure
    results = client.upload_all_organized(
        output_folder=Path("downloaded_pdfs"),
        zip_files=[
            Path("electoral_rolls_ALL.zip"),
            Path("electoral_rolls_NCT_OF_Delhi.zip")
        ]
    )
    
    print(f"PDFs: {results['pdfs']['uploaded']} uploaded, {results['pdfs']['failed']} failed")
    print(f"ZIPs: {results['zips']['uploaded']} uploaded, {results['zips']['failed']} failed")
    print(f"Log: {'uploaded' if results['log'] else 'failed'}")
else:
    print("MinIO not configured. Check environment variables.")
```

## MinIO Organization

Files are uploaded with the following structure:

```
electoral-rolls/              # bucket
├── pdfs/                     # Individual PDF/ZIP files preserving folder hierarchy
│   └── {STATE}/
│       └── {YEAR}/
│           └── {ROLL_TYPE}/
│               └── {DISTRICT}/
│                   └── {CONSTITUENCY}/
│                       └── {LANGUAGE}/
│                           └── {FILENAME}.zip
├── archives/                 # Organized ZIP archives
│   ├── electoral_rolls_ALL.zip
│   ├── electoral_rolls_NCT_OF_Delhi.zip
│   ├── electoral_rolls_MAHARASHTRA.zip
│   └── ...
└── download_log.json         # Download metadata
```

## MinIO Client Methods

### `upload_file(file_path, object_name=None, bucket=None)`
Upload a single file.

```python
client.upload_file(
    Path("downloaded_pdfs/NCT_OF_Delhi/2025/.../file.zip"),
    object_name="custom/path/file.zip"
)
```

### `upload_directory(directory, prefix="", bucket=None, extensions=None)`
Upload all files in a directory.

```python
# Upload only PDFs with a prefix
client.upload_directory(
    Path("downloaded_pdfs"),
    prefix="backups/",
    extensions=[".pdf", ".zip"]
)
```

### `upload_download_log(log_path, bucket=None)`
Upload the download log.

```python
client.upload_download_log(Path("download_log.json"))
```

### `upload_all_organized(output_folder, zip_files, bucket=None)`
Complete upload of PDFs, ZIPs, and log.

```python
results = client.upload_all_organized(
    output_folder=Path("downloaded_pdfs"),
    zip_files=[Path("electoral_rolls_ALL.zip"), Path("electoral_rolls_NCT_OF_Delhi.zip")]
)
```

### `list_objects(prefix="", bucket=None)`
List objects in bucket.

```python
objects = client.list_objects(prefix="pdfs/NCT_OF_Delhi/")
for obj in objects:
    print(f"{obj['name']} - {obj['size']} bytes - {obj['last_modified']}")
```

### `download_object(object_name, file_path, bucket=None)`
Download an object from MinIO.

```python
client.download_object(
    "archives/electoral_rolls_ALL.zip",
    Path("downloaded_electoral_rolls_ALL.zip")
)
```

## Testing Connection

```bash
# Test MinIO configuration
python -c "
from src.minio_client import create_minio_client_from_env
client = create_minio_client_from_env()
if client.is_configured():
    print('MinIO configured successfully')
    objects = client.list_objects()
    print(f'Objects in bucket: {len(objects)}')
else:
    print('MinIO not configured. Check .env file.')
"
```

## Bucket Policies (Optional)

### Public Read Access (for sharing)

```bash
# Create public read policy
mc alias set local http://localhost:9000 minioadmin minioadmin
mc anonymous set public local/electoral-rolls/archives
mc anonymous set public local/electoral-rolls/pdfs
```

### Private Access (default)

All objects are private by default. Generate presigned URLs for temporary access:

```python
from minio import Minio
from datetime import timedelta

client = Minio(
    "localhost:9000",
    access_key="minioadmin",
    secret_key="minioadmin",
    secure=False
)

# Generate presigned URL valid for 1 hour
url = client.presigned_get_object(
    "electoral-rolls",
    "archives/electoral_rolls_ALL.zip",
    expires=timedelta(hours=1)
)
print(url)
```

## Troubleshooting

### Connection Refused
- Ensure MinIO is running: `docker ps | grep minio`
- Check endpoint in .env matches running instance

### Authentication Failed
- Verify ACCESS_KEY and SECRET_KEY in .env
- Check MinIO console: http://localhost:9001

### Bucket Not Found
- Auto-create is enabled in client
- Or create manually: `mc mb local/electoral-rolls`

### Upload Timeout
- Large files may need longer timeout
- Check network connectivity to MinIO endpoint

## Best Practices

1. **Use separate buckets** for different environments (dev/staging/prod)
2. **Enable versioning** on bucket for data protection
3. **Set lifecycle policies** to auto-delete old files
4. **Monitor bucket size** to avoid unexpected costs
5. **Use HTTPS in production** (set MINIO_SECURE=true)

## Advanced: Custom Endpoint (S3-Compatible)

Works with any S3-compatible storage:

```bash
# AWS S3
MINIO_ENDPOINT=s3.amazonaws.com
MINIO_ACCESS_KEY=your_aws_key
MINIO_SECRET_KEY=your_aws_secret
MINIO_BUCKET=your-bucket
MINIO_SECURE=true

# DigitalOcean Spaces
MINIO_ENDPOINT=nyc3.digitaloceanspaces.com
MINIO_ACCESS_KEY=your_do_key
MINIO_SECRET_KEY=your_do_secret
MINIO_BUCKET=your-space
MINIO_SECURE=true
```
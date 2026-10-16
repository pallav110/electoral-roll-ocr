# ECI Electoral Roll Downloader API Documentation

## Overview

The ECI Electoral Roll Downloader API provides programmatic access to discover electoral roll structures and trigger downloads from the Election Commission of India website.

**Base URL**: `http://localhost:8000`

## Authentication

Currently, the API does not require authentication for read operations. For download operations, you can optionally provide a vision API key in the request body for automated captcha solving.

## Endpoints

### Health Check

```http
GET /health
```

**Response**:
```json
{
  "status": "healthy",
  "service": "ECI Electoral Roll Downloader"
}
```

---

### List All States

```http
GET /states
```

**Response**:
```json
{
  "states": [
    "ANDAMAN & NICOBAR ISLANDS",
    "ANDHRA PRADESH",
    "ARUNACHAL PRADESH",
    "ASSAM",
    "BIHAR",
    "CHANDIGARH",
    "CHHATTISGARH",
    "DADRA & NAGAR HAVELI AND DAMAN & DIU",
    "GOA",
    "GUJARAT",
    "HARYANA",
    "HIMACHAL PRADESH",
    "JAMMU & KASHMIR",
    "JHARKHAND",
    "KARNATAKA",
    "KERALA",
    "LADAKH",
    "LAKSHADWEEP",
    "MADHYA PRADESH",
    "MAHARASHTRA",
    "MANIPUR",
    "MEGHALAYA",
    "MIZORAM",
    "NAGALAND",
    "NCT OF DELHI",
    "ODISHA",
    "PUDUCHERRY",
    "PUNJAB",
    "RAJASTHAN",
    "SIKKIM",
    "TAMIL NADU",
    "TELANGANA",
    "TRIPURA",
    "UTTAR PRADESH",
    "UTTARAKHAND",
    "WEST BENGAL"
  ]
}
```

---

### List Years for a State

```http
GET /states/{state}/years
```

**Path Parameters**:
- `state` (string): State label (e.g., "NCT OF Delhi")

**Response**:
```json
{
  "years": ["2026", "2025"]
}
```

**Errors**:
- `404`: State not found

---

### List Roll Types for State/Year

```http
GET /states/{state}/years/{year}/roll-types
```

**Path Parameters**:
- `state` (string): State label
- `year` (string): Year of revision (e.g., "2025")

**Response**:
```json
{
  "roll_types": [
    "Supplement-5 2025",
    "State General Election Roll 2025",
    "DraftRoll - 2025",
    "Final Roll - 2025"
  ]
}
```

**Errors**:
- `404`: State or year not found

---

### List Districts for State/Year/Roll Type

```http
GET /states/{state}/years/{year}/roll-types/{roll_type}/districts
```

**Path Parameters**:
- `state` (string): State label
- `year` (string): Year
- `roll_type` (string): Roll type label

**Response**:
```json
{
  "districts": [
    "Central",
    "Central North",
    "East",
    "New Delhi",
    "North",
    "North East",
    "North West",
    "Old Delhi",
    "Outer North",
    "South",
    "South East",
    "South West",
    "West"
  ]
}
```

**Errors**:
- `404`: Path not found

---

### List Constituencies for District

```http
GET /states/{state}/years/{year}/roll-types/{roll_type}/districts/{district}/constituencies
```

**Path Parameters**:
- `state` (string): State label
- `year` (string): Year
- `roll_type` (string): Roll type label
- `district` (string): District label

**Response**:
```json
{
  "constituencies": [
    {"value": "23", "label": "23 - KAROL BAGH"},
    {"value": "24", "label": "24 - PATEL NAGAR"},
    {"value": "25", "label": "25 - MOTI NAGAR"},
    {"value": "39", "label": "39 - RAJINDER NAGAR"}
  ]
}
```

**Errors**:
- `404`: Path not found

---

### List Languages for Constituency

```http
GET /states/{state}/years/{year}/roll-types/{roll_type}/districts/{district}/constituencies/{constituency}/languages
```

**Path Parameters**:
- `state` (string): State label
- `year` (string): Year
- `roll_type` (string): Roll type label
- `district` (string): District label
- `constituency` (string): Constituency label (e.g., "23 - KAROL BAGH")

**Response**:
```json
{
  "languages": [
    {"value": "ENG", "label": "ENGLISH"},
    {"value": "HIN", "label": "HINDI"}
  ]
}
```

**Errors**:
- `404`: Path not found

---

### Trigger Download

```http
POST /download
```

**Request Body**:
```json
{
  "state": "NCT OF Delhi",
  "year": "2025",
  "roll_type": "Final Roll - 2025",
  "district": "Central",
  "constituency": "23 - KAROL BAGH",
  "language": "ENGLISH",
  "vision_api_key": "optional_nvidia_api_key"
}
```

**Response**:
```json
{
  "success": true,
  "message": "Download started in background. Check /download/log for progress.",
  "file_path": null,
  "parts_count": 0,
  "parts": []
}
```

**Notes**:
- Download runs in background to avoid blocking the API
- Check `/download/log` for progress
- If `vision_api_key` is not provided, uses the server's default (from `VISION_API_KEY` env var)
- Without a vision API key, captcha requires manual solving (download will fail)

---

### Get Download Log

```http
GET /download/log
```

**Response**:
```json
[
  {
    "path": "downloaded_pdfs/NCT_OF_DELHI/2025/Final_Roll_-_2025/Central/23_-_KAROL_BAGH/ENGLISH/part1.pdf",
    "relative": "NCT_OF_DELHI/2025/Final_Roll_-_2025/Central/23_-_KAROL_BAGH/ENGLISH/part1.pdf",
    "state": "NCT OF Delhi",
    "year": "2025",
    "roll_type": "Final Roll - 2025",
    "district": "Central",
    "constituency": "23 - KAROL BAGH",
    "language": "ENGLISH",
    "parts_count": 10,
    "parts": ["Part 1", "Part 2", "Part 3", "Part 4", "Part 5", "Part 6", "Part 7", "Part 8", "Part 9", "Part 10"]
  }
]
```

---

### Download State ZIP

```http
GET /download/zip/{state}
```

**Path Parameters**:
- `state` (string): State label

**Response**: ZIP file download

**Errors**:
- `404`: ZIP not found (run download first)

---

### Download Master ZIP (All States)

```http
GET /download/zip/all
```

**Response**: ZIP file download

**Errors**:
- `404`: Master ZIP not found (run download first)

---

## Example Usage

### Discover Structure and Download (Python)

```python
import requests

BASE_URL = "http://localhost:8000"

# 1. Get all states
states = requests.get(f"{BASE_URL}/states").json()["states"]
print("Available states:", states)

# 2. Get years for a state
years = requests.get(f"{BASE_URL}/states/NCT OF Delhi/years").json()["years"]
print("Years for Delhi:", years)

# 3. Get roll types
roll_types = requests.get(f"{BASE_URL}/states/NCT OF Delhi/years/2025/roll-types").json()["roll_types"]
print("Roll types:", roll_types)

# 4. Get districts
districts = requests.get(f"{BASE_URL}/states/NCT OF Delhi/years/2025/roll-types/Final Roll - 2025/districts").json()["districts"]
print("Districts:", districts)

# 5. Get constituencies for a district
constituencies = requests.get(f"{BASE_URL}/states/NCT OF Delhi/years/2025/roll-types/Final Roll - 2025/districts/Central/constituencies").json()["constituencies"]
print("Constituencies:", constituencies)

# 6. Get languages
languages = requests.get(f"{BASE_URL}/states/NCT OF Delhi/years/2025/roll-types/Final Roll - 2025/districts/Central/constituencies/23 - KAROL BAGH/languages").json()["languages"]
print("Languages:", languages)

# 7. Trigger download
download_response = requests.post(f"{BASE_URL}/download", json={
    "state": "NCT OF Delhi",
    "year": "2025",
    "roll_type": "Final Roll - 2025",
    "district": "Central",
    "constituency": "23 - KAROL BAGH",
    "language": "ENGLISH"
})
print("Download triggered:", download_response.json())

# 8. Check log
import time
time.sleep(30)  # Wait for background download
log = requests.get(f"{BASE_URL}/download/log").json()
print("Download log:", log)
```

### Using curl

```bash
# List states
curl http://localhost:8000/states

# List years for Delhi
curl "http://localhost:8000/states/NCT%20OF%20Delhi/years"

# Trigger download
curl -X POST http://localhost:8000/download \
  -H "Content-Type: application/json" \
  -d '{
    "state": "NCT OF Delhi",
    "year": "2025",
    "roll_type": "Final Roll - 2025",
    "district": "Central",
    "constituency": "23 - KAROL BAGH",
    "language": "ENGLISH"
  }'

# Get download log
curl http://localhost:8000/download/log

# Download state ZIP
curl -O http://localhost:8000/download/zip/NCT%20OF%20Delhi

# Download master ZIP
curl -O http://localhost:8000/download/zip/all
```

---

## Error Responses

All endpoints may return standard HTTP error codes:

| Code | Description |
|------|-------------|
| 200 | Success |
| 400 | Bad Request - Invalid parameters |
| 404 | Not Found - Resource doesn't exist |
| 500 | Internal Server Error |
| 503 | Service Unavailable - Downloader not initialized |

Error format:
```json
{
  "detail": "Error description"
}
```

---

## Rate Limiting

The API does not implement rate limiting, but the underlying ECI website may rate-limit aggressive requests. The downloader includes built-in delays (3 seconds between requests) to be respectful.

---

## Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `VISION_API_KEY` | NVIDIA API key for captcha solving | None |
| `MINIO_ENDPOINT` | MinIO endpoint | localhost:9000 |
| `MINIO_ACCESS_KEY` | MinIO access key | None |
| `MINIO_SECRET_KEY` | MinIO secret key | None |
| `MINIO_BUCKET` | MinIO bucket name | electoral-rolls |

---

## Running the API Server

```bash
# Install dependencies
pip install -r requirements.txt

# Start server
uvicorn src.api:app --host 0.0.0.0 --port 8000 --reload

# Or with custom host/port
uvicorn src.api:app --host 127.0.0.1 --port 8080
```

The API will be available at `http://localhost:8000` with automatic OpenAPI documentation at `http://localhost:8000/docs`.
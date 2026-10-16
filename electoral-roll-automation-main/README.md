# ECI Electoral Roll Downloader

A professional Python service for automating the download of electoral roll PDFs from the Election Commission of India (ECI) website: <https://voters.eci.gov.in/download-eroll>

## Features

- **Automated Form Navigation**: Handles 6 chained dropdowns (State → Year → Roll Type → District → Assembly Constituency → Language)
- **Captcha Solving**: Integrates with vision models (NVIDIA API with llama-3.2-11b-vision-instruct) for automated captcha solving
- **Pagination Handling**: Navigates through paginated tables (10 PDFs per page) - goes to last page and iterates backward
- **Bulk Download**: Selects all parts on all pages and downloads via "Download Selected PDFs" button
- **Organized Output**: Creates structured folder hierarchy and per-state ZIP archives + master ZIP
- **Download Logging**: Comprehensive JSON log of all downloads with metadata
- **MinIO Integration**: Ready for object storage upload with single-command option
- **API Endpoint**: FastAPI-based REST API for programmatic access

## Repository Structure

```text
PdfZipperMiniO/
├── src/
│   ├── __init__.py           # Package exports
│   ├── eci_downloader.py     # Main downloader implementation
│   ├── minio_client.py       # MinIO client for object storage
│   └── api.py                # FastAPI REST API
├── scripts/
│   ├── discover_all.py       # Discovery script (standalone)
│   ├── eci_downloader.py     # Legacy version
│   ├── eci_downloader_comprehensive.py  # Extended version
│   └── update_delhi_structure.py
├── tests/
│   ├── test_structure.py     # Main test script
│   ├── check_*.py            # Various test/debug scripts
├── debug/
│   ├── debug_*.py            # Debug scripts for pagination, tables, etc.
├── docs/
│   ├── API.md                # API documentation
│   ├── STRUCTURE.md          # Folder structure & naming convention
│   └── MINIO.md              # MinIO integration guide
├── downloaded_pdfs/          # Output folder for downloaded PDFs
├── venv/                     # Python 3.12 virtual environment
├── discovered_structure.json # Cached dropdown structure
├── download_log.json         # Download history log
├── requirements.txt          # Python dependencies
├── captcha.png               # Captcha image (temporary)
└── README.md                 # This file
```

## Installation

### Prerequisites

- Python 3.12+
- Playwright browsers

### Setup

```bash
# Clone/navigate to the repository
cd PdfZipperMiniO

# Create virtual environment (Python 3.12)
py -3.12 -m venv venv

# Activate virtual environment
# Windows:
venv\Scripts\activate
# Linux/Mac:
source venv/bin/activate

# Install dependencies
pip install -r requirements.txt

# Install Playwright browsers
playwright install chromium
```

## Usage

### Command Line Interface

```bash
# Discover structure only (no downloads)
python src/eci_downloader.py --discover

# Download with limits (for testing - 1 per category)
python src/eci_downloader.py --download --max-per-category 1

# Download specific state and year
python src/eci_downloader.py --download --state "NCT OF Delhi" --year 2025

# Full download (all states, all years, all combinations)
python src/eci_downloader.py --download

# With vision API key for automated captcha solving
python src/eci_downloader.py --download --vision-key YOUR_NVIDIA_API_KEY

# Download AND upload to MinIO in one command
python src/eci_downloader.py --download --upload-to-minio

# Upload existing downloads to MinIO only
python src/eci_downloader.py --upload-to-minio
```

### Programmatic Usage

```python
from src.eci_downloader import ECIDownloader

downloader = ECIDownloader(vision_api_key="your_nvidia_api_key")

# Discover all options
structure = downloader.discover_all_options()

# Download with filters
downloader.download_all(structure, max_per_category=5, target_state="NCT OF Delhi", target_year="2025")

# Upload to MinIO
downloader.upload_to_minio()
```

## Vision Model Integration

The service uses the NVIDIA API with the model `nvidia/meta/llama-3.2-11b-vision-instruct` for captcha solving.

1. Get an API key from [NVIDIA NGC](https://ngc.nvidia.com/)
2. Pass it via `--vision-key` argument or set the `VISION_API_KEY` environment variable

**Note**: If no API key is provided, the service falls back to manual captcha entry (prompts in console).

## Folder Structure & Naming Convention

For complete documentation on folder structure, naming conventions, and examples, see **[docs/STRUCTURE.md](docs/STRUCTURE.md)**.

### Complete Example for Delhi

If you download for **NCT OF Delhi → 2025 → Final Roll - 2025 → Central → 23 - KAROL BAGH → ENGLISH**, the folder structure would be:

```
downloaded_pdfs/
└── NCT_OF_Delhi/
    └── 2025/
        └── Final_Roll_-_2025/
            └── Central/
                └── 23_-_KAROL_BAGH/
                    └── ENGLISH/
                        └── eroll_23_KAROL_BAGH_ENGLISH.zip   (filename from site's Content-Disposition)
```

### Sanitization Rules

The code replaces special characters in folder names:

| Original | Sanitized |
| :--- | :--- |
| `NCT OF Delhi` | `NCT_OF_Delhi` |
| `Final Roll - 2025` | `Final_Roll_-_2025` |
| `23 - KAROL BAGH` | `23_-_KAROL_BAGH` |
| `Central North` | `Central_North` |
| `ANDAMAN & NICOBAR ISLANDS` | `ANDAMAN_and_NICOBAR_ISLANDS` |

**Replacements**: spaces → `_`, `&` → `and`, `/` → `_`, `-` → `_`

### What Gets Downloaded

The site's "Download Selected PDFs" button returns **a single ZIP file** containing all the individual part PDFs (Part 1, Part 2, etc.) for that constituency/language combination. The filename comes from the site's `Content-Disposition` header (typically like `eroll_23_KAROL_BAGH_ENGLISH.zip`).

### After Download: Organized ZIPs

The `create_organized_zips()` method creates:

1. **Per-state ZIP**: `electoral_rolls_NCT_OF_Delhi.zip` (contains all folders for that state)
2. **Master ZIP**: `electoral_rolls_ALL.zip` (contains everything from all states)

### Download Log (`download_log.json`)

Tracks each download with full metadata:

```json
{
  "path": "downloaded_pdfs/NCT_OF_Delhi/2025/Final_Roll_-_2025/Central/23_-_KAROL_BAGH/ENGLISH/eroll.zip",
  "relative": "NCT_OF_Delhi/2025/Final_Roll_-_2025/Central/23_-_KAROL_BAGH/ENGLISH/eroll.zip",
  "state": "NCT OF Delhi",
  "year": "2025",
  "roll_type": "Final Roll - 2025",
  "district": "Central",
  "constituency": "23 - KAROL BAGH",
  "language": "ENGLISH",
  "parts_count": 15,
  "parts": ["Part 1", "Part 2", "Part 3", ...]
}
```

### For All States

With 36 states, multiple years (2024-2026), multiple roll types per year, ~13 districts per state, ~70 constituencies per state, and 2-3 languages each — you'd get a deep but well-organized hierarchy that the per-state and master ZIPs make easy to manage.

## MinIO Integration

For complete MinIO integration guide, see **[docs/MINIO.md](docs/MINIO.md)**.

### Configuration

Configure MinIO credentials via environment variables in `.env`:

```bash
MINIO_ENDPOINT=localhost:9000
MINIO_ACCESS_KEY=your_access_key
MINIO_SECRET_KEY=your_secret_key
MINIO_BUCKET=electoral-rolls
MINIO_SECURE=false  # Set to true for HTTPS
```

### Single-Command Upload

After downloading, upload everything to MinIO with one flag:

```bash
# Download and upload in one go
python src/eci_downloader.py --download --upload-to-minio
```

### Programmatic Upload

```python
from src.minio_client import MinIOClient
from pathlib import Path

client = MinIOClient()
results = client.upload_all_organized(
    output_folder=Path("downloaded_pdfs"),
    zip_files=[Path("electoral_rolls_ALL.zip"), Path("electoral_rolls_NCT_OF_Delhi.zip")]
)
print(results)
```

### MinIO Organization

In your MinIO bucket, files are organized as:

```text
electoral-rolls/
├── pdfs/
│   └── NCT_OF_Delhi/
│       └── 2025/
│           └── Final_Roll_-_2025/
│               └── Central/
│                   └── 23_-_KAROL_BAGH/
│                       └── ENGLISH/
│                           └── eroll_23_KAROL_BAGH_ENGLISH.zip
├── archives/
│   ├── electoral_rolls_ALL.zip
│   ├── electoral_rolls_NCT_OF_Delhi.zip
│   └── ...
└── download_log.json
```

You can browse/download individual PDFs or the ZIP archives directly from the MinIO web UI.

## API Endpoint

Start the FastAPI server:

```bash
uvicorn src.api:app --host 0.0.0.0 --port 8000 --reload
```

### API Endpoints

- `GET /health` - Health check
- `GET /states` - List all available states
- `GET /states/{state}/years` - List years for a state
- `GET /states/{state}/years/{year}/roll-types` - List roll types
- `GET /states/{state}/years/{year}/roll-types/{roll_type}/districts` - List districts
- `GET /states/{state}/years/{year}/roll-types/{roll_type}/districts/{district}/constituencies` - List constituencies
- `GET /states/{state}/years/{year}/roll-types/{roll_type}/districts/{district}/constituencies/{constituency}/languages` - List languages
- `POST /download` - Trigger download for specific combination
- `GET /download/log` - Get download log
- `GET /download/zip/{state}` - Download state ZIP
- `GET /download/zip/all` - Download master ZIP

See `docs/API.md` for detailed API documentation.

## Key Technical Details

### Dropdown Chain (AJAX-dependent)

1. **State** (`#stateCode`) - 36 States/UTs
2. **Year of Revision** (`#revyear`) - 2024, 2025, 2026 (2024 may show "not available" alert)
3. **Roll Type** (`#roleType`) - Varies by state/year (e.g., "Final Roll - 2025", "Supplement-5 2025", "DraftRoll - 2025")
4. **District** (`#district`) - Loads after roll type selection
5. **Assembly Constituency** (`#constituency`) - Loads after district selection
6. **Language** (`#langCd`) - ENG/HIN/URDU - Loads after constituency selection

### Pagination

- Table: `table.datatable.contenttable-eroll`
- 10 parts per page
- Controls: `<<` (first), `<` (prev), `>` (next), `>>` (last)
- Strategy: Click `>>` to go to last page, then iterate backward with `<`

### Captcha

- Base64 data URL: `data:image/png;base64,...` or `data:image/jpg;base64,...`
- Must extract base64, save as image, send to vision model
- Input field: `#captcha`
- Submit button: `button.btn.btn-block.submit`

## Known Issues & Limitations

1. **Year 2024**: Shows "Publish Eroll not available for this Year" alert - use 2025/2026 instead
2. **Rate Limiting**: Site may rate-limit aggressive requests - built-in delays help
3. **Captcha**: Vision model not 100% accurate - manual fallback available
4. **Delhi Discovery**: Had initial bug (now fixed in `discovered_structure.json`)

## Testing

Run the test scripts in `tests/` folder:

```bash
# Test structure
python tests/test_structure.py

# Test specific functionality
python tests/check_2025.py
python tests/check_lang_2025.py
```

## License

This project is for educational and research purposes. Please respect the ECI website's terms of service and robots.txt.

## Contributing

1. Fork the repository
2. Create a feature branch
3. Make your changes
4. Submit a pull request

## Support

For issues, please check the debug scripts in `debug/` folder for troubleshooting tips.

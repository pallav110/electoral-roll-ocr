"""
FastAPI REST API for ECI Electoral Roll Downloader
Provides programmatic access to discovered structure and download triggering.
"""

import os
import json
from pathlib import Path
from typing import Optional, List, Dict, Any
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, BackgroundTasks, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

# Import downloader
from .eci_downloader import ECIDownloader


# Global downloader instance
downloader_instance: Optional[ECIDownloader] = None
discovered_structure: Dict[str, Any] = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan handler."""
    global downloader_instance, discovered_structure

    # Initialize downloader
    vision_key = os.environ.get("VISION_API_KEY")
    downloader_instance = ECIDownloader(vision_api_key=vision_key)

    # Load discovered structure if exists
    structure_path = Path("discovered_structure.json")
    if structure_path.exists():
        with open(structure_path, "r", encoding="utf-8") as f:
            discovered_structure = json.load(f)

    yield

    # Cleanup if needed
    pass


app = FastAPI(
    title="ECI Electoral Roll Downloader API",
    description="REST API for discovering and downloading electoral roll PDFs from ECI website",
    version="1.0.0",
    lifespan=lifespan
)


# Pydantic models for request/response
class DownloadRequest(BaseModel):
    state: str = Field(..., description="State label (e.g., 'NCT OF Delhi')")
    year: str = Field(..., description="Year of revision (e.g., '2025')")
    roll_type: str = Field(..., description="Roll type label (e.g., 'Final Roll - 2025')")
    district: str = Field(..., description="District label")
    constituency: str = Field(..., description="Constituency label (e.g., '23 - KAROL BAGH')")
    language: str = Field(..., description="Language label (e.g., 'ENGLISH')")
    vision_api_key: Optional[str] = Field(None, description="Optional vision API key for this request")


class DownloadResponse(BaseModel):
    success: bool
    message: str
    file_path: Optional[str] = None
    parts_count: int = 0
    parts: List[str] = []


class StructureResponse(BaseModel):
    states: List[str]


class YearsResponse(BaseModel):
    years: List[str]


class RollTypesResponse(BaseModel):
    roll_types: List[str]


class DistrictsResponse(BaseModel):
    districts: List[str]


class ConstituenciesResponse(BaseModel):
    constituencies: List[Dict[str, str]]


class LanguagesResponse(BaseModel):
    languages: List[Dict[str, str]]


class LogEntry(BaseModel):
    path: str
    relative: str
    state: str
    year: str
    roll_type: str
    district: str
    constituency: str
    language: str
    parts_count: int
    parts: List[str]


# API Endpoints
@app.get("/health")
async def health_check():
    """Health check endpoint."""
    return {"status": "healthy", "service": "ECI Electoral Roll Downloader"}


@app.get("/states", response_model=StructureResponse)
async def list_states():
    """List all available states."""
    states = list(discovered_structure.keys())
    return {"states": states}


@app.get("/states/{state}/years", response_model=YearsResponse)
async def list_years(state: str):
    """List available years for a state."""
    if state not in discovered_structure:
        raise HTTPException(status_code=404, detail=f"State '{state}' not found")

    years = list(discovered_structure[state].get("years", {}).keys())
    return {"years": years}


@app.get("/states/{state}/years/{year}/roll-types", response_model=RollTypesResponse)
async def list_roll_types(state: str, year: str):
    """List available roll types for a state/year."""
    if state not in discovered_structure:
        raise HTTPException(status_code=404, detail=f"State '{state}' not found")

    years_data = discovered_structure[state].get("years", {})
    if year not in years_data:
        raise HTTPException(status_code=404, detail=f"Year '{year}' not found for state '{state}'")

    roll_types = list(years_data[year].get("roll_types", {}).keys())
    return {"roll_types": roll_types}


@app.get("/states/{state}/years/{year}/roll-types/{roll_type}/districts", response_model=DistrictsResponse)
async def list_districts(state: str, year: str, roll_type: str):
    """List available districts for a state/year/roll_type."""
    try:
        districts = list(
            discovered_structure[state]["years"][year]["roll_types"][roll_type]["districts"].keys()
        )
        return {"districts": districts}
    except KeyError as e:
        raise HTTPException(status_code=404, detail=f"Path not found: {e}")


@app.get("/states/{state}/years/{year}/roll-types/{roll_type}/districts/{district}/constituencies", response_model=ConstituenciesResponse)
async def list_constituencies(state: str, year: str, roll_type: str, district: str):
    """List available constituencies for a district."""
    try:
        constituencies = discovered_structure[state]["years"][year]["roll_types"][roll_type]["districts"][district]["constituencies"]
        return {"constituencies": constituencies}
    except KeyError as e:
        raise HTTPException(status_code=404, detail=f"Path not found: {e}")


@app.get("/states/{state}/years/{year}/roll-types/{roll_type}/districts/{district}/constituencies/{constituency}/languages", response_model=LanguagesResponse)
async def list_languages(state: str, year: str, roll_type: str, district: str, constituency: str):
    """List available languages for a constituency."""
    try:
        languages = discovered_structure[state]["years"][year]["roll_types"][roll_type]["districts"][district]["languages"]
        return {"languages": languages}
    except KeyError as e:
        raise HTTPException(status_code=404, detail=f"Path not found: {e}")


@app.post("/download", response_model=DownloadResponse)
async def trigger_download(request: DownloadRequest, background_tasks: BackgroundTasks):
    """
    Trigger download for a specific combination.
    This runs in background to avoid blocking the API.
    """
    if not downloader_instance:
        raise HTTPException(status_code=503, detail="Downloader not initialized")

    # Validate structure exists
    try:
        state_data = discovered_structure[request.state]
        year_data = state_data["years"][request.year]
        roll_data = year_data["roll_types"][request.roll_type]
        dist_data = roll_data["districts"][request.district]

        # Find constituency and language values
        const_value = None
        for const in dist_data["constituencies"]:
            if const["label"] == request.constituency:
                const_value = const["value"]
                break

        lang_value = None
        for lang in dist_data["languages"]:
            if lang["label"] == request.language:
                lang_value = lang["value"]
                break

        if not const_value:
            raise HTTPException(status_code=404, detail=f"Constituency '{request.constituency}' not found")
        if not lang_value:
            raise HTTPException(status_code=404, detail=f"Language '{request.language}' not found")

    except KeyError as e:
        raise HTTPException(status_code=404, detail=f"Invalid path: {e}")

    # Get state value (need to fetch from page or cache)
    # For now, we'll need to fetch it
    state_value = await get_state_value(request.state)
    if not state_value:
        raise HTTPException(status_code=500, detail="Could not determine state value")

    params = {
        "state": state_value,
        "year": request.year,
        "rollType": request.roll_type,
        "district": request.district,
        "constituency": request.constituency,
        "language": request.language
    }

    # Run download in background
    def download_task():
        # Use provided vision key or instance default
        if request.vision_api_key:
            downloader_instance.vision_api_key = request.vision_api_key

        with __import__('playwright.sync_api', fromlist=['sync_playwright']).sync_playwright() as p:
            browser = p.chromium.launch(headless=False)
            context = browser.new_context()
            page = context.new_page()
            page.goto("https://voters.eci.gov.in/download-eroll")
            page.wait_for_load_state("networkidle")

            success, result = downloader_instance.download_all_parts_for_combination(
                page, params, request.state, request.year, request.roll_type,
                request.district, request.constituency, request.language
            )

            browser.close()

            if success:
                downloader_instance.create_organized_zips()

    background_tasks.add_task(download_task)

    return DownloadResponse(
        success=True,
        message="Download started in background. Check /download/log for progress.",
        parts_count=0
    )


@app.get("/download/log", response_model=List[LogEntry])
async def get_download_log():
    """Get download history log."""
    log_path = Path("download_log.json")
    if not log_path.exists():
        return []

    with open(log_path, "r", encoding="utf-8") as f:
        log_data = json.load(f)

    return log_data


@app.get("/download/zip/{state}")
async def download_state_zip(state: str):
    """Download ZIP archive for a specific state."""
    safe_state = state.replace(" ", "_").replace("&", "and").replace("/", "_")
    zip_path = Path(f"electoral_rolls_{safe_state}.zip")

    if not zip_path.exists():
        raise HTTPException(status_code=404, detail=f"ZIP for state '{state}' not found. Run download first.")

    return FileResponse(
        path=str(zip_path),
        filename=zip_path.name,
        media_type="application/zip"
    )


@app.get("/download/zip/all")
async def download_all_zip():
    """Download master ZIP archive with all states."""
    zip_path = Path("electoral_rolls_ALL.zip")

    if not zip_path.exists():
        raise HTTPException(status_code=404, detail="Master ZIP not found. Run download first.")

    return FileResponse(
        path=str(zip_path),
        filename=zip_path.name,
        media_type="application/zip"
    )


async def get_state_value(state_label: str) -> Optional[str]:
    """Get state value from the page."""
    try:
        with __import__('playwright.sync_api', fromlist=['sync_playwright']).sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()
            page.goto("https://voters.eci.gov.in/download-eroll")
            page.wait_for_load_state("networkidle")

            opts = page.locator("#stateCode option").all()
            for opt in opts:
                val = opt.get_attribute("value")
                text = opt.inner_text().strip()
                if text == state_label:
                    browser.close()
                    return val

            browser.close()
    except Exception:
        pass
    return None


# For direct script execution
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
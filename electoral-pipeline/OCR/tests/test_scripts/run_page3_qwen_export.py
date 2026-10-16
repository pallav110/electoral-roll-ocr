import asyncio
import json
from pathlib import Path

from pipeline.pdf_extract import extract_from_pdf_bytes

PDF_PATH = Path("/home/spx015/whisper-local/2026-EROLLGEN-S24-53-SIR-FinalRoll-Revision1-HIN-300-WI.pdf")
OUT_PATH = Path("/home/spx015/whisper-local/page3_qwen_results.json")


async def main() -> None:
    print(f"Reading PDF: {PDF_PATH}")
    pdf_bytes = PDF_PATH.read_bytes()

    result = await extract_from_pdf_bytes(
        pdf_bytes=pdf_bytes,
        query="Extract all voter information from this page",
        llm_url="http://localhost:11434",
        model="qwen2.5vl:7b",
        max_pages=1,
        start_page=3,
    )

    OUT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"Saved result to: {OUT_PATH}")
    print(json.dumps({
        "ok": result.get("ok"),
        "pages_processed": result.get("pages_processed"),
        "pages_ok": result.get("pages_ok"),
        "pages_failed": result.get("pages_failed"),
        "records": len(result.get("result", {}).get("records", [])) if isinstance(result.get("result"), dict) else 0,
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())

"""Probe: click submit and capture the FORM POST RESPONSE BODY, not browser download."""
import base64
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from playwright.sync_api import sync_playwright
from dotenv import load_dotenv

load_dotenv(dotenv_path=str(ROOT / ".env"))

import requests

BASE_URL = "https://voters.eci.gov.in/download-eroll"
VISION_API_URL = "https://integrate.api.nvidia.com/v1/chat/completions"
VISION_MODEL = "meta/llama-3.2-11b-vision-instruct"

STATE_VALUE = "U05"
YEAR_VALUE = "2025"
DISTRICT_LABEL = "Central"
CONSTITUENCY_LABEL = "23 - KAROL BAGH"
LANGUAGE_LABEL = "ENGLISH"
PART_LIMIT = 10


def solve(image_path):
    key = __import__("os").environ["VISION_API_KEY"]
    with open(image_path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode()
    r = requests.post(
        VISION_API_URL,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        json={
            "model": VISION_MODEL,
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "text", "text": "Read the captcha code in this image. Reply with only the code, no explanation."},
                    {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
                ],
            }],
            "max_tokens": 20,
        },
        timeout=60,
    )
    r.raise_for_status()
    raw = r.json()["choices"][0]["message"]["content"].strip()
    code = re.sub(r"[^A-Za-z0-9]", "", raw)
    print(f"    vision -> {raw!r} -> {code!r}", flush=True)
    return code


def pick(page, selector, label):
    page.wait_for_selector(f"{selector} option[value]", state="attached", timeout=90000)
    for opt in page.locator(f"{selector} option").all():
        if opt.inner_text().strip() == label:
            return opt.get_attribute("value")
    return None


def main():
    responses_bodies = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(accept_downloads=True)
        page = ctx.new_page()

        # Capture RESPONSE BODIES for form POSTs
        async def on_response(resp):
            if resp.request.method == "POST" and "/download" in resp.url:
                try:
                    body = await resp.body()
                    ctype = resp.headers.get("content-type", "")
                    print(f"    [POST RESPONSE] status={resp.status} len={len(body)} type={ctype}", flush=True)
                    if len(body) > 100:
                        responses_bodies.append((resp.status, ctype, body))
                except Exception as e:
                    print(f"    [POST RESPONSE ERROR] {e}", flush=True)

        page.on("response", on_response)

        print("[*] Loading form", flush=True)
        loaded = False
        for attempt in range(1, 6):
            try:
                resp = page.goto(BASE_URL, timeout=90000)
                status = resp.status if resp else None
                print(f"    attempt {attempt}: HTTP {status}", flush=True)
                if status == 200 or status is None:
                    page.wait_for_selector("#stateCode option[value]", state="attached", timeout=60000)
                    loaded = True
                    break
            except Exception as exc:
                print(f"    attempt {attempt}: {type(exc).__name__}", flush=True)
            time.sleep(15)
        if not loaded:
            print("[!] Form never loaded after 5 attempts", flush=True)
            browser.close()
            return 1

        page.select_option("#stateCode", value=STATE_VALUE)
        page.wait_for_load_state("networkidle")
        time.sleep(1.5)
        page.select_option("#revyear", value=YEAR_VALUE)
        page.wait_for_selector("#roleType option[value]", state="attached", timeout=60000)
        page.wait_for_load_state("networkidle")
        time.sleep(1.5)

        roll_value = pick(page, "#roleType", "Final Roll - 2025")
        if not roll_value:
            roll_value = pick(page, "#roleType", "State General Election Roll 2025")
        page.select_option("#roleType", value=roll_value)
        page.wait_for_selector("#district option[value]", state="attached", timeout=60000)
        page.wait_for_load_state("networkidle")
        time.sleep(1.5)

        const_value = pick(page, "#constituency", CONSTITUENCY_LABEL)
        page.select_option("#constituency", value=const_value)
        time.sleep(1.5)

        lang_value = pick(page, "#langCd", LANGUAGE_LABEL)
        page.select_option("#langCd", value=lang_value)
        time.sleep(3)

        # Check parts
        rows = page.locator("table.datatable.contenttable-eroll tbody tr").all()
        checked = 0
        for row in rows:
            if checked >= PART_LIMIT:
                break
            cb = row.locator("td input[type='checkbox']").first
            if cb.count() == 0:
                continue
            try:
                cb.check(timeout=5000)
                checked += 1
            except Exception:
                break
        print(f"[*] Checked {checked} parts", flush=True)

        # Fill captcha
        def captcha_locator(page):
            img = page.locator("img[alt='captcha'], img[alt='Captcha'], img[id='captchaImg'], img.captcha").first
            if img.count() == 0:
                img = page.locator("#captcha").locator("..").locator("img").first
            return img

        img = captcha_locator(page)
        src = img.get_attribute("src")
        if src and src.startswith("data:image"):
            (ROOT / "probe_captcha.png").write_bytes(base64.b64decode(src.split(",", 1)[1]))
        else:
            img.screenshot(path=str(ROOT / "probe_captcha.png"))
        code = solve(ROOT / "probe_captcha.png")

        print("[*] Filling captcha and submitting via form POST capture", flush=True)
        page.fill("#captcha", code)
        page.click("button.btn.btn-block.submit")
        time.sleep(20)

        print(f"\n[*] Captured {len(responses_bodies)} form POST response(s)", flush=True)
        for i, (status, ctype, body) in enumerate(responses_bodies):
            print(f"  --- Response {i+1} ---", flush=True)
            print(f"    Status: {status}", flush=True)
            print(f"    Content-Type: {ctype}", flush=True)
            print(f"    Body length: {len(body)}", flush=True)
            if b"PK" in body[:4] or b"zip" in ctype.lower() or b"octet" in ctype.lower():
                print(f"    *** LIKELY ZIP FILE ***", flush=True)
                (ROOT / f"downloaded_part_{i+1}.zip").write_bytes(body)
                print(f"    *** Saved to downloaded_part_{i+1}.zip ***", flush=True)
            elif body[:200].strip():
                print(f"    Body preview: {body[:500]}", flush=True)

        browser.close()


if __name__ == "__main__":
    main()

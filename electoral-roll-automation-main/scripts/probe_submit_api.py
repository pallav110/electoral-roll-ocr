"""Probe: auto-fill captcha, click submit, capture download API response."""
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


def main():
    captured_bodies = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(accept_downloads=True)
        page = ctx.new_page()

        def on_response(resp):
            if resp.request.method == "POST":
                try:
                    body = resp.body()
                    if body and len(body) > 100:
                        captured_bodies.append({
                            'status': resp.status,
                            'url': resp.url,
                            'ctype': resp.headers.get('content-type', ''),
                            'body': body,
                        })
                        print(f"    [POST BODY] {resp.status} {resp.url[:80]} len={len(body)} ctype={resp.headers.get('content-type','')[:40]}", flush=True)
                        if body[:4] == b'PK\x03\x04':
                            print(f"    *** ZIP FILE! ***", flush=True)
                            (ROOT / "downloaded.zip").write_bytes(body)
                except Exception as e:
                    pass

        page.on("response", on_response)

        page.goto(BASE_URL, timeout=90000)
        page.wait_for_selector("#stateCode option[value]", state="attached", timeout=60000)

        page.select_option("#stateCode", value=STATE_VALUE)
        page.wait_for_load_state("networkidle")
        time.sleep(1)
        page.select_option("#revyear", value=YEAR_VALUE)
        page.wait_for_selector("#roleType option[value]", state="attached", timeout=60000)
        page.wait_for_load_state("networkidle")
        time.sleep(1)

        # Select Final Roll
        roll_val = None
        for opt in page.locator("#roleType option").all():
            if "Final Roll" in opt.inner_text().strip():
                roll_val = opt.get_attribute("value")
                break
        page.select_option("#roleType", value=roll_val)
        page.wait_for_selector("#district option[value]", state="attached", timeout=60000)
        page.wait_for_load_state("networkidle")
        time.sleep(1)

        for opt in page.locator("#district option").all():
            if opt.get_attribute("value"):
                page.select_option("#district", value=opt.get_attribute("value"))
                break
        page.wait_for_load_state("networkidle")
        time.sleep(1)

        for opt in page.locator("#constituency option").all():
            if opt.get_attribute("value"):
                page.select_option("#constituency", value=opt.get_attribute("value"))
                break
        page.wait_for_load_state("networkidle")
        time.sleep(1)

        for opt in page.locator("#langCd option").all():
            if opt.get_attribute("value"):
                page.select_option("#langCd", value=opt.get_attribute("value"))
                break
        page.wait_for_load_state("networkidle")
        time.sleep(3)

        # Wait for table
        for i in range(30):
            rows = page.locator("table.datatable.contenttable-eroll tbody tr").all()
            if rows:
                print(f"[*] Table has {len(rows)} rows")
                break
            time.sleep(1)
        else:
            print("[!] No table rows")
            browser.close()
            return

        for row in rows[:10]:
            cb = row.locator("td input[type='checkbox']").first
            if cb.count():
                cb.check()
                time.sleep(0.2)

        # Solve captcha
        def captcha_locator(page):
            img = page.locator("img[alt='captcha'], img[alt='Captcha'], img[id='captchaImg'], img.captcha").first
            if img.count() == 0:
                img = page.locator("#captcha").locator("..").locator("img").first
            return img

        img = captcha_locator(page)
        src = img.get_attribute("src")
        if src and src.startswith("data:image"):
            (ROOT / "captcha.png").write_bytes(base64.b64decode(src.split(",", 1)[1]))
        else:
            img.screenshot(path=str(ROOT / "captcha.png"))
        code = solve(ROOT / "captcha.png")

        print(f"[*] Filling captcha: {code}", flush=True)
        page.fill("#captcha", code)
        
        # Click submit
        print("[*] Clicking submit...", flush=True)
        page.click("button.btn.btn-block.submit")
        
        # Wait for response
        time.sleep(20)

        print(f"\n[*] Captured {len(captured_bodies)} POST responses with bodies", flush=True)
        for i, cb in enumerate(captured_bodies):
            print(f"  {i+1}. {cb['status']} {cb['url'][:100]}")
            print(f"     ctype: {cb['ctype']}")
            print(f"     len: {len(cb['body'])}")
            if cb['body'][:4] == b'PK\x03\x04':
                print(f"     *** ZIP *** saved as downloaded.zip")

        # Check for download event
        print("\n[*] Done", flush=True)
        browser.close()


if __name__ == "__main__":
    main()

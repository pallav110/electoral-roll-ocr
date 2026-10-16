"""Minimal debug: fill form, submit, dump ALL responses."""
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from playwright.sync_api import sync_playwright
from dotenv import load_dotenv

load_dotenv(dotenv_path=str(ROOT / ".env"))

BASE_URL = "https://voters.eci.gov.in/download-eroll"

STATE_VALUE = "U05"
YEAR_VALUE = "2025"


def main():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False, slow_mo=500)  # HEADED!
        ctx = browser.new_context(accept_downloads=True)
        page = ctx.new_page()

        responses = []

        def on_response(resp):
            responses.append({
                'status': resp.status,
                'method': resp.request.method,
                'url': resp.url,
                'type': resp.request.resource_type,
                'ctype': resp.headers.get('content-type', ''),
            })

        page.on("response", on_response)

        print("[*] Loading...")
        page.goto(BASE_URL, timeout=90000)
        page.wait_for_selector("#stateCode option[value]", state="attached", timeout=60000)
        print("[*] Form loaded")

        page.select_option("#stateCode", value=STATE_VALUE)
        page.wait_for_load_state("networkidle")
        time.sleep(1)
        page.select_option("#revyear", value=YEAR_VALUE)
        page.wait_for_selector("#roleType option[value]", state="attached", timeout=60000)
        page.wait_for_load_state("networkidle")
        time.sleep(1)

        print("[*] Roll types:", [o.inner_text().strip() for o in page.locator("#roleType option").all()])
        
        # Select first real roll
        for opt in page.locator("#roleType option").all():
            if opt.get_attribute("value"):
                page.select_option("#roleType", value=opt.get_attribute("value"))
                break
        page.wait_for_selector("#district option[value]", state="attached", timeout=60000)
        page.wait_for_load_state("networkidle")
        time.sleep(1)

        print("[*] Districts:", [o.inner_text().strip() for o in page.locator("#district option").all()])
        
        for opt in page.locator("#district option").all():
            if opt.get_attribute("value"):
                page.select_option("#district", value=opt.get_attribute("value"))
                break
        page.wait_for_load_state("networkidle")
        time.sleep(1)

        print("[*] Constituencies:", [o.inner_text().strip() for o in page.locator("#constituency option").all()])
        
        for opt in page.locator("#constituency option").all():
            if opt.get_attribute("value"):
                page.select_option("#constituency", value=opt.get_attribute("value"))
                break
        page.wait_for_load_state("networkidle")
        time.sleep(1)

        print("[*] Languages:", [o.inner_text().strip() for o in page.locator("#langCd option").all()])
        
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

        # Check first 3
        for row in rows[:3]:
            cb = row.locator("td input[type='checkbox']").first
            if cb.count():
                cb.check()
                time.sleep(0.2)

        print("[*] Ready to submit. Filling captcha and submitting via code...")

        # Solve captcha
        def captcha_locator(page):
            img = page.locator(
                "img[alt='captcha'], img[alt='Captcha'], img[id='captchaImg'], img.captcha"
            ).first
            if img.count() == 0:
                img = page.locator("#captcha").locator("..").locator("img").first
            return img

        import base64
        import requests
        import re

        VISION_API_URL = "https://integrate.api.nvidia.com/v1/chat/completions"
        VISION_MODEL = "meta/llama-3.2-11b-vision-instruct"
        key = __import__("os").environ["VISION_API_KEY"]

        img = captcha_locator(page)
        src = img.get_attribute("src")
        if src and src.startswith("data:image"):
            (ROOT / "debug_captcha.png").write_bytes(base64.b64decode(src.split(",", 1)[1]))
        else:
            img.screenshot(path=str(ROOT / "debug_captcha.png"))

        with open(ROOT / "debug_captcha.png", "rb") as f:
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

        print("[*] Submitting...", flush=True)
        page.fill("#captcha", code)
        page.click("button.btn.btn-block.submit")

        # Wait for response
        time.sleep(25)

        print(f"\n[*] Total responses: {len(responses)}")
        for r in responses:
            print(f"  {r['status']} {r['method']:6} {r['type']:8} {r['ctype'][:40]:40} {r['url'][:100]}")

        browser.close()


if __name__ == "__main__":
    main()

"""Diagnose what the ECI form actually does when the captcha is submitted.

`download_all_parts_for_combination` waits for a Playwright download event and
has never seen one fire, so it retries the same captcha five times against a
site that may already have accepted it. This script does not guess: it attaches
handlers to every request and response, clicks submit once, and dumps what came
back -- status, content-type, and body length -- so the capture mechanism can be
chosen from evidence.

Run it from the project root:
    python scripts/probe_submit.py
"""
import base64
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from playwright.sync_api import sync_playwright  # noqa: E402
from dotenv import load_dotenv  # noqa: E402

load_dotenv(dotenv_path=str(ROOT / ".env"))

import requests  # noqa: E402

BASE_URL = "https://voters.eci.gov.in/download-eroll"
VISION_API_URL = "https://integrate.api.nvidia.com/v1/chat/completions"
VISION_MODEL = "meta/llama-3.2-11b-vision-instruct"

STATE_VALUE = "U05"  # NCT OF Delhi
YEAR_VALUE = "2025"
DISTRICT_VALUE = "Central"
CONSTITUENCY_VALUE = None  # resolved by label below
LANGUAGE_VALUE = None
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
                    {"type": "text",
                     "text": "Read the captcha code in this image. Reply with only "
                             "the code, no explanation."},
                    {"type": "image_url",
                     "image_url": {"url": f"data:image/png;base64,{b64}"}},
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
    """Select the <option> whose text matches `label`, return its value."""
    page.wait_for_selector(f"{selector} option[value]", state="attached", timeout=90000)
    for opt in page.locator(f"{selector} option").all():
        if opt.inner_text().strip() == label:
            return opt.get_attribute("value")
    return None


def main():
    events = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(accept_downloads=True)
        page = ctx.new_page()

        def on_response(resp):
            if resp.request.resource_type in ("xhr", "fetch", "document"):
                events.append((resp.status, resp.request.method,
                               resp.url, resp.headers.get("content-type", "")))

        page.on("response", on_response)

        def on_download(dl):
            print(f"  [download event] {dl.suggested_filename}", flush=True)

        page.on("download", on_download)
        page.on("popup", lambda pg: print(f"  [popup] {pg.url}", flush=True))

        # The site 503s intermittently with an empty body, so retry and judge
        # success on the form actually rendering, not on the status code.
        print("[*] Loading form", flush=True)
        loaded = False
        for attempt in range(1, 6):
            try:
                resp = page.goto(BASE_URL, timeout=90000)
                status = resp.status if resp else None
                print(f"    attempt {attempt}: HTTP {status}", flush=True)
                if status == 200 or status is None:
                    page.wait_for_selector("#stateCode option[value]",
                                           state="attached", timeout=60000)
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
        # Wait for roll type dropdown to repopulate (AJAX)
        page.wait_for_selector("#roleType option[value]", state="attached", timeout=60000)
        page.wait_for_load_state("networkidle")
        time.sleep(1.5)

        for sel in ("#roleType", "#district", "#constituency", "#langCd"):
            print(f"    {sel}: {[o.inner_text().strip() for o in page.locator(f'{sel} option').all()][:6]}", flush=True)

        page.select_option("#roleType", index=1)
        page.wait_for_selector("#district option[value]", state="attached", timeout=60000)
        page.wait_for_load_state("networkidle")
        time.sleep(1.5)

        const_value = pick(page, "#constituency", "23 - KAROL BAGH")
        page.select_option("#constituency", value=const_value)
        time.sleep(1.5)

        lang_value = pick(page, "#langCd", "ENGLISH")
        page.select_option("#langCd", value=lang_value)
        time.sleep(2)

        print("[*] Parts table present:",
              page.locator("table.datatable.contenttable-eroll").count())

        # Check the first PART_LIMIT checkboxes and nothing else, so the site
        # does not refuse the 11th.
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
            except Exception as e:
                print(f"    stopped at {checked}: {type(e).__name__}", flush=True)
                break
        print(f"[*] Checked {checked} parts on page 1", flush=True)

        # Report what the form is about to do, so the response can be interpreted.
        form_html = page.locator("form").all()
        for i, f in enumerate(form_html):
            a, m = f.get_attribute("action"), f.get_attribute("method")
            tgt = f.get_attribute("target")
            if a:
                print(f"    form[{i}] action={a!r} method={m!r} target={tgt!r}", flush=True)

        btn = page.locator("button.btn.btn-block.submit").first
        print("[*] Submit button count:",
              page.locator("button.btn.btn-block.submit").count())

        before = len(events)

        def captcha_locator(page):
            img = page.locator(
                "img[alt='captcha'], img[alt='Captcha'], img[id='captchaImg'], img.captcha"
            ).first
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

        print("[*] Submitting", flush=True)
        page.fill("#captcha", code)
        btn.click()
        time.sleep(25)

        print(f"\n[*] {len(events) - before} request(s) since submit:", flush=True)
        for status, method, url, ctype in events[before:]:
            short = re.sub(r"^https?://[^/]+", "", url)
            print(f"    {status} {method:6} {ctype[:40]:40} {short[:90]}", flush=True)

        # Any visible error the site is showing us.
        for sel in (".alert", ".library-alert", ".error", ".toast", ".swal2-container",
                    "div[class*='message']"):
            for el in page.locator(sel).all():
                try:
                    txt = el.inner_text().strip()
                except Exception:
                    continue
                if txt:
                    print(f"    [{sel}] {txt[:120]!r}", flush=True)

        page.screenshot(path=str(ROOT / "probe_after_submit.png"), full_page=True)
        print("\n[*] Screenshot: probe_after_submit.png", flush=True)
        browser.close()


if __name__ == "__main__":
    main()

"""Step-by-step probe. Prints one numbered STEP per action, so failures are
located rather than guessed at.

Run:  python scripts/step_probe.py

Debug log appended to: debug_steps.md
"""
import base64
import json
import os
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
GATEWAY = "https://gateway-voters.eci.gov.in"
VISION_API_URL = "https://integrate.api.nvidia.com/v1/chat/completions"
VISION_MODEL = "meta/llama-3.2-11b-vision-instruct"

LOG_PATH = ROOT / "debug_steps.md"

_step_no = 0
_log_lines = []


def step(msg):
    """Print and record a numbered step."""
    global _step_no
    _step_no += 1
    line = f"STEP {_step_no:02d}  {msg}"
    print(line, flush=True)
    _log_lines.append(line)


def note(msg):
    print(f"        {msg}", flush=True)
    _log_lines.append(f"        {msg}")


def flush_log():
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(f"\n\n## Run {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
        f.write("```\n")
        f.write("\n".join(_log_lines))
        f.write("\n```\n")


def solve(image_path):
    """Solve via consensus using the downloader's own normaliser.

    Shares src/eci_downloader.py deliberately: the probe exists to characterise
    the live site, and a second captcha implementation here would drift from the
    one the real pipeline runs.
    """
    sys.path.insert(0, str(ROOT / "src"))
    from eci_downloader import ECIDownloader

    helper = ECIDownloader(vision_api_key=os.environ["VISION_API_KEY"])
    code = helper.solve_captcha_with_vision(image_path)
    return (code if code != "MANUAL" else None), code


def pick(page, selector, label):
    page.wait_for_selector(f"{selector} option[value]", state="attached", timeout=90000)
    for opt in page.locator(f"{selector} option").all():
        if opt.inner_text().strip() == label:
            return opt.get_attribute("value")
    return None


def main():
    step("START - probing generate-published-pdfs")

    with sync_playwright() as p:
        # Playwright's own browser cache was cleared from this machine mid-session,
        # which is why the form never rendered under system Chrome (its bundled
        # engine is now reinstalled). Prefer Playwright's chromium, fall back to
        # the system Chrome rather than failing outright.
        launch_kwargs = dict(headless=True)
        last_exc = None
        for extra in ({}, {"channel": "chrome"},
                      {"executable_path": "/usr/bin/google-chrome"}):
            try:
                browser = p.chromium.launch(**launch_kwargs, **extra)
                note(f"browser launched via {extra or 'playwright chromium'}")
                break
            except Exception as exc:
                last_exc = exc
                note(f"launch with {extra or 'playwright chromium'} failed: "
                     f"{type(exc).__name__}: {str(exc).splitlines()[0]}")
        else:
            step(f"FAILED - no usable browser: {last_exc}")
            flush_log()
            return 1
        ctx = browser.new_context(accept_downloads=True)
        page = ctx.new_page()

        # Capture the request that the submit click produces, in full.
        captured_requests = []

        def on_request(req):
            if "generate-published-pdfs" in req.url:
                try:
                    body = req.post_data
                except Exception as e:
                    body = f"<unreadable: {e}>"
                captured_requests.append({
                    "url": req.url,
                    "method": req.method,
                    "headers": req.headers,
                    "post_data": body,
                })
                step(f"REQ -> {req.method} {req.url}")
                note(f"headers: {json.dumps(dict(req.headers), indent=2)}")
                note(f"POST BODY: {body}")

        def on_response(resp):
            if "generate-published-pdfs" in resp.url:
                step(f"RES <- {resp.status} {resp.url}")
                note(f"request headers sent by browser:")
                for k, v in resp.request.headers.items():
                    note(f"   {k}: {v}")
                try:
                    body = resp.body()
                    note(f"RESPONSE BODY ({len(body)} bytes): {body!r}")
                    try:
                        note(f"PARSED JSON: {json.dumps(json.loads(body), indent=2)}")
                    except Exception:
                        note("(not valid JSON)")
                except Exception as e:
                    note(f"could not read body: {e}")
                for h in ("content-type", "content-disposition", "content-length"):
                    note(f"{h}: {resp.headers.get(h)}")

        page.on("request", on_request)
        page.on("response", on_response)

        # ---- STEP: load form -------------------------------------------------
        step("Loading form URL")
        loaded = False
        for attempt in range(1, 6):
            try:
                resp = page.goto(BASE_URL, timeout=90000)
                status = resp.status if resp else None
                note(f"attempt {attempt}: HTTP {status}")
                page.wait_for_selector("#stateCode option[value]",
                                       state="attached", timeout=120000)
                loaded = True
                step("Form loaded OK")
                break
            except Exception as exc:
                note(f"attempt {attempt}: {type(exc).__name__}: {str(exc)[:300]}")
            time.sleep(15)
        if not loaded:
            step("FAILED - form never loaded")
            flush_log()
            browser.close()
            return 1

        # ---- STEP: record what the page thinks the captcha session is ---------
        step("Reading captcha session state from the page")
        state = page.evaluate("""() => {
            const out = {};
            out.cookiesVisible = document.cookie;
            try {
                out.localStorage = JSON.stringify(Object.entries(localStorage));
            } catch (e) { out.localStorage = 'n/a'; }
            try {
                out.sessionStorage = JSON.stringify(Object.entries(sessionStorage));
            } catch (e) { out.sessionStorage = 'n/a'; }
            const c = document.querySelector('#captcha');
            out.captchaFieldName = c ? c.getAttribute('name') : null;
            out.captchaFieldId = c ? c.id : null;
            out.captchaFieldType = c ? c.getAttribute('type') : null;
            // Hidden inputs carry the server-side identifiers the POST needs.
            out.hidden = Array.from(document.querySelectorAll('input[type=hidden]'))
                .map(i => ({name: i.name, value: i.value, id: i.id}));
            out.allInputs = Array.from(document.querySelectorAll('input'))
                .map(i => ({name: i.name, id: i.id, type: i.type, value: (i.value||'').slice(0,40)}));
            const img = document.querySelector("img[alt*='captcha' i], img[id*='captcha' i], img.captcha");
            out.captchaImgSrcHead = img ? (img.getAttribute('src')||'').slice(0, 60) : null;
            return out;
        }""")
        note(json.dumps(state, indent=2))

        # ---- STEP: choose state/year/roll/dist/const/lang --------------------
        step("Selecting NCT OF Delhi / 2025")
        page.select_option("#stateCode", value="U05")
        page.wait_for_load_state("networkidle")
        time.sleep(2)
        page.select_option("#revyear", value="2025")
        page.wait_for_selector("#roleType option[value]", state="attached", timeout=90000)
        page.wait_for_load_state("networkidle")
        time.sleep(2)

        step("Selecting roll type 'Final Roll - 2025'")
        roll = pick(page, "#roleType", "Final Roll - 2025")
        note(f"roll value = {roll}")
        page.select_option("#roleType", value=roll)
        page.wait_for_selector("#district option[value]", state="attached", timeout=90000)
        page.wait_for_load_state("networkidle")
        time.sleep(2)

        step("Selecting district 'Central'")
        dist = pick(page, "#district", "Central")
        note(f"district value = {dist}")
        page.select_option("#district", value=dist)
        page.wait_for_load_state("networkidle")
        time.sleep(2)

        step("Selecting constituency '23 - KAROL BAGH'")
        const = pick(page, "#constituency", "23 - KAROL BAGH")
        note(f"constituency value = {const}")
        page.select_option("#constituency", value=const)
        page.wait_for_load_state("networkidle")
        time.sleep(2)

        step("Selecting language 'ENGLISH'")
        langs = [o.inner_text().strip() for o in page.locator("#langCd option").all()]
        note(f"available languages = {langs}")
        lang = pick(page, "#langCd", "ENGLISH")
        note(f"language value = {lang}")
        page.select_option("#langCd", value=lang)
        page.wait_for_load_state("networkidle")
        time.sleep(4)

        # ---- STEP: capture the parts list, which the POST must reference -----
        step("Reading the parts table")
        rows = page.locator("table.datatable.contenttable-eroll tbody tr").all()
        note(f"rows on page = {len(rows)}")
        checked = 0
        part_values = []
        for row in rows:
            if checked >= 10:
                break
            cb = row.locator("td input[type='checkbox']").first
            if cb.count() == 0:
                continue
            try:
                cb.check(timeout=5000)
                checked += 1
                part_values.append({
                    "name": cb.get_attribute("name"),
                    "value": cb.get_attribute("value"),
                    "id": cb.get_attribute("id"),
                })
            except Exception as e:
                note(f"stopped checking at {checked}: {type(e).__name__}")
                break
        note(f"checked {checked} parts")
        note(f"part checkbox attributes: {json.dumps(part_values, indent=2)}")

        # ---- STEP: solve captcha --------------------------------------------
        step("Solving captcha")
        img = page.locator(
            "img[alt='captcha'], img[alt='Captcha'], img[id='captchaImg'], img.captcha"
        ).first
        if img.count() == 0:
            img = page.locator("#captcha").locator("..").locator("img").first
        src = img.get_attribute("src")
        if src and src.startswith("data:image"):
            (ROOT / "step_captcha.png").write_bytes(
                base64.b64decode(src.split(",", 1)[1]))
            note("captcha image taken from inline data URI")
        else:
            img.screenshot(path=str(ROOT / "step_captcha.png"))
            note("captcha image taken from a screenshot of the element")
        raw, code = solve(ROOT / "step_captcha.png")
        note(f"vision attempt 1 = {raw!r}")
        # The vision model is not deterministic enough to trust a single read,
        # and a wrong read is indistinguishable from a wrong code server-side.
        # Re-ask on the SAME image (no refresh needed) until the normaliser
        # yields a 6-character code, so this probe reaches the POST rather than
        # dying one step earlier than the thing it is meant to characterise.
        tries = 0
        while not code and tries < 6:
            tries += 1
            note(f"no 6-char code yet, re-asking vision (try {tries}/6)")
            raw, code = solve(ROOT / "step_captcha.png")
            note(f"vision attempt {tries + 1} = {raw!r}")
        if not code:
            step("FAILED - vision model gave no 6-char code")
            flush_log()
            browser.close()
            return 1

        # ---- STEP: submit and read the failure -------------------------------
        step(f"Submitting with captcha {code!r}")
        page.fill("#captcha", code)
        page.click("button.btn.btn-block.submit")

        step("Waiting 30s for the response")
        time.sleep(30)

        step("Reading any visible error the site displayed")
        for sel in (".alert", ".library-alert", ".error", ".toast",
                    ".swal2-container", "div[class*='message']", "[role='alert']"):
            for el in page.locator(sel).all():
                try:
                    txt = el.inner_text().strip()
                except Exception:
                    continue
                if txt:
                    note(f"[{sel}] {txt[:300]!r}")

        page.screenshot(path=str(ROOT / "step_after_submit.png"), full_page=True)
        step(f"Screenshot written to step_after_submit.png")

        if not captured_requests:
            step("NO generate-published-pdfs request was made at all - "
                 "the button click did not reach the API")
        else:
            step(f"{len(captured_requests)} generate-published-pdfs request(s) seen")

        browser.close()

    flush_log()
    step("Wrote debug_steps.md")
    return 0


if __name__ == "__main__":
    sys.exit(main())

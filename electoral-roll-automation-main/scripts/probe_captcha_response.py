"""Capture the captcha-service responses while the form is filled.

Answers three questions the DOM cannot:
  1. Does the SPA call getCaptcha/EROLL at all, and what is in the body?
  2. Is there an `id` field, and is it the same one the POST needs?
  3. Is the inline <img src="data:..."> built from that response's `data`?

Everything the earlier code believed about the captcha ID was inferred from the
Angular bundle; this measures it on the live site instead.

Run:  python scripts/probe_captcha_response.py
"""
import base64
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

load_dotenv(dotenv_path=str(ROOT / ".env"))

BASE_URL = "https://voters.eci.gov.in/download-eroll"

captcha_hits = []
all_api = []


def step(msg):
    print(f"STEP  {msg}", flush=True)


def note(msg):
    print(f"      {msg}", flush=True)


def pick(page, selector, label):
    page.wait_for_selector(f"{selector} option[value]", state="attached", timeout=90000)
    for opt in page.locator(f"{selector} option").all():
        if opt.inner_text().strip() == label:
            return opt.get_attribute("value")
    return None


def on_response(resp):
    url = resp.url
    if "captcha" not in url.lower() and "ecinet" not in url.lower():
        return
    try:
        body = resp.body()
    except Exception as exc:
        note(f"  body unreadable: {exc}")
        return
    entry = {"url": url, "status": resp.status, "size": len(body)}
    try:
        parsed = json.loads(body)
        entry["json"] = parsed
    except Exception:
        entry["raw_head"] = body[:120].decode("utf-8", "replace")
    captcha_hits.append(entry)
    all_api.append(url)
    note(f"RESP {resp.status} {url}")
    if "json" in entry:
        keys = list(entry["json"].keys()) if isinstance(entry["json"], dict) else "list"
        note(f"  keys={keys}")
        if isinstance(entry["json"], dict):
            for k, v in entry["json"].items():
                if isinstance(v, str):
                    note(f"    {k} = {v[:70]!r}{'...' if len(v) > 70 else ''}")
                else:
                    note(f"    {k} = {v!r}"[:200])


def main():
    step("start - probing captcha-service responses on the live form")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(accept_downloads=True)
        page = ctx.new_page()
        page.on("response", on_response)

        step("loading form")
        for attempt in range(1, 12):
            try:
                r = page.goto(BASE_URL, timeout=120000)
                # The 1.8 MB Angular bundle takes 20-40 s to serve, so the app
                # renders long after the (fast) HTML document arrives. Budget the
                # render wait against the bundle, not the document.
                page.wait_for_selector("#stateCode option[value]",
                                       state="attached", timeout=120000)
                note(f"attempt {attempt}: HTTP {r.status}, form rendered")
                break
            except Exception as exc:
                note(f"attempt {attempt} failed: {type(exc).__name__}")
                time.sleep(4)
        else:
            step("FAILED - form never rendered")
            browser.close()
            return 1

        note(f"captcha responses after load: {len(captcha_hits)}")
        img = page.locator("img[alt*='aptcha'], img[id*='captcha'], img.captcha")
        note(f"captcha <img> present before fill: {img.count()}")
        note(f"#captcha input present before fill: "
             f"{page.locator('#captcha').count()}")

        # Fill the form -- the captcha only renders once language is chosen.
        for sel, label in (("#stateCode", "Andhra Pradesh"),
                           ("#revyear", None),
                           ("#district", None),
                           ("#constituency", None),
                           ("#langCd", "ENGLISH")):
            if label is None:
                continue
            val = pick(page, sel, label)
            if val is None:
                note(f"{sel}: label {label!r} not found")
                continue
            page.select_option(sel, value=val)
            page.wait_for_load_state("networkidle")
            time.sleep(3)
            note(f"{sel} = {val}")

        step("captcha responses after filling the form")
        for e in captcha_hits:
            note(f"  {e['status']} {e['url']} ({e['size']} bytes)")
            if "json" in e and isinstance(e["json"], dict):
                for k, v in e["json"].items():
                    if isinstance(v, str) and len(v) > 60:
                        note(f"    {k} = <{len(v)} chars> {v[:50]!r}...")
                    else:
                        note(f"    {k} = {str(v)[:120]!r}")

        # Cross-check: is the inline data URI the same bytes as the response `data`?
        img = page.locator("img[alt*='aptcha'], img[id*='captcha'], img.captcha").first
        if img.count():
            src = img.get_attribute("src") or ""
            note(f"inline <img src> prefix: {src[:40]!r} len={len(src)}")
            if src.startswith("data:"):
                inline = base64.b64decode(src.split(",", 1)[1])
                note(f"inline image decodes to {len(inline):,} bytes")
                for e in captcha_hits:
                    d = (e.get("json") or {}).get("data") if isinstance(e.get("json"), dict) else None
                    if isinstance(d, str) and d:
                        same = base64.b64decode(d) == inline
                        note(f"response `data` from {e['url'][-40:]} -> "
                             f"identical to inline image: {same}")
                (ROOT / "probe_captcha.png").write_bytes(inline)
                note("wrote probe_captcha.png")
        else:
            note("no captcha <img> found after filling the form")

        step("hidden inputs present")
        hidden = page.eval_on_selector_all(
            "input[type=hidden]",
            "els => els.map(e => ({name: e.name, value: String(e.value).slice(0,60)}))")
        note(json.dumps(hidden, indent=2)[:600])

        browser.close()

    step(f"TOTAL captcha/ecinet responses captured: {len(captcha_hits)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
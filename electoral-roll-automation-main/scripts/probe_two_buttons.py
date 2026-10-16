"""Determine which of the form's two buttons actually submits.

The live form has both "Search" and "download selected pdfs". The flow
clicks `button.btn.btn-block.submit`, which a previous probe matched to
"Download Selected PDFs" (blue, right side). But the site's own error text
is "Please Enter Valid Captcha", which means the click DID reach the API --
so the selector is clicking something real.

This confirms it by clicking each button in turn on a fully-filled form and
reporting which one produces a generate-published-pdfs request.

Run:  python scripts/probe_two_buttons.py
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

# Every clickable element, with its own text, so "Search" and
# "download selected pdfs" are distinguished by what they actually are.
COLLECT_JS = """
() => {
  const out = [];
  for (const el of document.querySelectorAll('button, input[type=submit], input[type=button], a')) {
    const r = el.getBoundingClientRect();
    if (!r.width || !r.height) continue;
    const cs = getComputedStyle(el);
    out.push({
      tag: el.tagName.toLowerCase(),
      type: el.getAttribute('type'),
      id: el.id || null,
      cls: (el.className || '').toString(),
      text: (el.innerText || el.value || '').trim().slice(0, 40),
      bg: cs.backgroundColor,
      x: Math.round(r.x), y: Math.round(r.y),
    });
  }
  return out;
}
"""


def step(msg):
    print(f"STEP  {msg}", flush=True)


def note(msg):
    print(f"      {msg}", flush=True)


def pick(page, selector, label):
    page.wait_for_selector(f"{selector} option[value]", state="attached", timeout=120000)
    for opt in page.locator(f"{selector} option").all():
        if opt.inner_text().strip() == label:
            return opt.get_attribute("value")
    return None


def main():
    step("start - which button submits?")
    posts = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(accept_downloads=True)
        page = ctx.new_page()

        def on_request(req):
            if "generate-published-pdfs" in req.url:
                posts.append({"url": req.url, "body": req.post_data})
                note(f"  >>> generate-published-pdfs REQUEST SEEN")

        page.on("request", on_request)

        step("loading form")
        for attempt in range(1, 12):
            try:
                r = page.goto(BASE_URL, timeout=120000)
                page.wait_for_selector("#stateCode option[value]",
                                       state="attached", timeout=120000)
                note(f"attempt {attempt}: HTTP {r.status}")
                break
            except Exception as exc:
                note(f"attempt {attempt} failed: {type(exc).__name__}")
                time.sleep(5)
        else:
            step("FAILED - form never rendered")
            browser.close()
            return 1

        step("filling the whole form")
        for sel, label in (("#stateCode", "Andhra Pradesh"),
                           ("#revyear", "2026")):
            val = pick(page, sel, label)
            if val:
                page.select_option(sel, value=val)
                page.wait_for_load_state("networkidle")
                time.sleep(2)
                note(f"{sel} = {val}")

        # The chained dropdowns need their parent's value resolved first.
        for sel, label in (("#roleType", "SIR DraftRoll - 2026"),
                           ("#district", "Srikakulam"),
                           ("#constituency", "1 - Ichchapuram"),
                           ("#langCd", "ENGLISH")):
            val = pick(page, sel, label)
            if val is None:
                note(f"{sel}: {label!r} not found")
                continue
            page.select_option(sel, value=val)
            page.wait_for_load_state("networkidle")
            time.sleep(3)
            note(f"{sel} = {val}")

        step("listing every clickable element")
        for b in page.evaluate(COLLECT_JS):
            note(f"  <{b['tag']} class={b['cls']!r}> text={b['text']!r} "
                 f"bg={b['bg']} at ({b['x']},{b['y']})")

        # Fill the captcha with a deliberately wrong value. We only care WHICH
        # button reaches the API -- both will be rejected, and rejection still
        # proves the request was made.
        img = page.locator("img[alt*='aptcha'], img[id*='captcha']").first
        if img.count():
            src = img.get_attribute("src") or ""
            if src.startswith("data:"):
                raw = base64.b64decode(src.split(",", 1)[1])
                (ROOT / "probe2_captcha.png").write_bytes(raw)
                note(f"captcha image captured: {len(raw):,} bytes")

        for label_text in ("Search", "download selected pdfs"):
            step(f"clicking '{label_text}'")
            before = len(posts)
            page.fill("#captcha", "ZZZ999")
            try:
                page.click(f"text={label_text}", timeout=10000)
                page.wait_for_timeout(8000)
            except Exception as exc:
                note(f"  click failed: {type(exc).__name__}")
            after = len(posts)
            note(f"  generate-published-pdfs requests: {after - before}")
            if posts:
                note(f"  POST BODY: {posts[-1]['body']}")
                note("  => THIS BUTTON SUBMITS")
            else:
                note("  => no submit request from this button")

        page.screenshot(path=str(ROOT / "probe_two_buttons.png"), full_page=True)
        browser.close()

    return 0


if __name__ == "__main__":
    sys.exit(main())
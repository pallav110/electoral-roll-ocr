"""Enumerate every button on the filled form, with classes, colour and position.

The flow clicks a single `button.btn.btn-block.submit`, but the user reports
two buttons on the form -- blue on the right, green on the left. If that is
real, the click may be hitting the wrong one and the API never sees a submit.

This measures rather than assumes: it fills the form, then records every
button/anchor that looks clickable, including its computed background colour
and its viewport position, so "blue on the right, green on the left" can be
matched to actual selectors.

Run:  python scripts/probe_buttons.py
"""
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

# Runs in the page: for each candidate, report what a user would see and where.
COLLECT_JS = """
() => {
  const out = [];
  const nodes = document.querySelectorAll('button, input[type=submit], input[type=button], a.btn, a[role=button]');
  for (const el of nodes) {
    const r = el.getBoundingClientRect();
    const cs = getComputedStyle(el);
    out.push({
      tag: el.tagName.toLowerCase(),
      type: el.getAttribute('type'),
      id: el.id || null,
      cls: el.className && el.className.toString ? el.className.toString() : null,
      text: (el.innerText || el.value || '').trim().slice(0, 60),
      visible: !!(r.width && r.height),
      w: Math.round(r.width),
      h: Math.round(r.height),
      x: Math.round(r.x),
      y: Math.round(r.y),
      bg: cs.backgroundColor,
      color: cs.color,
      disabled: !!el.disabled,
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
    page.wait_for_selector(f"{selector} option[value]", state="attached", timeout=90000)
    for opt in page.locator(f"{selector} option").all():
        if opt.inner_text().strip() == label:
            return opt.get_attribute("value")
    return None


def show(buttons, title):
    step(title)
    visible = [b for b in buttons if b["visible"]]
    note(f"{len(buttons)} candidates, {len(visible)} visible")
    for b in visible:
        note(f"  <{b['tag']}{' type=' + b['type'] if b['type'] else ''}> "
             f"id={b['id']!r} cls={b['cls']!r}")
        note(f"      text={b['text']!r} bg={b['bg']} color={b['color']} "
             f"pos=({b['x']},{b['y']}) {b['w']}x{b['h']} "
             f"disabled={b['disabled']}")


def main():
    step("start - enumerating buttons on the ECI download form")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(accept_downloads=True)
        page = ctx.new_page()

        step("loading form")
        for attempt in range(1, 13):
            try:
                r = page.goto(BASE_URL, timeout=90000)
                page.wait_for_selector("#stateCode option[value]",
                                       state="attached", timeout=45000)
                note(f"attempt {attempt}: HTTP {r.status}, form rendered")
                break
            except Exception as exc:
                note(f"attempt {attempt} failed: {type(exc).__name__}")
                time.sleep(4)
        else:
            step("FAILED - form never rendered")
            browser.close()
            return 1

        show(page.evaluate(COLLECT_JS), "Buttons on the EMPTY form")

        for sel, label in (("#stateCode", "Andhra Pradesh"),
                           ("#langCd", "ENGLISH")):
            val = pick(page, sel, label)
            if val is None:
                note(f"{sel}: {label!r} not found")
                continue
            page.select_option(sel, value=val)
            page.wait_for_load_state("networkidle")
            time.sleep(3)
            note(f"{sel} = {val}")

        show(page.evaluate(COLLECT_JS), "Buttons on the FILLED form (with captcha)")

        # Does the selector the downloader actually clicks exist, and is it
        # unique? A non-unique match would silently click the first one.
        step("Does 'button.btn.btn-block.submit' resolve?")
        loc = page.locator("button.btn.btn-block.submit")
        note(f"count = {loc.count()}")
        for i in range(loc.count()):
            el = loc.nth(i)
            note(f"  [{i}] text={(el.inner_text() or '').strip()!r} "
                 f"visible={el.is_visible()}")

        # Any captcha-specific controls (the voice-captcha button is a likely
        # candidate for the second button the user described).
        step("Anything captcha-related that is clickable")
        for sel in ("#captcha", "img[alt*='aptcha']", "audio", "[id*='voice']",
                    "[class*='voice']", "[id*='refresh']", "[class*='refresh']"):
            n = page.locator(sel).count()
            if n:
                note(f"  {sel}: {n}")
        page.screenshot(path=str(ROOT / "probe_buttons.png"), full_page=True)
        note("wrote probe_buttons.png")

        browser.close()

    step("done")
    return 0


if __name__ == "__main__":
    sys.exit(main())
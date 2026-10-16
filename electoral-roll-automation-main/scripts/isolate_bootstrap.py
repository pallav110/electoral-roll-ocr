"""Isolate WHY the form never renders: does the bundle load, and does the XHR
that fills the state dropdown actually fire and what does it return?

Every previous run collapsed this into one TimeoutError. This records the full
network story of a single page load, plus the live DOM state of #stateCode, so
the failure is pinned to a request rather than a symptom.

Run:  python scripts/isolate_bootstrap.py
"""
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from playwright.sync_api import sync_playwright  # noqa: E402

BASE_URL = "https://voters.eci.gov.in/download-eroll"
LOG = ROOT / "isolate_log.md"

_n = 0
_lines = []


def step(msg):
    global _n
    _n += 1
    line = f"STEP {_n:02d}  {msg}"
    print(line, flush=True)
    _lines.append(line)


def note(msg):
    print(f"        {msg}", flush=True)
    _lines.append(f"        {msg}")


def flush():
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(f"\n\n## Run {time.strftime('%Y-%m-%d %H:%M:%S')}\n```\n")
        f.write("\n".join(_lines))
        f.write("\n```\n")
    step("Wrote isolate_log.md")


def main():
    step("Loading page once and recording every request/response")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context()
        page = ctx.new_page()

        api_calls = []
        failures = []
        console = []

        def on_response(resp):
            url = resp.url
            if "voters.eci.gov.in" not in url:
                return
            short = url.split("voters.eci.gov.in", 1)[-1]
            ctype = resp.headers.get("content-type", "")
            # The SPA shell, its bundles, and the gateway API are what matter;
            # fonts and images are noise.
            interesting = (
                resp.request.resource_type in ("document", "script", "fetch", "xhr")
                or "gateway" in url
            )
            if not interesting:
                return
            entry = {
                "status": resp.status,
                "type": resp.request.resource_type,
                "url": short[:110],
                "ctype": ctype[:40],
            }
            api_calls.append(entry)
            note(f"{resp.status} {resp.request.resource_type:9} {ctype[:28]:28} {short[:95]}")

            # If a gateway call failed, its body says why.
            if "gateway" in url and resp.status >= 400:
                try:
                    body = resp.body()
                    note(f"    ERROR BODY: {body[:400]!r}")
                except Exception as e:
                    note(f"    (could not read body: {e})")

        def on_request_failed(req):
            failures.append(f"{req.method} {req.url[:110]} :: {req.failure}")
            note(f"REQ FAILED {req.method} {req.url[:100]} :: {req.failure}")

        page.on("response", on_response)
        page.on("requestfailed", on_request_failed)
        page.on("console", lambda m: console.append(f"[{m.type}] {m.text}"))

        step("GET the page")
        resp = page.goto(BASE_URL, timeout=120000)
        note(f"main document HTTP {resp.status if resp else 'None'}")

        step("Waiting 45s for the SPA to boot (no selector wait - observe instead)")
        time.sleep(45)

        step("Inspecting the live DOM")
        dom = page.evaluate("""() => {
            const out = {};
            out.title = document.title;
            const root = document.querySelector('#root');
            out.rootChildren = root ? root.children.length : 'no #root';
            out.bodyText = (document.body.innerText || '').slice(0, 400);
            const sel = document.querySelector('#stateCode');
            if (sel) {
                const opts = Array.from(sel.options);
                out.stateOptionCount = opts.length;
                out.stateOptions = opts.slice(0, 6).map(o => ({v: o.value, t: o.text}));
            } else {
                out.stateCode = 'SELECT NOT IN DOM';
            }
            out.forms = document.querySelectorAll('form').length;
            out.scripts = document.querySelectorAll('script').length;
            return out;
        }""")
        note(json.dumps(dom, indent=2))

        step("Reporting")
        note(f"total recorded responses: {len(api_calls)}")
        note(f"failed requests: {len(failures)}")
        for f in failures:
            note(f"    {f}")

        gateway = [c for c in api_calls if "gateway" in c["url"]]
        note(f"gateway calls: {len(gateway)}")
        for g in gateway:
            note(f"    {g['status']} {g['url'][:100]}")

        step("Console output (errors only)")
        for c in console:
            if "error" in c.lower() or "error" in c[:20].lower():
                note(f"    {c[:250]}")

        page.screenshot(path=str(ROOT / "isolate_after_load.png"), full_page=True)
        step("Screenshot: isolate_after_load.png")

        browser.close()

    flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
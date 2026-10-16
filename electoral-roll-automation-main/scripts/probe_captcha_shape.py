"""Dump every captcha-related response the live page makes.

The submit body needs `captchaId`, which comes from the getCaptcha response's
`id`. Reading it as `payload.id` is a guess made from the site's other
endpoints; this prints the bytes so the guess can be checked instead of
inferred.

Run:  .venv-test/bin/python -u scripts/probe_captcha_shape.py
"""
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")

from playwright.sync_api import sync_playwright  # noqa: E402

from eci_downloader import BASE_URL, HEADLESS, ECIDownloader  # noqa: E402


def main():
    seen = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=HEADLESS)
        page = browser.new_page()

        # Everything captcha-ish, whatever the URL looks like. The endpoint
        # name may differ from what the bundle calls it, and a filter guessed
        # from the bundle is exactly what is being tested here.
        def on_response(resp):
            url = resp.url.lower()
            if "captcha" not in url:
                return
            entry = {"url": resp.url, "status": resp.status}
            try:
                body = resp.body()
                entry["len"] = len(body)
                entry["body"] = body[:600].decode("utf-8", "replace")
            except Exception as exc:
                entry["error"] = f"{type(exc).__name__}: {exc}"
            seen.append(entry)
            print(f"\n>>> {resp.status} {resp.url}")
            if "error" in entry:
                print(f"    body unreadable: {entry['error']}")
            else:
                print(f"    {entry['len']} bytes: {entry['body'][:400]}")

        page.on("response", on_response)

        print(f"loading {BASE_URL}")
        for attempt in range(1, 13):
            try:
                page.goto(BASE_URL, timeout=90000)
                page.wait_for_selector("#stateCode option[value]",
                                       state="attached", timeout=60000)
                print(f"\nform live on attempt {attempt}")
                break
            except Exception as exc:
                print(f"attempt {attempt}: {type(exc).__name__}")
                time.sleep(10)
        else:
            print("form never loaded")
            browser.close()
            return 1

        # Give any late captcha refetch time to land.
        time.sleep(5)

        print("\n" + "=" * 70)
        print(f"{len(seen)} captcha response(s)")
        for e in seen:
            print(f"  {e['status']} {e['url'][:100]}")
            print(f"    {e.get('body', e.get('error'))[:300]}")

        # Also report what the in-page captcha element looks like, since the id
        # may be embedded in the markup rather than only in the JSON.
        markup = page.evaluate("""() => {
            const c = document.querySelector('#captcha');
            const img = c ? c.parentElement.querySelector('img') : null;
            return {
                field: c ? c.outerHTML.slice(0, 300) : null,
                imgAttrs: img ? Array.from(img.attributes).map(
                    a => `${a.name}=${a.value.slice(0, 40)}`) : null,
            };
        }""")
        print("\nCAPTCHA ELEMENT:")
        for k, v in markup.items():
            print(f"  {k}: {v}")

        browser.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
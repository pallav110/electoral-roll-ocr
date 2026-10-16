"""Get form attributes via evaluate."""
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
        browser = p.chromium.launch(headless=False, slow_mo=200)
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
            if resp.request.method == "POST" and ("download" in resp.url or "printing" in resp.url or "part" in resp.url):
                try:
                    body = resp.body()
                    if body:
                        print(f"    [POST RESPONSE BODY] status={resp.status} len={len(body)} ctype={resp.headers.get('content-type','')}", flush=True)
                        if body[:4] == b'PK\x03\x04':
                            print(f"    *** ZIP FILE DETECTED ***", flush=True)
                            (ROOT / "captured_zip.zip").write_bytes(body)
                except Exception as e:
                    print(f"    [BODY ERROR] {e}", flush=True)

        page.on("response", on_response)

        page.goto(BASE_URL, timeout=90000)
        page.wait_for_selector("#stateCode option[value]", state="attached", timeout=60000)
        print("[*] Form loaded")

        # Get form attributes via evaluate
        form_info = page.evaluate("""() => {
            const f = document.querySelector('form');
            if (!f) return null;
            return {
                action: f.action,
                method: f.method,
                target: f.target,
                onsubmit: f.onsubmit ? f.onsubmit.toString() : null,
                outerHTML: f.outerHTML.substring(0, 500)
            };
        }""")
        print(f"    Form: {form_info}", flush=True)

        page.select_option("#stateCode", value=STATE_VALUE)
        page.wait_for_load_state("networkidle")
        time.sleep(1)
        page.select_option("#revyear", value=YEAR_VALUE)
        page.wait_for_selector("#roleType option[value]", state="attached", timeout=60000)
        page.wait_for_load_state("networkidle")
        time.sleep(1)

        for opt in page.locator("#roleType option").all():
            if opt.get_attribute("value"):
                page.select_option("#roleType", value=opt.get_attribute("value"))
                break
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

        print("[*] Fill captcha manually and click submit. Watching for 30s...")
        time.sleep(30)

        print(f"\n[*] Total responses: {len(responses)}")
        for r in responses:
            print(f"  {r['status']} {r['method']:6} {r['type']:8} {r['ctype'][:40]:40} {r['url'][:120]}")

        browser.close()


if __name__ == "__main__":
    main()

"""Check which roll type exposes the constituencies in discovered_structure.json.

The structure records district "Central" holding 23 - KAROL BAGH, 24 - PATEL
NAGAR and so on. A live probe of the same district showed 1 - NERELA, 2 - BURARI,
3 - TIMARPUR instead, which suggests the roll type selected there was not the one
the structure was recorded against. This walks every roll type for the district
and reports which one actually contains KAROL BAGH.

    .venv-test/bin/python -u scripts/probe_selectors.py
"""
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE_URL = "https://voters.eci.gov.in/download-eroll"
STATE_VALUE = "U05"   # NCT OF Delhi
YEAR_VALUE = "2025"
TARGET_CONSTITUENCY = "KAROL BAGH"


def options(page, selector):
    page.wait_for_selector(f"{selector} option", state="attached", timeout=30000)
    return [(o.inner_text().strip(), o.get_attribute("value"))
            for o in page.locator(f"{selector} option").all()]


def main():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_context().new_page()

        for attempt in range(1, 6):
            resp = page.goto(BASE_URL, timeout=90000)
            print(f"[load attempt {attempt}] HTTP {resp.status if resp else None}",
                  flush=True)
            if (resp is None or resp.status == 200):
                try:
                    page.wait_for_selector("#stateCode option[value]",
                                           state="attached", timeout=60000)
                    break
                except Exception as exc:
                    print("   form did not render:", type(exc).__name__, flush=True)
            time.sleep(15)

        page.select_option("#stateCode", value=STATE_VALUE)
        page.wait_for_load_state("networkidle")
        time.sleep(2)
        page.select_option("#revyear", value=YEAR_VALUE)
        page.wait_for_load_state("networkidle")
        time.sleep(2)

        rolls = options(page, "#roleType")
        print(f"\nRoll types for {YEAR_VALUE}: "
              f"{[r[0] for r in rolls]}", flush=True)

        for roll_label, roll_value in rolls:
            if not roll_value:
                continue
            page.select_option("#roleType", value=roll_value)
            page.wait_for_load_state("networkidle")
            time.sleep(2)

            districts = [d[0] for d in options(page, "#district")]
            print(f"\n=== {roll_label} === districts: {districts}", flush=True)

            for dist_label, dist_value in options(page, "#district"):
                if not dist_value or dist_label != "Central":
                    continue
                page.select_option("#district", value=dist_value)
                page.wait_for_load_state("networkidle")
                time.sleep(2)
                consts = options(page, "#constituency")
                labels = [c[0] for c in consts]
                hit = TARGET_CONSTITUENCY in " ".join(labels)
                print(f"  Central -> {len(labels)} constituencies; "
                      f"contains {TARGET_CONSTITUENCY}: {hit}", flush=True)
                print(f"    first 8: {labels[:8]}", flush=True)

        browser.close()


if __name__ == "__main__":
    main()

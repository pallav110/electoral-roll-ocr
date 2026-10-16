"""Report the language codes the site offers for a constituency.

`download_all_parts_for_combination` fails with "Language not found: ENGLISH",
which means the language list depends on the roll type and is not simply
{ENGLISH, HINDI, URDU} for every combination. This walks a few combinations and
prints exactly what is offered, so the mismatch is visible rather than inferred.

    .venv-test/bin/python -u scripts/probe_languages.py
"""
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE_URL = "https://voters.eci.gov.in/download-eroll"
STATE_VALUE = "U05"        # NCT OF Delhi
COMBOS = [
    ("2026", "SIR DraftRoll - 2026", "Central", "23 - KAROL BAGH"),
    ("2025", "Final Roll - 2025", "Central", "23 - KAROL BAGH"),
]


def opts(page, selector):
    try:
        page.wait_for_selector(f"{selector} option", state="attached", timeout=30000)
    except Exception:
        return []
    return [(o.inner_text().strip(), o.get_attribute("value"))
            for o in page.locator(f"{selector} option").all()]


def main():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_context().new_page()

        loaded = False
        for attempt in range(1, 6):
            resp = page.goto(BASE_URL, timeout=90000)
            print(f"[load {attempt}] HTTP {resp.status if resp else None}", flush=True)
            if resp is None or resp.status == 200:
                try:
                    page.wait_for_selector("#stateCode option[value]",
                                           state="attached", timeout=60000)
                    loaded = True
                    break
                except Exception:
                    pass
            time.sleep(20 * attempt)
        if not loaded:
            print("[!] form never loaded", flush=True)
            browser.close()
            return 1

        for year, roll_label, dist_label, const_label in COMBOS:
            page.goto(BASE_URL, timeout=90000)
            page.wait_for_selector("#stateCode option[value]",
                                   state="attached", timeout=60000)

            page.select_option("#stateCode", value=STATE_VALUE)
            page.wait_for_load_state("networkidle")
            time.sleep(2)
            page.select_option("#revyear", value=year)
            page.wait_for_selector("#roleType option[value]", state="attached",
                                   timeout=30000)
            time.sleep(2)

            roll_value = next((v for t, v in opts(page, "#roleType")
                               if t == roll_label), None)
            if not roll_value:
                print(f"{year} / {roll_label}: roll type not offered", flush=True)
                continue
            page.select_option("#roleType", value=roll_value)
            page.wait_for_load_state("networkidle")
            time.sleep(2)

            dist_value = next((v for t, v in opts(page, "#district")
                               if t == dist_label), None)
            if not dist_value:
                print(f"{year} / {roll_label}: district {dist_label} absent",
                      flush=True)
                continue
            page.select_option("#district", value=dist_value)
            page.wait_for_load_state("networkidle")
            time.sleep(2)

            const_value = next((v for t, v in opts(page, "#constituency")
                                if t == const_label), None)
            if not const_value:
                print(f"{year} / {roll_label}: {const_label} absent", flush=True)
                continue
            page.select_option("#constituency", value=const_value)
            time.sleep(3)

            langs = opts(page, "#langCd")
            print(f"\n{year} / {roll_label} / {dist_label} / {const_label}",
                  flush=True)
            print(f"  languages offered ({len(langs)}): "
                  f"{[(t, v) for t, v in langs]}", flush=True)
            print(f"  parts table present: "
                  f"{page.locator('table.datatable.contenttable-eroll').count()}",
                  flush=True)

        browser.close()


if __name__ == "__main__":
    main()

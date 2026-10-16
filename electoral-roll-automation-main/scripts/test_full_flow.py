#!/usr/bin/env python3
"""
Full end-to-end test for ONE combination:
- Fill form completely
- Solve captcha
- Submit and download
- Verify PDF/ZIP
"""
import os
import re
import time
import base64
import sys
from pathlib import Path
from dotenv import load_dotenv
load_dotenv()

sys.path.insert(0, str(Path(__file__).parent.parent))

from playwright.sync_api import sync_playwright
from src.eci_downloader import ECIDownloader, BASE_URL, page_fetch, EROLL_MEDIA_BASE, GATEWAY_URL, MIS_KEY, HEADLESS

# Combination under test. Defaults are one real NCT of Delhi combination taken
# from discovered_structure.json; override any of them on the command line to
# test a different one without editing this file:
#
#   python scripts/test_full_flow.py --state S01 --district Srikakulam \
#       --constituency "1 - Ichchapuram"
#
# state_codes.json was dumped from the live #stateCode dropdown. The codes are
# NOT alphabetical by state name -- U05 is NCT OF Delhi while S24 is Uttar
# Pradesh -- and discovered_structure.json records labels only, so the code
# has to come from that table rather than be inferred. Getting this wrong is
# silent: the form accepts the code, then refills the downstream dropdowns
# with the WRONG state's districts (selecting S24 offered Agra, Aligarh,
# Ambedkar Nagar...) and fails at the district step instead of the state one.
import argparse

_ap = argparse.ArgumentParser()
_ap.add_argument("--state", default="U05")
_ap.add_argument("--state-name", default="NCT OF Delhi")
_ap.add_argument("--year", default="2026")
_ap.add_argument("--roll-type", default="SIR DraftRoll - 2026")
_ap.add_argument("--district", default="Central")
_ap.add_argument("--constituency", default="23 - KAROL BAGH")
_ap.add_argument("--language", default="ENGLISH")
_ap.add_argument("--max-parts", type=int, default=10)
ARGS = _ap.parse_args()

downloader = ECIDownloader()


def die(page, browser, msg):
    print(f'[!] {msg}')
    browser.close()
    exit(1)

with sync_playwright() as p:
    browser = p.chromium.launch(headless=HEADLESS)
    context = browser.new_context(accept_downloads=True)
    page = context.new_page()
    downloader.attach_status_probe(page)
    # Must precede _open_form: the 503s arrive during the page load, and a
    # response cannot be replayed after the fact.
    downloader._install_5xx_retry(page)

    print('[*] Loading form...')
    if not downloader._open_form(page):
        print('[!] Form never loaded')
        browser.close()
        exit(1)
    print('[+] Form loaded')

    # Attach part listener EARLY (before any selections)
    downloader._part_responses = downloader._read_api_response(page, "get-publish-part-list")
    # Captcha listener will be attached after form is filled (before submit)

    # Fill form step by step with logging
    print(f'\n[*] Selecting state: {ARGS.state} ({ARGS.state_name})')
    page.select_option('#stateCode', value=ARGS.state)
    page.wait_for_load_state('networkidle')
    time.sleep(2)

    # Confirm the code meant the state we asked for. A wrong-but-valid code is
    # accepted silently and then refills every downstream dropdown from a
    # different state, which surfaces much later as a confusing "district not
    # found" instead of a wrong-state error.
    actual_state = downloader._selected_label(page, '#stateCode')
    if actual_state and ARGS.state_name and actual_state.strip().lower() != ARGS.state_name.strip().lower():
        die(page, browser, f'State code {ARGS.state} is {actual_state!r}, '
                           f'not {ARGS.state_name!r} - check state_codes.json')

    print(f'[*] Selecting year: {ARGS.year}')
    page.select_option('#revyear', value=ARGS.year)
    page.wait_for_load_state('networkidle')
    time.sleep(2)

    print(f'[*] Selecting roll type: {ARGS.roll_type}')
    roll_value = downloader._find_option_value(page, '#roleType', ARGS.roll_type)
    if roll_value:
        page.select_option('#roleType', value=roll_value)
        page.wait_for_load_state('networkidle')
        time.sleep(2)
    else:
        die(page, browser, f'Roll type not found: {ARGS.roll_type}')

    print(f'[*] Selecting district: {ARGS.district}')
    dist_value = downloader._find_option_value(page, '#district', ARGS.district)
    if dist_value:
        page.select_option('#district', value=dist_value)
        page.wait_for_load_state('networkidle')
        time.sleep(2)
    else:
        die(page, browser, f'District not found: {ARGS.district}')

    print(f'[*] Selecting constituency: {ARGS.constituency}')
    const_value = downloader._find_option_value(page, '#constituency', ARGS.constituency)
    if const_value:
        page.select_option('#constituency', value=const_value)
        page.wait_for_load_state('networkidle')
        time.sleep(2)
    else:
        die(page, browser, f'Constituency not found: {ARGS.constituency}')

    print(f'[*] Selecting language: {ARGS.language}')
    # Language dropdown populates after constituency - wait for it
    page.wait_for_load_state('networkidle')
    time.sleep(3)
    lang_value = downloader._find_option_value(page, '#langCd', ARGS.language)
    if lang_value:
        page.select_option('#langCd', value=lang_value)
        page.wait_for_load_state('networkidle')
        time.sleep(3)
    else:
        die(page, browser, f'Language not found: {ARGS.language}')

    # Wait for parts table, re-triggering the request if it comes back empty.
    # The part list is fetched on language selection, and that call is one of
    # the same 503-prone endpoints: measured, a fresh load of this exact
    # combination returned get-publish-part-list 200 with 10 rows and 10
    # checkboxes, while an identical run produced an empty table that never
    # recovered. Re-selecting the language makes the SPA issue the call again.
    print('[*] Waiting for parts table...')
    for attempt in range(1, 4):
        try:
            downloader._wait_for_parts_table(page, timeout=30)
            break
        except TimeoutError:
            print(f'    [!] parts table empty on attempt {attempt}/3 - '
                  f're-selecting language to re-trigger the request')
            page.select_option('#langCd', value=lang_value)
            page.wait_for_load_state('networkidle')
            time.sleep(3)
    else:
        die(page, browser, 'parts table stayed empty after 3 attempts')

    # Check captured part numbers
    api_part_numbers = downloader._part_numbers_from_responses(downloader._part_responses)
    print(f'[*] Part numbers from API: {api_part_numbers}')

    # Select up to MAX parts
    print('[*] Selecting parts...')
    part_names = downloader._check_parts_on_current_page(page, ARGS.max_parts)
    print(f'[*] Selected {len(part_names)} parts')
    for _part in part_names:
        print(f'    {_part["label"]} -> partNumber: {_part["number"]}')

    if not part_names:
        die(page, browser, 'No parts selected')

    # Build partNumberList
    part_ids = list(api_part_numbers[:len(part_names)]) if api_part_numbers else []
    part_ids += [p["number"] for p in part_names[len(part_ids):]]
    print(f'[*] partNumberList = {part_ids}')

    # NOW attach captcha listener - captcha ID comes from generateVoiceCaptcha URL
    # which is triggered when submit button is clicked
    print('[*] Attaching captcha listener (listening for generateVoiceCaptcha)...')
    downloader._captcha_responses = downloader._read_api_response(page, "generateVoiceCaptcha")

    # Solve captcha - _refresh_captcha_image triggers the captcha request by clicking submit
    print('[*] Solving captcha (refresh will trigger captcha request)...')
    downloader._refresh_captcha_image(page)
    time.sleep(2)  # Wait for captcha response to arrive
    captcha_id = downloader._fetch_captcha_id(page)
    print(f'[*] Captcha ID: {captcha_id}')
    if not captcha_id:
        # Not fatal: measured, the live form's captcha renders as an inline
        # data URI and the SPA builds the POST body itself, supplying whatever
        # id it holds. Proceed and let the server decide.
        print('[*] no captcha id captured - proceeding anyway')

    captcha_img = downloader._captcha_locator(page)
    src = captcha_img.get_attribute("src") if captcha_img.count() else None
    # Prefer the inline data URI: measured, it is a real JPEG (/9j/...), while
    # the getCaptcha response's `data` field decodes to non-image bytes.
    raw_image = None
    if src and src.startswith("data:image") and "base64," in src:
        candidate = base64.b64decode(src.split(",", 1)[1])
        if downloader._looks_like_image(candidate):
            raw_image = candidate
            print(f'    [*] captcha image from inline data URI ({len(candidate):,} bytes)')
    if raw_image is None:
        raw_image = downloader._captcha_image_bytes()
    if raw_image:
        with open('captcha.png', "wb") as f:
            f.write(raw_image)
    else:
        captcha_img.screenshot(path='captcha.png')
        print('    [*] captcha image from element screenshot')

    captcha_code = downloader.solve_captcha_with_vision('captcha.png')
    print(f'[*] Captcha code: {captcha_code}')

    if captcha_code == "MANUAL":
        die(page, browser, 'Captcha requires manual solving')

    # Submit
    print('[*] Submitting form...')
    generate_responses = downloader._read_api_response(page, "generate-published-pdfs")

    page.fill("#captcha", captcha_code)
    page.click("button.btn.btn-block.submit")
    page.wait_for_timeout(3000)

    if not generate_responses:
        print('[!] No generate-published-pdfs request seen')
        browser.close()
        exit(1)

    entry = generate_responses[-1]
    body = entry.get("json", {})
    print(f'[*] Generate response: HTTP {entry["status"]} body={body}')

    status = body.get("statusCode") if isinstance(body, dict) else None
    if status != 200:
        print(f'[!] Rejected: {body.get("message")}')
        browser.close()
        exit(1)

    payload = body.get("payload") if isinstance(body, dict) else None
    if isinstance(payload, list):
        payload = payload[0] if payload else None
    if not payload:
        print(f'[!] Empty payload: {body}')
        browser.close()
        exit(1)

    print(f'[+] Accepted! Fetching: {payload}')
    zip_content = downloader._download_result(page, payload)

    if not zip_content:
        print('[!] File fetch returned nothing')
        browser.close()
        exit(1)

    # Verify content
    kind = downloader._sniff_kind(zip_content)
    print(f'[+] Downloaded {len(zip_content):,} bytes, type: {kind}')

    if kind == "html":
        sample = zip_content[:300].decode("utf-8", "replace")
        print(f'[!] Got HTML error page: {sample[:200]}')
        browser.close()
        exit(1)

    # Save
    from pathlib import Path
    from datetime import datetime
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    ext = {"zip": "zip", "pdf": "pdf"}.get(kind, "bin")
    slug = "_".join([
        "test", ARGS.state, ARGS.year,
        re.sub(r"\W+", "", ARGS.roll_type),
        re.sub(r"\W+", "", ARGS.district),
        re.sub(r"\W+", "", ARGS.constituency),
        ARGS.language,
    ])
    filename = f"{slug}_{timestamp}.{ext}"
    save_path = Path("downloaded_pdfs") / filename
    save_path.parent.mkdir(parents=True, exist_ok=True)
    save_path.write_bytes(zip_content)
    print(f'[+] Saved to: {save_path}')

    # If ZIP, list contents
    if kind == "zip":
        import zipfile
        with zipfile.ZipFile(save_path, 'r') as zf:
            print('[*] ZIP contents:')
            for name in zf.namelist():
                print(f'    {name}')

    browser.close()
    print('\n[+] FULL FLOW TEST PASSED!')
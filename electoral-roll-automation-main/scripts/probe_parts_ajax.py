#!/usr/bin/env python3
"""
Probe the parts table AJAX race - determine when get-publish-part-list fires
and why the table stays empty.
"""
import os
import time
from dotenv import load_dotenv
load_dotenv()

from playwright.sync_api import sync_playwright
from src.eci_downloader import ECIDownloader, BASE_URL

downloader = ECIDownloader()

with sync_playwright() as p:
    browser = p.chromium.launch(headless=False)
    page = browser.new_page()

    if downloader._open_form(page):
        print('[+] Form loaded')

        # Attach part listener BEFORE any selections
        print('[*] Attaching get-publish-part-list listener...')
        part_responses_early = downloader._read_api_response(page, 'get-publish-part-list')

        # Select state
        page.select_option('#stateCode', value='S01')
        page.wait_for_load_state('networkidle')
        time.sleep(2)

        # Select year
        page.select_option('#revyear', value='2026')
        page.wait_for_load_state('networkidle')
        time.sleep(2)

        # Select roll type
        roll_value = downloader._find_option_value(page, '#roleType', 'SIR DraftRoll - 2026')
        if roll_value:
            page.select_option('#roleType', value=roll_value)
            page.wait_for_load_state('networkidle')
            time.sleep(2)

        # Select district
        dist_value = downloader._find_option_value(page, '#district', 'Srikakulam')
        if dist_value:
            page.select_option('#district', value=dist_value)
            page.wait_for_load_state('networkidle')
            time.sleep(2)

        # Select constituency - this should trigger get-publish-part-list
        const_value = downloader._find_option_value(page, '#constituency', '1 - Ichchapuram')
        if const_value:
            page.select_option('#constituency', value=const_value)
            page.wait_for_load_state('networkidle')
            time.sleep(3)  # Longer wait for AJAX

        # Check parts API responses
        print('[*] Checking captured responses after constituency...')
        part_responses = downloader._read_api_response(page, 'get-publish-part-list')
        print(f'    get-publish-part-list responses (early listener): {len(part_responses_early)}')
        print(f'    get-publish-part-list responses (late listener): {len(part_responses)}')
        for i, r in enumerate(part_responses):
            print(f'      Response {i}: status={r.get("status")}, keys={list(r.get("json", {}).keys())}')
            if 'json' in r and 'payload' in r['json']:
                payload = r['json']['payload']
                print(f'        payload type: {type(payload)}')
                if isinstance(payload, list) and payload:
                    print(f'        first item: {payload[0]}')

        # Select language
        lang_value = downloader._find_option_value(page, '#langCd', 'ENGLISH')
        if lang_value:
            page.select_option('#langCd', value=lang_value)
            page.wait_for_load_state('networkidle')
            time.sleep(3)
        else:
            # Try fallback - select first available
            lang_value = downloader._find_option_value(page, '#langCd', 'HINDI')
            if lang_value:
                page.select_option('#langCd', value=lang_value)
                page.wait_for_load_state('networkidle')
                time.sleep(3)

        # Check parts API responses again
        part_responses2 = downloader._read_api_response(page, 'get-publish-part-list')
        print(f'    After language: get-publish-part-list responses: {len(part_responses2)}')

        # Check table
        try:
            page.wait_for_selector('table.datatable.contenttable-eroll', timeout=10000)
            print('[+] Parts table found')
            rows = page.locator('table.datatable.contenttable-eroll tbody tr').all()
            print(f'Rows: {len(rows)}')
        except:
            print('[!] Parts table not found')

            # Diagnostic
            diag = page.evaluate("""() => ({
                tables: Array.from(document.querySelectorAll('table')).map(t => t.className || '(no class)').slice(0, 8),
                rows: document.querySelectorAll('tbody tr').length,
                checkboxes: document.querySelectorAll("input[type='checkbox']").length,
            })""")
            print(f'    DOM: {diag}')
    else:
        print('[!] Form never loaded')

    input('Press Enter to close...')
    browser.close()
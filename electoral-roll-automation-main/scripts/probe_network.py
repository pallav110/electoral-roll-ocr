#!/usr/bin/env python3
"""
Probe ALL network requests to find the actual parts API endpoint.
"""
import os
import sys
import time
from pathlib import Path
from dotenv import load_dotenv
load_dotenv()

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from playwright.sync_api import sync_playwright
from src.eci_downloader import ECIDownloader, BASE_URL

downloader = ECIDownloader()

all_responses = []

def capture_all_responses(response):
    url = response.url
    if any(x in url for x in ['api', 'captcha', 'part', 'publish', 'printing']):
        try:
            body = response.body()
            status = response.status
            all_responses.append({
                'url': url,
                'status': status,
                'body_preview': body[:200].decode('utf-8', 'replace') if body else ''
            })
        except:
            pass

with sync_playwright() as p:
    browser = p.chromium.launch(headless=False)
    page = browser.new_page()
    page.on("response", capture_all_responses)

    if downloader._open_form(page):
        print('[+] Form loaded')

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

        # Select constituency
        const_value = downloader._find_option_value(page, '#constituency', '1 - Ichchapuram')
        if const_value:
            page.select_option('#constituency', value=const_value)
            page.wait_for_load_state('networkidle')
            time.sleep(3)

        # Select language
        lang_value = downloader._find_option_value(page, '#langCd', 'ENGLISH')
        if lang_value:
            page.select_option('#langCd', value=lang_value)
            page.wait_for_load_state('networkidle')
            time.sleep(3)

        # Wait for table
        try:
            page.wait_for_selector('table.datatable.contenttable-eroll', timeout=10000)
            print('[+] Parts table found')
            rows = page.locator('table.datatable.contenttable-eroll tbody tr').all()
            print(f'Rows: {len(rows)}')
        except:
            print('[!] Parts table not found')

    print('\n=== ALL RELEVANT NETWORK RESPONSES ===')
    for r in all_responses:
        print(f"\nURL: {r['url']}")
        print(f"Status: {r['status']}")
        print(f"Body: {r['body_preview']}")

    browser.close()
from playwright.sync_api import sync_playwright
import time
import json

BASE_URL = 'https://voters.eci.gov.in/download-eroll'

with sync_playwright() as p:
    browser = p.chromium.launch(headless=False)  # visible
    context = browser.new_context()
    page = context.new_page()
    page.goto(BASE_URL)
    page.wait_for_load_state('networkidle')
    time.sleep(3)

    # Check what's on the page
    print("Page title:", page.title())

    # Get all states
    state_select = page.locator('#stateCode')
    print(f"State select count: {state_select.count()}")
    state_options = state_select.locator('option').all()
    print(f"State options count: {len(state_options)}")
    for opt in state_options:
        val = opt.get_attribute('value')
        text = opt.inner_text().strip()
        print(f'  value="{val}" text="{text}"')

    # Check if there are any iframes
    frames = page.frames
    print(f"\nFrames: {len(frames)}")
    for frame in frames:
        print(f"  Frame: {frame.url}")

    time.sleep(5)
    browser.close()
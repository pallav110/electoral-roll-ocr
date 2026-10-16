import time
from playwright.sync_api import sync_playwright

BASE_URL = "https://voters.eci.gov.in/download-eroll"

with sync_playwright() as p:
    browser = p.chromium.launch(headless=False)
    context = browser.new_context()
    page = context.new_page()
    page.goto(BASE_URL)
    page.wait_for_load_state("networkidle")

    # Select Delhi
    page.select_option("#stateCode", value="U05")
    page.wait_for_load_state("networkidle")
    time.sleep(2)

    # Select Year
    page.select_option("#revyear", value="2024")
    page.wait_for_load_state("networkidle")
    time.sleep(2)

    # Check ALL select elements now
    selects = page.locator("select").all()
    print(f"\nTotal selects found: {len(selects)}")
    for i, sel in enumerate(selects):
        sel_id = sel.get_attribute("id")
        sel_name = sel.get_attribute("name")
        options = sel.locator("option").all()
        option_texts = [opt.inner_text() for opt in options]
        print(f"  Select #{i}: id='{sel_id}', name='{sel_name}'")
        print(f"    Options: {option_texts}")

    time.sleep(10)
    browser.close()
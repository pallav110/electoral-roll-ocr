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
    time.sleep(1)

    # Select Roll Type
    page.select_option("#roleType", label="SIR DraftRoll - 2026")
    page.wait_for_load_state("networkidle")
    time.sleep(1)

    # Select District
    page.select_option("#district", label="NEW DELHI")
    page.wait_for_load_state("networkidle")
    time.sleep(1)

    # Check language options
    lang_select = page.locator("#langCd")
    options = lang_select.locator("option").all()
    print("\nLANGUAGE OPTIONS:")
    for opt in options:
        val = opt.get_attribute("value")
        text = opt.inner_text()
        print(f"  value='{val}' text='{text}'")

    time.sleep(10)
    browser.close()
import time
from playwright.sync_api import sync_playwright

BASE_URL = "https://voters.eci.gov.in/download-eroll"

with sync_playwright() as p:
    browser = p.chromium.launch(headless=False)
    context = browser.new_context()
    page = context.new_page()
    page.goto(BASE_URL)
    page.wait_for_load_state("networkidle")

    # Select Delhi using the correct value
    print("Selecting Delhi (U05)...")
    page.select_option("#stateCode", value="U05")
    page.wait_for_load_state("networkidle")
    time.sleep(3)

    # Check roll type options
    roll_select = page.locator("#rollType")
    print(f"Roll type count: {roll_select.count()}")
    if roll_select.count() > 0:
        options = roll_select.locator("option").all()
        print("\nROLL TYPE OPTIONS:")
        for opt in options:
            val = opt.get_attribute("value")
            text = opt.inner_text()
            print(f"  value='{val}' text='{text}'")
    else:
        print("Roll type dropdown not found yet")

    time.sleep(10)
    browser.close()
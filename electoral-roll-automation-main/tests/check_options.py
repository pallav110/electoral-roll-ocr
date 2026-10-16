from playwright.sync_api import sync_playwright

BASE_URL = "https://voters.eci.gov.in/download-eroll"

with sync_playwright() as p:
    browser = p.chromium.launch(headless=False)
    context = browser.new_context()
    page = context.new_page()
    page.goto(BASE_URL)
    page.wait_for_load_state("networkidle")

    # Check state options
    state_select = page.locator("#stateCode")
    options = state_select.locator("option").all()
    print("STATE OPTIONS:")
    for opt in options:
        val = opt.get_attribute("value")
        text = opt.inner_text()
        print(f"  value='{val}' text='{text}'")

    # Select Delhi to see next dropdowns
    page.select_option("#stateCode", value="07")  # Delhi's code
    page.wait_for_load_state("networkidle")
    time.sleep(2)

    # Check roll type options
    roll_select = page.locator("#rollType")
    if roll_select.count() > 0:
        options = roll_select.locator("option").all()
        print("\nROLL TYPE OPTIONS:")
        for opt in options:
            val = opt.get_attribute("value")
            text = opt.inner_text()
            print(f"  value='{val}' text='{text}'")

    input("Press Enter to close...")
    browser.close()
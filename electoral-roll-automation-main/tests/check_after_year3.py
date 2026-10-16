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
    time.sleep(5)  # Wait longer for AJAX

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

    # Check for any hidden selects
    all_selects = page.query_selector_all("select")
    print(f"\nquery_selector_all finds: {len(all_selects)} selects")

    # Check the full HTML for any rollType or district elements
    html = page.content()
    if "roleType" in html:
        print("\n'roleType' found in page HTML")
    if "district" in html:
        print("'district' found in page HTML")
    if "constituency" in html:
        print("'constituency' found in page HTML")
    if "langCd" in html:
        print("'langCd' found in page HTML")

    # Also check for any buttons
    buttons = page.locator("button").all()
    print(f"\nButtons found: {len(buttons)}")
    for btn in buttons:
        btn_id = btn.get_attribute("id")
        btn_class = btn.get_attribute("class")
        btn_text = btn.inner_text()
        print(f"  Button: id='{btn_id}', class='{btn_class}', text='{btn_text}'")

    # Check for any input elements
    inputs = page.locator("input").all()
    print(f"\nInputs found: {len(inputs)}")
    for inp in inputs:
        inp_id = inp.get_attribute("id")
        inp_name = inp.get_attribute("name")
        inp_type = inp.get_attribute("type")
        inp_class = inp.get_attribute("class")
        print(f"  Input: id='{inp_id}', name='{inp_name}', type='{inp_type}', class='{inp_class}'")

    time.sleep(10)
    browser.close()
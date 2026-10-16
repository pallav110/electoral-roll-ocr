import time
from playwright.sync_api import sync_playwright

BASE_URL = "https://voters.eci.gov.in/download-eroll"

with sync_playwright() as p:
    browser = p.chromium.launch(headless=False)
    context = browser.new_context()
    page = context.new_page()
    page.goto(BASE_URL)
    page.wait_for_load_state("networkidle")

    # Print full page HTML to understand structure
    html = page.content()
    with open("page_initial.html", "w", encoding="utf-8") as f:
        f.write(html)
    print("Initial page HTML saved to page_initial.html")

    # Select Delhi
    page.select_option("#stateCode", value="U05")
    page.wait_for_load_state("networkidle")
    time.sleep(2)

    # Select Year
    page.select_option("#revyear", value="2024")
    page.wait_for_load_state("networkidle")
    time.sleep(2)

    # Check if there's a search/submit button for state+year
    buttons = page.locator("button").all()
    print(f"\nButtons after year selection: {len(buttons)}")
    for btn in buttons:
        btn_id = btn.get_attribute("id")
        btn_class = btn.get_attribute("class")
        btn_text = btn.inner_text()
        print(f"  Button: id='{btn_id}', class='{btn_class}', text='{btn_text}'")

    # Check for any clickable elements (a, input type=submit, etc.)
    clickable = page.locator("a, input[type='submit'], input[type='button']").all()
    print(f"\nClickable elements: {len(clickable)}")
    for elem in clickable:
        tag = elem.evaluate("el => el.tagName")
        elem_id = elem.get_attribute("id")
        elem_class = elem.get_attribute("class")
        elem_text = elem.inner_text()
        print(f"  {tag}: id='{elem_id}', class='{elem_class}', text='{elem_text}'")

    # Now click the "Download Selected PDFs" button to see if it triggers the next dropdowns
    # Actually wait - that button probably submits the final form
    # Let's see if there's a "Search" or "Go" button after state+year

    # Check for any form elements
    forms = page.locator("form").all()
    print(f"\nForms found: {len(forms)}")
    for form in forms:
        form_id = form.get_attribute("id")
        form_class = form.get_attribute("class")
        print(f"  Form: id='{form_id}', class='{form_class}'")

    # Save HTML after year selection
    html = page.content()
    with open("page_after_year.html", "w", encoding="utf-8") as f:
        f.write(html)
    print("\nPage after year selection saved to page_after_year.html")

    time.sleep(10)
    browser.close()
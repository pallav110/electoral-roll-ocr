import time
from playwright.sync_api import sync_playwright

BASE_URL = "https://voters.eci.gov.in/download-eroll"

with sync_playwright() as p:
    browser = p.chromium.launch(headless=False)
    context = browser.new_context()
    page = context.new_page()
    page.goto(BASE_URL)
    page.wait_for_load_state("networkidle")

    # Get all states
    state_select = page.locator("#stateCode")
    state_options = state_select.locator("option").all()
    print("STATES:")
    states = []
    for opt in state_options:
        val = opt.get_attribute("value")
        text = opt.inner_text().strip()
        if val and val != "":
            states.append({"value": val, "label": text})
            print(f"  value='{val}' label='{text}'")

    # For each state, get years and subsequent dropdowns
    for state in states:
        print(f"\n{'='*60}")
        print(f"STATE: {state['label']} ({state['value']})")
        print(f"{'='*60}")

        page.select_option("#stateCode", value=state['value'])
        page.wait_for_load_state("networkidle")
        time.sleep(2)

        # Get years
        year_select = page.locator("#revyear")
        year_options = year_select.locator("option").all()
        years = []
        for opt in year_options:
            val = opt.get_attribute("value")
            text = opt.inner_text().strip()
            if val and val != "":
                years.append({"value": val, "label": text})
                print(f"  YEAR: value='{val}' label='{text}'")

        for year in years:
            page.select_option("#revyear", value=year['value'])
            page.wait_for_load_state("networkidle")
            time.sleep(2)

            # Check for alerts (like "Publish Eroll not available for this Year")
            alerts = page.locator(".library-alert, .alert").all()
            has_alert = False
            for alert in alerts:
                alert_text = alert.inner_text()
                if "not available" in alert_text.lower() or "warning" in alert_text.lower():
                    print(f"    ALERT: {alert_text}")
                    has_alert = True

            if has_alert:
                continue

            # Get Roll Types
            try:
                page.wait_for_selector("#roleType", state="attached", timeout=5000)
                roll_select = page.locator("#roleType")
                roll_options = roll_select.locator("option").all()
                roll_types = []
                for opt in roll_options:
                    val = opt.get_attribute("value")
                    text = opt.inner_text().strip()
                    if val and val != "":
                        roll_types.append({"value": val, "label": text})
                        print(f"    ROLL TYPE: value='{val}' label='{text}'")
            except:
                print(f"    ROLL TYPE: No roll type dropdown found")
                roll_types = []

            for roll_type in roll_types:
                page.select_option("#roleType", label=roll_type['label'])
                page.wait_for_load_state("networkidle")
                time.sleep(1)

                # Get Districts
                try:
                    page.wait_for_selector("#district", state="attached", timeout=5000)
                    dist_select = page.locator("#district")
                    dist_options = dist_select.locator("option").all()
                    districts = []
                    for opt in dist_options:
                        val = opt.get_attribute("value")
                        text = opt.inner_text().strip()
                        if val and val != "":
                            districts.append({"value": val, "label": text})
                            print(f"      DISTRICT: value='{val}' label='{text}'")
                except:
                    print(f"      DISTRICT: No district dropdown found")
                    districts = []

                for district in districts:
                    page.select_option("#district", label=district['label'])
                    page.wait_for_load_state("networkidle")
                    time.sleep(1)

                    # Get Constituencies
                    try:
                        page.wait_for_selector("#constituency", state="attached", timeout=5000)
                        const_select = page.locator("#constituency")
                        const_options = const_select.locator("option").all()
                        constituencies = []
                        for opt in const_options:
                            val = opt.get_attribute("value")
                            text = opt.inner_text().strip()
                            if val and val != "":
                                constituencies.append({"value": val, "label": text})
                                print(f"        CONSTITUENCY: value='{val}' label='{text}'")
                    except:
                        print(f"        CONSTITUENCY: No constituency dropdown found")
                        constituencies = []

                    # Get Languages
                    try:
                        page.wait_for_selector("#langCd", state="attached", timeout=5000)
                        lang_select = page.locator("#langCd")
                        lang_options = lang_select.locator("option").all()
                        languages = []
                        for opt in lang_options:
                            val = opt.get_attribute("value")
                            text = opt.inner_text().strip()
                            if val and val != "":
                                languages.append({"value": val, "label": text})
                                print(f"        LANGUAGE: value='{val}' label='{text}'")
                    except:
                        print(f"        LANGUAGE: No language dropdown found")
                        languages = []

    time.sleep(10)
    browser.close()
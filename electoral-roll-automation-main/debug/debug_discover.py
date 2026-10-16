from playwright.sync_api import sync_playwright
import time
import json

BASE_URL = 'https://voters.eci.gov.in/download-eroll'

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    context = browser.new_context()
    page = context.new_page()
    page.goto(BASE_URL)
    page.wait_for_load_state('networkidle')

    # Get all states
    state_select = page.locator('#stateCode')
    state_options = state_select.locator('option').all()
    states = []
    for opt in state_options:
        val = opt.get_attribute('value')
        text = opt.inner_text().strip()
        if val and val != '':
            states.append({'value': val, 'label': text})

    print(f'Found {len(states)} states')

    all_data = {}

    for state in states[:2]:  # Just first 2 states
        print(f'Discovering: {state["label"]} ({state["value"]})')
        page.select_option('#stateCode', value=state['value'])
        page.wait_for_load_state('networkidle')
        time.sleep(1)

        # Get years
        year_select = page.locator('#revyear')
        year_options = year_select.locator('option').all()
        years = []
        for opt in year_options:
            val = opt.get_attribute('value')
            text = opt.inner_text().strip()
            if val and val != '':
                years.append({'value': val, 'label': text})

        print(f'  Years: {[y["label"] for y in years]}')

        state_data = {'years': {}}

        for year in years:
            page.select_option('#revyear', value=year['value'])
            page.wait_for_load_state('networkidle')
            time.sleep(1)

            # Check for alerts
            alerts = page.locator('.library-alert, .alert').all()
            has_alert = False
            for alert in alerts:
                alert_text = alert.inner_text()
                if 'not available' in alert_text.lower() or 'warning' in alert_text.lower():
                    has_alert = True

            if has_alert:
                print(f'  Skipping year {year["label"]} - alert found')
                continue

            # Get Roll Types
            try:
                page.wait_for_selector('#roleType', state='visible', timeout=10000)
                roll_select = page.locator('#roleType')
                roll_options = roll_select.locator('option').all()
                roll_types = []
                for opt in roll_options:
                    val = opt.get_attribute('value')
                    text = opt.inner_text().strip()
                    if val and val != '':
                        roll_types.append({'value': val, 'label': text})
                print(f'  Roll types: {[r["label"] for r in roll_types]}')
            except Exception as e:
                print(f'  Error getting roll types: {e}')
                roll_types = []

            year_data = {'roll_types': {}}

            for roll_type in roll_types:
                print(f'    Selecting roll type: {roll_type["label"]}')
                page.select_option('#roleType', value=roll_type['value'])
                page.wait_for_load_state('networkidle')
                time.sleep(1)

                # Get Districts
                try:
                    page.wait_for_selector('#district', state='visible', timeout=10000)
                    dist_select = page.locator('#district')
                    dist_options = dist_select.locator('option').all()
                    districts = []
                    for opt in dist_options:
                        val = opt.get_attribute('value')
                        text = opt.inner_text().strip()
                        if val and val != '':
                            districts.append({'value': val, 'label': text})
                    print(f'      Found {len(districts)} districts: {[d["label"] for d in districts]}')
                except Exception as e:
                    print(f'      Error getting districts: {e}')
                    districts = []

                roll_data = {'districts': {}}

                for district in districts[:1]:  # Just first district
                    print(f'      Selecting district: {district["label"]}')
                    page.select_option('#district', value=district['value'])
                    page.wait_for_load_state('networkidle')
                    time.sleep(1)

                    # Get Constituencies
                    try:
                        page.wait_for_selector('#constituency', state='visible', timeout=10000)
                        const_select = page.locator('#constituency')
                        const_options = const_select.locator('option').all()
                        constituencies = []
                        for opt in const_options:
                            val = opt.get_attribute('value')
                            text = opt.inner_text().strip()
                            if val and val != '':
                                constituencies.append({'value': val, 'label': text})
                        print(f'        Found {len(constituencies)} constituencies')
                    except Exception as e:
                        print(f'        Error getting constituencies: {e}')
                        constituencies = []

                    # Get Languages - need to select a constituency first!
                    languages = []
                    if constituencies:
                        first_const = constituencies[0]
                        print(f'        Selecting constituency for language: {first_const["label"]}')
                        page.select_option('#constituency', value=first_const['value'])
                        page.wait_for_load_state('networkidle')
                        time.sleep(1)

                        try:
                            page.wait_for_selector('#langCd', state='visible', timeout=10000)
                            lang_select = page.locator('#langCd')
                            lang_options = lang_select.locator('option').all()
                            for opt in lang_options:
                                val = opt.get_attribute('value')
                                text = opt.inner_text().strip()
                                if val and val != '':
                                    languages.append({'value': val, 'label': text})
                            print(f'        Found {len(languages)} languages: {[l["label"] for l in languages]}')
                        except Exception as e:
                            print(f'        Error getting languages: {e}')
                            languages = [{'value': 'ENG', 'label': 'ENGLISH'}, {'value': 'HIN', 'label': 'HINDI'}]
                    else:
                        languages = [{'value': 'ENG', 'label': 'ENGLISH'}, {'value': 'HIN', 'label': 'HINDI'}]

                    roll_data['districts'][district['label']] = {
                        'constituencies': constituencies,
                        'languages': languages
                    }

                year_data['roll_types'][roll_type['label']] = roll_data

            state_data['years'][year['label']] = year_data

        all_data[state['label']] = state_data

    browser.close()

    with open('discovered_structure.json', 'w', encoding='utf-8') as f:
        json.dump(all_data, f, indent=2, ensure_ascii=False)

    print('[+] Discovery complete')
    print(json.dumps(all_data, indent=2))
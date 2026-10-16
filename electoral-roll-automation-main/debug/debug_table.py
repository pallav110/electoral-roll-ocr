from playwright.sync_api import sync_playwright
import time

BASE_URL = 'https://voters.eci.gov.in/download-eroll'

with sync_playwright() as p:
    browser = p.chromium.launch(headless=False)
    context = browser.new_context()
    page = context.new_page()
    page.goto(BASE_URL)
    page.wait_for_load_state('networkidle')
    time.sleep(2)

    # Get states
    state_select = page.locator('#stateCode')
    state_options = state_select.locator('option').all()
    states = []
    for opt in state_options:
        val = opt.get_attribute('value')
        text = opt.inner_text().strip()
        if val and val != '':
            states.append({'value': val, 'label': text})

    # Try Delhi (U05)
    state = next(s for s in states if s['value'] == 'U05')
    page.select_option('#stateCode', value=state['value'])
    page.wait_for_load_state('networkidle')
    time.sleep(2)

    # Get years
    year_select = page.locator('#revyear')
    year_options = year_select.locator('option').all()
    years = []
    for opt in year_options:
        val = opt.get_attribute('value')
        text = opt.inner_text().strip()
        if val and val != '':
            years.append({'value': val, 'label': text})

    # Try year 2025
    for y in years:
        if '2025' in y['label']:
            page.select_option('#revyear', value=y['value'])
            page.wait_for_load_state('networkidle')
            time.sleep(3)

            # Get roll types
            page.wait_for_selector('#roleType', state='visible', timeout=10000)
            roll_select = page.locator('#roleType')
            roll_options = roll_select.locator('option').all()
            roll_types = []
            for opt in roll_options:
                val = opt.get_attribute('value')
                text = opt.inner_text().strip()
                if val and val != '':
                    roll_types.append({'value': val, 'label': text})

            # Try first roll type
            for rt in roll_types[:1]:
                page.select_option('#roleType', value=rt['value'])
                page.wait_for_load_state('networkidle')
                time.sleep(3)

                # Get districts
                page.wait_for_selector('#district', state='visible', timeout=10000)
                dist_select = page.locator('#district')
                dist_options = dist_select.locator('option').all()
                districts = []
                for opt in dist_options:
                    val = opt.get_attribute('value')
                    text = opt.inner_text().strip()
                    if val and val != '':
                        districts.append({'value': val, 'label': text})

                # Try first district
                for d in districts[:1]:
                    page.select_option('#district', value=d['value'])
                    page.wait_for_load_state('networkidle')
                    time.sleep(3)

                    # Get constituencies
                    const_select = page.locator('#constituency')
                    const_options = const_select.locator('option').all()
                    constituencies = []
                    for opt in const_options:
                        val = opt.get_attribute('value')
                        text = opt.inner_text().strip()
                        if val and val != '':
                            constituencies.append({'value': val, 'label': text})

                    # Try first constituency
                    for c in constituencies[:1]:
                        page.select_option('#constituency', value=c['value'])
                        page.wait_for_load_state('networkidle')
                        time.sleep(3)

                        # Get languages
                        lang_select = page.locator('#langCd')
                        lang_options = lang_select.locator('option').all()
                        languages = []
                        for opt in lang_options:
                            val = opt.get_attribute('value')
                            text = opt.inner_text().strip()
                            if val and val != '':
                                languages.append({'value': val, 'label': text})

                        # Select first language
                        for l in languages[:1]:
                            page.select_option('#langCd', value=l['value'])
                            page.wait_for_load_state('networkidle')
                            time.sleep(3)

                            # Now check table in detail
                            print('Checking table in detail...')
                            table = page.locator('table.datatable.contenttable-eroll')
                            if table.count() > 0:
                                # Check header
                                headers = table.locator('thead th').all()
                                header_texts = [h.inner_text().strip() for h in headers]
                                print(f'Headers: {header_texts}')

                                # Check rows
                                rows = table.locator('tbody tr').all()
                                print(f'Total rows: {len(rows)}')
                                for i, row in enumerate(rows):
                                    cells = row.locator('td').all()
                                    cell_texts = [cell.inner_text().strip() for cell in cells]
                                    print(f'  Row {i}: {cell_texts}')
                                    # Check for checkbox
                                    checkbox = row.locator('td input[type="checkbox"]').first
                                    if checkbox.count() > 0:
                                        print(f'    Checkbox found: value={checkbox.get_attribute("value")}')

                            # Check pagination
                            print('\nChecking pagination...')
                            pag = page.locator('.pagination-outer')
                            if pag.count() > 0:
                                print('Pagination found!')
                                btns = page.locator('.control-btn').all()
                                for btn in btns:
                                    print(f'  Button: text="{btn.inner_text().strip()}" enabled={btn.is_enabled()}')
                                btns2 = page.locator('.control-btn2').all()
                                for btn in btns2:
                                    print(f'  Button2: text="{btn.inner_text().strip()}" enabled={btn.is_enabled()}')
                            else:
                                print('No pagination found')
                            break
                        break
                    break
                break
            break

    browser.close()
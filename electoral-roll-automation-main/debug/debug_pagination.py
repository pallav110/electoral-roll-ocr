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

                            # Now test pagination - go to last page
                            print('Testing pagination - going to last page...')
                            last_btn = page.locator('.control-btn:has-text(">>")').first
                            if last_btn.count() > 0 and last_btn.is_enabled():
                                last_btn.click()
                                page.wait_for_load_state('networkidle')
                                time.sleep(3)

                                # Check page number
                                page_indicator = page.locator('.control-btn2 strong').first
                                if page_indicator.count() > 0:
                                    print(f'Current page: {page_indicator.inner_text().strip()}')

                                # Check rows on last page
                                rows = page.locator('table.datatable.contenttable-eroll tbody tr').all()
                                print(f'Rows on last page: {len(rows)}')
                                for i, row in enumerate(rows):
                                    cells = row.locator('td').all()
                                    cell_texts = [cell.inner_text().strip() for cell in cells]
                                    print(f'  Row {i}: {cell_texts}')

                                # Now go back to previous pages
                                while True:
                                    prev_btn = page.locator('.control-btn:has-text("<")').first
                                    # Need to find the one with just "<"
                                    btns = page.locator('.control-btn').all()
                                    prev_found = False
                                    for btn in btns:
                                        text = btn.inner_text().strip()
                                        if text == "<" and btn.is_enabled():
                                            prev_found = True
                                            print('Going to previous page...')
                                            btn.click()
                                            page.wait_for_load_state('networkidle')
                                            time.sleep(2)

                                            page_indicator = page.locator('.control-btn2 strong').first
                                            if page_indicator.count() > 0:
                                                print(f'Current page: {page_indicator.inner_text().strip()}')

                                            rows = page.locator('table.datatable.contenttable-eroll tbody tr').all()
                                            print(f'Rows: {len(rows)}')
                                            for i, row in enumerate(rows):
                                                cells = row.locator('td').all()
                                                cell_texts = [cell.inner_text().strip() for cell in cells]
                                                print(f'  Row {i}: {cell_texts}')
                                            break

                                    if not prev_found:
                                        print('No more previous pages')
                                        break
                            break
                        break
                    break
                break
            break

    browser.close()
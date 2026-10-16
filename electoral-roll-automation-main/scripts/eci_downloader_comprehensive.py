import os
import time
import zipfile
import base64
import json
import requests
from pathlib import Path
from playwright.sync_api import sync_playwright

OUTPUT_FOLDER = "downloaded_pdfs"
BASE_URL = "https://voters.eci.gov.in/download-eroll"
CAPTCHA_PATH = "captcha.png"

# Vision API configuration (using the model mentioned by user)
VISION_API_URL = "https://api.nvidia.com/v1/chat/completions"  # Placeholder - update with actual endpoint
VISION_MODEL = "nvidia/meta/llama-3.2-11b-vision-instruct"

class ECIDownloader:
    def __init__(self, vision_api_key=None):
        if not os.path.exists(OUTPUT_FOLDER):
            os.makedirs(OUTPUT_FOLDER)
        self.vision_api_key = vision_api_key
        self.downloaded_files = []  # Track downloaded files for zipping

    def solve_captcha_with_vision(self, image_path):
        """
        Use vision model to solve captcha.
        If no API key provided, falls back to manual input.
        """
        print(f"[*] Analyzing {image_path} with vision model...")

        if not self.vision_api_key:
            print("[!] No vision API key provided, falling back to manual input")
            # In headless mode, we can't use input(), so we'll return a placeholder
            return "MANUAL"

        try:
            with open(image_path, "rb") as f:
                image_data = base64.b64encode(f.read()).decode('utf-8')

            headers = {
                "Authorization": f"Bearer {self.vision_api_key}",
                "Content-Type": "application/json"
            }

            payload = {
                "model": VISION_MODEL,
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "text",
                                "text": "Read the captcha code from this image. Return ONLY the alphanumeric code, nothing else."
                            },
                            {
                                "type": "image_url",
                                "image_url": {"url": f"data:image/png;base64,{image_data}"}
                            }
                        ]
                    }
                ],
                "max_tokens": 50,
                "temperature": 0
            }

            response = requests.post(VISION_API_URL, headers=headers, json=payload, timeout=30)
            if response.status_code == 200:
                result = response.json()
                captcha_code = result['choices'][0]['message']['content'].strip()
                print(f"[*] Vision model returned: {captcha_code}")
                return captcha_code
            else:
                print(f"[!] Vision API error: {response.status_code} - {response.text}")
        except Exception as e:
            print(f"[!] Vision model error: {e}")

        return "MANUAL"

    def discover_all_options(self):
        """Discover all states, years, roll types, districts, constituencies, languages"""
        print("[*] Discovering all dropdown options...")

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context()
            page = context.new_page()
            page.goto(BASE_URL)
            page.wait_for_load_state("networkidle")

            # Get all states
            state_select = page.locator("#stateCode")
            state_options = state_select.locator("option").all()
            states = []
            for opt in state_options:
                val = opt.get_attribute("value")
                text = opt.inner_text().strip()
                if val and val != "":
                    states.append({"value": val, "label": text})

            all_data = {}

            for state in states:
                print(f"  Discovering: {state['label']} ({state['value']})")
                page.select_option("#stateCode", value=state['value'])
                page.wait_for_load_state("networkidle")
                time.sleep(1)

                # Get years
                year_select = page.locator("#revyear")
                year_options = year_select.locator("option").all()
                years = []
                for opt in year_options:
                    val = opt.get_attribute("value")
                    text = opt.inner_text().strip()
                    if val and val != "":
                        years.append({"value": val, "label": text})

                state_data = {"years": {}}

                for year in years:
                    page.select_option("#revyear", value=year['value'])
                    page.wait_for_load_state("networkidle")
                    time.sleep(1)

                    # Check for alerts
                    alerts = page.locator(".library-alert, .alert").all()
                    has_alert = False
                    for alert in alerts:
                        alert_text = alert.inner_text()
                        if "not available" in alert_text.lower() or "warning" in alert_text.lower():
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
                    except:
                        roll_types = []

                    year_data = {"roll_types": {}}

                    for roll_type in roll_types:
                        page.select_option("#roleType", label=roll_type['label'])
                        page.wait_for_load_state("networkidle")
                        time.sleep(0.5)

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
                        except:
                            districts = []

                        roll_data = {"districts": {}}

                        for district in districts:
                            page.select_option("#district", label=district['label'])
                            page.wait_for_load_state("networkidle")
                            time.sleep(0.5)

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
                            except:
                                constituencies = []

                            # Get Languages (usually just ENGLISH and HINDI)
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
                            except:
                                languages = [{"value": "ENG", "label": "ENGLISH"}, {"value": "HIN", "label": "HINDI"}]

                            roll_data["districts"][district['label']] = {
                                "constituencies": constituencies,
                                "languages": languages
                            }

                        year_data["roll_types"][roll_type['label']] = roll_data

                    state_data["years"][year['label']] = year_data

                all_data[state['label']] = state_data

            browser.close()

            # Save discovered structure to JSON for reference
            with open("discovered_structure.json", "w", encoding="utf-8") as f:
                json.dump(all_data, f, indent=2, ensure_ascii=False)

            print("[+] Discovery complete. Structure saved to discovered_structure.json")
            return all_data

    def download_single(self, page, params, state_label, year_label, roll_label, dist_label, const_label, lang_label):
        """Download a single PDF with given parameters"""
        try:
            # Select State
            page.select_option("#stateCode", value=params['state'])
            page.wait_for_load_state("networkidle")
            time.sleep(1.5)

            # Select Year
            page.select_option("#revyear", value=params['year'])
            page.wait_for_load_state("networkidle")
            time.sleep(1)

            # Check for alert after year selection
            alerts = page.locator(".library-alert, .alert").all()
            for alert in alerts:
                alert_text = alert.inner_text()
                if "not available" in alert_text.lower() or "warning" in alert_text.lower():
                    return False, f"Alert: {alert_text}"

            # Select Roll Type
            page.wait_for_selector("#roleType", state="attached", timeout=10000)
            page.select_option("#roleType", label=params['rollType'])
            page.wait_for_load_state("networkidle")
            time.sleep(0.5)

            # Select District
            page.wait_for_selector("#district", state="attached", timeout=10000)
            page.select_option("#district", label=params['district'])
            page.wait_for_load_state("networkidle")
            time.sleep(0.5)

            # Select Constituency
            page.wait_for_selector("#constituency", state="attached", timeout=10000)
            page.select_option("#constituency", label=params['constituency'])
            time.sleep(0.5)

            # Select Language
            page.wait_for_selector("#langCd", state="attached", timeout=10000)
            page.select_option("#langCd", label=params['language'])
            time.sleep(0.5)

            # Handle Captcha
            captcha_img = page.locator("img[alt='captcha'], img[alt='Captcha'], img[id='captchaImg'], img.captcha").first
            if captcha_img.count() == 0:
                captcha_img = page.locator("#captcha").locator("..").locator("img").first

            src = captcha_img.get_attribute("src")
            if src and src.startswith("data:image/png;base64,"):
                b64_data = src.split(",")[1]
                with open(CAPTCHA_PATH, "wb") as f:
                    f.write(base64.b64decode(b64_data))
            else:
                captcha_img.screenshot(path=CAPTCHA_PATH)

            # Solve captcha
            captcha_code = self.solve_captcha_with_vision(CAPTCHA_PATH)
            if captcha_code == "MANUAL":
                return False, "Captcha requires manual solving"

            # Submit and Download
            with page.expect_download(timeout=30000) as download_info:
                page.fill("#captcha", captcha_code)
                page.click("button.btn.btn-block.submit")

            download = download_info.value
            filename = download.suggested_filename()

            # Create organized folder structure: state/year/roll_type/district/constituency/language/
            safe_state = state_label.replace(" ", "_").replace("&", "and").replace("/", "_")
            safe_year = year_label.replace(" ", "_")
            safe_roll = roll_label.replace(" ", "_").replace("-", "_").replace("/", "_")
            safe_dist = dist_label.replace(" ", "_").replace("/", "_")
            safe_const = const_label.replace(" ", "_").replace("-", "_").replace("/", "_")
            safe_lang = lang_label.replace(" ", "_")

            folder_path = Path(OUTPUT_FOLDER) / safe_state / safe_year / safe_roll / safe_dist / safe_const / safe_lang
            folder_path.mkdir(parents=True, exist_ok=True)

            save_path = folder_path / filename
            download.save_as(str(save_path))

            self.downloaded_files.append({
                "path": str(save_path),
                "relative": str(save_path.relative_to(OUTPUT_FOLDER)),
                "state": state_label,
                "year": year_label,
                "roll_type": roll_label,
                "district": dist_label,
                "constituency": const_label,
                "language": lang_label
            })

            print(f"[+] Downloaded: {save_path}")
            return True, str(save_path)

        except Exception as e:
            return False, str(e)

    def download_all(self, structure, max_per_category=None):
        """Download all PDFs based on discovered structure"""
        print("[*] Starting comprehensive download...")

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=False)
            context = browser.new_context()
            page = context.new_page()
            page.goto(BASE_URL)
            page.wait_for_load_state("networkidle")

            total = 0
            successful = 0
            failed = 0

            for state_label, state_data in structure.items():
                state_value = None
                # Find state value
                with sync_playwright() as p2:
                    browser2 = p2.chromium.launch(headless=True)
                    page2 = browser2.new_page()
                    page2.goto(BASE_URL)
                    page2.wait_for_load_state("networkidle")
                    opts = page2.locator("#stateCode option").all()
                    for opt in opts:
                        if opt.inner_text().strip() == state_label:
                            state_value = opt.get_attribute("value")
                            break
                    browser2.close()

                if not state_value:
                    print(f"[!] Could not find value for state: {state_label}")
                    continue

                print(f"\n{'='*60}")
                print(f"Processing STATE: {state_label}")
                print(f"{'='*60}")

                for year_label, year_data in state_data.get("years", {}).items():
                    print(f"\n  YEAR: {year_label}")

                    for roll_label, roll_data in year_data.get("roll_types", {}).items():
                        print(f"    ROLL TYPE: {roll_label}")

                        dist_count = 0
                        for dist_label, dist_data in roll_data.get("districts", {}).items():
                            if max_per_category and dist_count >= max_per_category:
                                break
                            dist_count += 1

                            const_count = 0
                            for const in dist_data.get("constituencies", []):
                                if max_per_category and const_count >= max_per_category:
                                    break
                                const_count += 1

                                for lang in dist_data.get("languages", []):
                                    total += 1
                                    params = {
                                        "state": state_value,
                                        "year": year_label,
                                        "rollType": roll_label,
                                        "district": dist_label,
                                        "constituency": const["label"],
                                        "language": lang["label"]
                                    }

                                    print(f"      [{total}] {dist_label} | {const['label']} | {lang['label']}")
                                    success, result = self.download_single(
                                        page, params, state_label, year_label, roll_label,
                                        dist_label, const["label"], lang["label"]
                                    )

                                    if success:
                                        successful += 1
                                    else:
                                        failed += 1
                                        print(f"        [!] Failed: {result}")

                                    # Small delay between requests
                                    time.sleep(2)

            browser.close()

            print(f"\n{'='*60}")
            print(f"DOWNLOAD COMPLETE")
            print(f"Total attempted: {total}")
            print(f"Successful: {successful}")
            print(f"Failed: {failed}")
            print(f"{'='*60}")

            self.create_organized_zips()

    def create_organized_zips(self):
        """Create organized ZIP files based on folder structure"""
        print("[*] Creating organized ZIP archives...")

        # Create ZIP per state
        state_folders = [f for f in os.listdir(OUTPUT_FOLDER) if os.path.isdir(os.path.join(OUTPUT_FOLDER, f))]

        for state_folder in state_folders:
            state_path = os.path.join(OUTPUT_FOLDER, state_folder)
            zip_name = f"electoral_rolls_{state_folder}.zip"

            print(f"  Creating {zip_name}...")
            with zipfile.ZipFile(zip_name, 'w', zipfile.ZIP_DEFLATED) as zipf:
                for root, dirs, files in os.walk(state_path):
                    for file in files:
                        if file.endswith(".pdf"):
                            full_path = os.path.join(root, file)
                            # Keep relative path from OUTPUT_FOLDER
                            arcname = os.path.relpath(full_path, OUTPUT_FOLDER)
                            zipf.write(full_path, arcname)

            print(f"  [+] Created {zip_name}")

        # Also create a master ZIP with everything
        master_zip = "electoral_rolls_ALL.zip"
        print(f"  Creating master archive {master_zip}...")
        with zipfile.ZipFile(master_zip, 'w', zipfile.ZIP_DEFLATED) as zipf:
            for root, dirs, files in os.walk(OUTPUT_FOLDER):
                for file in files:
                    if file.endswith(".pdf"):
                        full_path = os.path.join(root, file)
                        arcname = os.path.relpath(full_path, OUTPUT_FOLDER)
                        zipf.write(full_path, arcname)

        print(f"[+] All ZIP archives created!")

        # Save download log
        with open("download_log.json", "w", encoding="utf-8") as f:
            json.dump(self.downloaded_files, f, indent=2, ensure_ascii=False)
        print("[+] Download log saved to download_log.json")

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="ECI Electoral Roll Downloader")
    parser.add_argument("--discover", action="store_true", help="Only discover structure, don't download")
    parser.add_argument("--download", action="store_true", help="Download all (requires discovered_structure.json)")
    parser.add_argument("--max-per-category", type=int, default=None, help="Limit items per category for testing")
    parser.add_argument("--vision-key", type=str, default=None, help="Vision API key for captcha solving")
    parser.add_argument("--state", type=str, default=None, help="Specific state to download (e.g., 'NCT OF Delhi')")
    parser.add_argument("--year", type=str, default=None, help="Specific year to download (e.g., '2025')")

    args = parser.parse_args()

    downloader = ECIDownloader(vision_api_key=args.vision_key)

    if args.discover:
        structure = downloader.discover_all_options()
    elif args.download:
        # Load discovered structure
        if os.path.exists("discovered_structure.json"):
            with open("discovered_structure.json", "r", encoding="utf-8") as f:
                structure = json.load(f)
        else:
            print("[*] No discovered structure found, discovering first...")
            structure = downloader.discover_all_options()

        # Filter by state/year if specified
        if args.state:
            structure = {k: v for k, v in structure.items() if args.state.lower() in k.lower()}
        if args.year:
            for state in structure:
                structure[state]["years"] = {k: v for k, v in structure[state]["years"].items() if args.year in k}

        downloader.download_all(structure, max_per_category=args.max_per_category)
    else:
        # Default: discover then download with limit for testing
        structure = downloader.discover_all_options()
        downloader.download_all(structure, max_per_category=2)  # Test with 2 per category
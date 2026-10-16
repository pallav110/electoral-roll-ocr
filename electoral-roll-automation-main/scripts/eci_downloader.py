import os
import time
import zipfile
import base64
from playwright.sync_api import sync_playwright

OUTPUT_FOLDER = "downloaded_pdfs"
BASE_URL = "https://voters.eci.gov.in/download-eroll"
CAPTCHA_PATH = "captcha.png"

class ECIDownloader:
    def __init__(self):
        if not os.path.exists(OUTPUT_FOLDER):
            os.makedirs(OUTPUT_FOLDER)

    def solve_captcha_with_vision(self):
        """
        Placeholder for Vision Model integration.
        The captcha is a base64 data URL image.
        """
        print(f"[*] Analyzing {CAPTCHA_PATH}...")
        # INTEGRATION POINT: CALL VISION API HERE
        captcha_code = input("Enter the captcha code shown in captcha.png: ")
        return captcha_code

    def download(self, params):
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=False)
            context = browser.new_context()
            page = context.new_page()

            print(f"[*] Navigating to {BASE_URL}...")
            page.goto(BASE_URL)
            page.wait_for_load_state("networkidle")

            # 1. Select State
            print(f"[*] Selecting State: {params['state']}...")
            page.select_option("#stateCode", value=params['state'])
            page.wait_for_load_state("networkidle")
            time.sleep(2)

            # 2. Select Year
            print(f"[*] Selecting Year: {params['year']}...")
            page.select_option("#revyear", value=params['year'])
            page.wait_for_load_state("networkidle")
            time.sleep(1)

            # 3. Select Roll Type
            print(f"[*] Selecting Roll Type: {params['rollType']}...")
            page.wait_for_selector("#roleType", state="attached", timeout=10000)
            page.select_option("#roleType", label=params['rollType'])
            page.wait_for_load_state("networkidle")
            time.sleep(1)

            # 4. Select District
            print(f"[*] Selecting District: {params['district']}...")
            page.wait_for_selector("#district", state="attached", timeout=10000)
            page.select_option("#district", label=params['district'])
            page.wait_for_load_state("networkidle")
            time.sleep(1)

            # 5. Select Constituency
            print(f"[*] Selecting Constituency: {params['constituency']}...")
            page.wait_for_selector("#constituency", state="attached", timeout=10000)
            page.select_option("#constituency", label=params['constituency'])
            time.sleep(1)

            # 6. Select Language
            print(f"[*] Selecting Language: {params['language']}...")
            page.wait_for_selector("#langCd", state="attached", timeout=10000)
            page.select_option("#langCd", label=params['language'])
            time.sleep(1)

            # 7. Handle Captcha - the image is a base64 data URL
            # Target the captcha image specifically (has alt="captcha" or class containing captcha)
            print("[*] Capturing captcha image...")
            captcha_img = page.locator("img[alt='captcha'], img[alt='Captcha'], img[id='captchaImg'], img.captcha").first
            # If not found by those, try to find the one near the captcha input field
            if captcha_img.count() == 0:
                captcha_img = page.locator("#captcha").locator("..").locator("img").first

            src = captcha_img.get_attribute("src")
            if src and src.startswith("data:image/png;base64,"):
                b64_data = src.split(",")[1]
                with open(CAPTCHA_PATH, "wb") as f:
                    f.write(base64.b64decode(b64_data))
                print(f"[*] Captcha saved to {CAPTCHA_PATH}")
            else:
                # Fallback: screenshot the captcha area
                captcha_img.screenshot(path=CAPTCHA_PATH)

            captcha_code = self.solve_captcha_with_vision()

            # 8. Submit and Download
            print("[*] Submitting and downloading...")
            try:
                with page.expect_download() as download_info:
                    page.fill("#captcha", captcha_code)
                    page.click("button.btn.btn-block.submit")

                download = download_info.value
                filename = download.suggested_filename()
                save_path = os.path.join(OUTPUT_FOLDER, filename)
                download.save_as(save_path)
                print(f"[+] Successfully downloaded: {filename}")
            except Exception as e:
                print(f"[!] Error during download: {e}")

            browser.close()
            self.zip_results()

    def zip_results(self):
        zip_name = "electoral_rolls.zip"
        print(f"[*] Creating archive {zip_name}...")
        with zipfile.ZipFile(zip_name, 'w', zipfile.ZIP_DEFLATED) as zipf:
            for file in os.listdir(OUTPUT_FOLDER):
                if file.endswith(".pdf"):
                    zipf.write(os.path.join(OUTPUT_FOLDER, file), file)
        print(f"[+] All downloaded PDFs zipped into {zip_name}")

if __name__ == "__main__":
    # Delhi = U05, Year = 2025 (2024 shows warning "Publish Eroll not available for this Year")
    # Roll Type options for 2025: 'Supplement-5 2025', 'State General Election Roll 2025', 'DraftRoll - 2025', 'Final Roll - 2025'
    # District options: 'Central', 'Central North', 'East', 'New Delhi', 'North', 'North East', 'North West', 'Old Delhi', 'Outer North', 'South', 'South East', 'South West', 'West'
    # Constituency options: '1 - NERELA' through '70 - KARAWAL NAGAR'
    # Language options: 'ENGLISH' (value=ENG), 'HINDI' (value=HIN)
    target_params = {
        "state": "U05",  # NCT OF Delhi
        "year": "2025",
        "rollType": "Final Roll - 2025",  # Exact label from dropdown
        "district": "New Delhi",  # Exact label from dropdown (note: 'New Delhi' not 'NEW DELHI')
        "constituency": "40 - NEW DELHI",  # Exact label from dropdown
        "language": "ENGLISH"  # Exact label from dropdown
    }

    downloader = ECIDownloader()
    downloader.download(target_params)
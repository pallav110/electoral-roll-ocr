import re
import pandas as pd

txt_path = "OCR/input/copied_pdf.txt"
output_excel = "OCR/results/output/expected_ids.xlsx"

pattern = r'\b[A-Z]{3}\d{7}\b'

with open(txt_path, "r", encoding="utf-8") as f:
    text = f.read()

ids = re.findall(pattern, text)

print(f"Total IDs found: {len(ids)}")

df = pd.DataFrame({
    "Expected_ID": ids
})

df.to_excel(output_excel, index=False)

print(f"Saved to: {output_excel}")
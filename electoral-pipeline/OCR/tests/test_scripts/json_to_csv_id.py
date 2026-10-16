import json
import pandas as pd

excel_path = "OCR/results/output/expected_ids.xlsx"
json_path = "OCR/results/whole_pdf_ocr_results.json"

# -------------------------
# 1. Read existing Excel
# -------------------------
df = pd.read_excel(excel_path)

# -------------------------
# 2. Read JSON
# -------------------------
with open(json_path, "r", encoding="utf-8") as f:
    data = json.load(f)

# -------------------------
# 3. Extract actual IDs
# -------------------------
actual_ids = [
    record.get("id_card_no", "")
    for record in data["records"]
]

# -------------------------
# 4. Put them into new column
# -------------------------
df["actual_ids"] = actual_ids

# -------------------------
# 5. Compare the two columns
# -------------------------
def compare_ids(row, actual_ids, index, window=5):

    expected = row["Expected_ID"]
    actual = row["actual_ids"]

    # Exact match
    if expected == actual:
        return "MATCH"

    # Look nearby for the expected ID
    start = max(0, index - window)
    end = min(len(actual_ids), index + window + 1)

    nearby = actual_ids[start:end]

    if expected in nearby:
        return "ORDER_SHIFT"

    return "TRUE_MISMATCH"


df["Status"] = [
    compare_ids(row, actual_ids, i)
    for i, (_, row) in enumerate(df.iterrows())
]

# -------------------------
# 6. Save same Excel file
# -------------------------
df.to_excel(excel_path, index=False)

print(f"Expected IDs : {len(df)}")
print(f"Actual IDs   : {len(actual_ids)}")
print(f"Matches      : {(df['Status'] == 'MATCH').sum()}")
print(f"Order shifts : {(df['Status'] == 'ORDER_SHIFT').sum()}")
print(f"True mismatches: {(df['Status'] == 'TRUE_MISMATCH').sum()}")

# -------------------------
# 7. Print only TRUE mismatches
# -------------------------
mismatches = df[df["Status"] == "TRUE_MISMATCH"]

print("\n========== TRUE MISMATCHES ==========")

if mismatches.empty:
    print("No true mismatches found!")
else:
    for _, row in mismatches.iterrows():
        print(
            f"Expected: {row['Expected_ID']} | "
            f"Actual: {row['actual_ids']}"
        )

# -------------------------
# 8. Verify saved Excel
# -------------------------
check = pd.read_excel(excel_path)

print("\nColumns in saved Excel:")
print(check.columns.tolist())

print(f"\nSaved to: {excel_path}")
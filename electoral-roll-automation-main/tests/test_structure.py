"""
Test script to verify the downloader works with the updated Delhi structure.
"""

import json
import sys
import os

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.eci_downloader import ECIDownloader

# Test loading discovered structure
with open("discovered_structure.json", "r", encoding="utf-8") as f:
    structure = json.load(f)

print("=== Testing Discovered Structure ===")

# Check Delhi structure
if "NCT OF Delhi" in structure:
    delhi = structure["NCT OF Delhi"]
    print(f"\nNCT OF Delhi found!")
    print(f"Years: {list(delhi.get('years', {}).keys())}")

    for year_label, year_data in delhi.get("years", {}).items():
        print(f"\n  Year: {year_label}")
        for roll_label, roll_data in year_data.get("roll_types", {}).items():
            print(f"    Roll Type: {roll_label}")
            districts = list(roll_data.get("districts", {}).keys())
            print(f"      Districts ({len(districts)}): {districts[:5]}{'...' if len(districts) > 5 else ''}")

            # Check first district
            if districts:
                first_dist = districts[0]
                dist_data = roll_data["districts"][first_dist]
                constituencies = dist_data.get("constituencies", [])
                languages = dist_data.get("languages", [])
                print(f"      First district ({first_dist}): {len(constituencies)} constituencies, {len(languages)} languages")
                if constituencies:
                    print(f"        First constituency: {constituencies[0]['label']}")
                if languages:
                    print(f"        Languages: {[l['label'] for l in languages]}")

# Count total combinations
total_combinations = 0
for state_label, state_data in structure.items():
    for year_label, year_data in state_data.get("years", {}).items():
        for roll_label, roll_data in year_data.get("roll_types", {}).items():
            for dist_label, dist_data in roll_data.get("districts", {}).items():
                for const in dist_data.get("constituencies", []):
                    for lang in dist_data.get("languages", []):
                        total_combinations += 1

print(f"\n=== Total download combinations across all states: {total_combinations} ===")

# Test API import
try:
    from src.api import app
    print("\n=== API Module Import: SUCCESS ===")
except Exception as e:
    print(f"\n=== API Module Import: FAILED - {e} ===")

# Test MinIO import
try:
    from src.minio_client import MinIOClient
    print("=== MinIO Module Import: SUCCESS ===")
except Exception as e:
    print(f"=== MinIO Module Import: FAILED - {e} ===")

print("\n=== All Tests Complete ===")
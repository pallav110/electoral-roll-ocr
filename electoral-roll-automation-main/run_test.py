#!/usr/bin/env python3
import subprocess
import sys

result = subprocess.run([sys.executable, "scripts/real_download.py", "--count", "1", "--no-upload"],
                       capture_output=True, text=True, timeout=300)
print("STDOUT:")
print(result.stdout)
print("\nSTDERR:")
print(result.stderr)
print(f"\nReturn code: {result.returncode}")
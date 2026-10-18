import os, re

files = os.listdir("/data/pdfs/lansdown-corpus")
# Normalize "(1).pdf" -> ".pdf" to check for duplicates
normalized = [re.sub(r" \(\d+\)\.pdf$", ".pdf", f) for f in files]
unique = set(normalized)
print("total files:", len(files))
print("unique (normalized):", len(unique))
dupes = len(files) - len(unique)
print("duplicates:", dupes)

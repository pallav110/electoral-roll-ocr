import fitz, glob, sys

paths = sorted(glob.glob("/data/pdfs/lansdown-corpus/*.pdf"))
total_pages = 0
sizes = []
for p in paths[:20]:
    doc = fitz.open(p)
    n = doc.page_count
    total_pages += n
    sizes.append((p.split("/")[-1], n))
    print("%s -> %d pages" % (p.split("/")[-1], n))
print("---")
print("avg pages (first 20): %.1f" % (total_pages / 20))
print("files found:", len(paths))

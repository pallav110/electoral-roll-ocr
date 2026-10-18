import fitz, time, sys

path = sys.argv[1] if len(sys.argv) > 1 else "data/pdfs/lansdown.pdf"
doc = fitz.open(path)
print("pages:", doc.page_count)
t0 = time.time()
pix = doc[0].get_pixmap(dpi=200)
t1 = time.time()
print("PAGE0 RENDER: %.2fs size=%dx%d" % (t1 - t0, pix.width, pix.height))
for i in range(0, doc.page_count, 3):
    page = doc[i]
    t0 = time.time()
    pix = page.get_pixmap(dpi=200)
    t1 = time.time()
    print("page %d RENDER: %.2fs" % (i + 1, t1 - t0))

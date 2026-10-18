import sys, time
import fitz
sys.path.insert(0, "/code")
from app.ocr_adapter import ocr_page_words

path = sys.argv[1]
doc = fitz.open(path)
print("pages:", doc.page_count)
for i in range(0, doc.page_count, 5):
    page = doc[i]
    t0 = time.time()
    words = ocr_page_words(page)
    t1 = time.time()
    print("page %d ocr_page_words: %.2fs (%d words)" % (i + 1, t1 - t0, len(words)))

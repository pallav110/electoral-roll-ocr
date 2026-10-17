import sys
sys.stdout = open("/data/clipfix2_out.txt", "w", encoding="utf-8", errors="replace")
sys.path.insert(0, "/app")

import fitz, numpy as np, cv2, re
from PIL import Image
import ocr_pdf_api as A

d = fitz.open("/data/roll107.pdf"); page = d[0]
clip = A._cover_header_clip(page)
img = A.np.frombuffer(page.get_pixmap(dpi=A._STATE_TOKEN_DPI, clip=clip, alpha=False)
                      .tobytes("png"), A.np.uint8)
im = cv2.imdecode(img, cv2.IMREAD_COLOR)
h, w = im.shape[:2]
print(f"_STATE_TOKEN_DPI = {A._STATE_TOKEN_DPI}")
print(f"clip rendered: {w} x {h} px")

# EXACT pipeline steps
heading = im[int(h*0.50):int(h*0.92), int(w*0.48):int(w*0.60)]
print(f"heading before resize: {heading.shape}")
heading = cv2.resize(heading, None, fx=4, fy=4, interpolation=cv2.INTER_CUBIC)
print(f"heading after 4x resize: {heading.shape}  <-- {heading.shape[0]*heading.shape[1]*4/1e6:.1f} MP")
gray = cv2.cvtColor(heading, cv2.COLOR_BGR2GRAY)
cv2.imwrite("/data/hdr_upscaled.png", gray)

print("\n--- psm 7 (pipeline default) ---")
t = A._tess_string(Image.fromarray(gray), lang="eng", config="--oem 3 --psm 7")
print(repr(t))

print("\n--- psm modes on the same crop ---")
for psm in (6, 7, 8, 11, 13):
    t = A._tess_string(Image.fromarray(gray), lang="eng", config=f"--oem 3 --psm {psm}")
    print(f"  psm {psm:>2}: {t!r}   match={bool(re.search(r'([S$U])\\s*(\\d{2})', t.upper()))}")

print("\n--- WITHOUT the 4x upscale ---")
sm = cv2.cvtColor(im[int(h*0.50):int(h*0.92), int(w*0.48):int(w*0.60)], cv2.COLOR_BGR2GRAY)
print("  no-resize size:", sm.shape)
for psm in (6, 7, 11):
    t = A._tess_string(Image.fromarray(sm), lang="eng", config=f"--oem 3 --psm {psm}")
    print(f"  psm {psm:>2}: {t!r}")

print("\n--- what IS in the window: full-width header band ---")
band = im[int(h*0.50):int(h*0.92), :]
gb = cv2.cvtColor(band, cv2.COLOR_BGR2GRAY)
for psm in (6, 7, 11):
    t = A._tess_string(Image.fromarray(gb), lang="hin+eng", config=f"--oem 3 --psm {psm}")
    print(f"  full-width psm {psm}: {t[:110]!r}")
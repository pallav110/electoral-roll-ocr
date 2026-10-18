import sys, json
sys.stdout=open('/data/hin116_trace.txt','w',encoding='utf-8',errors='replace')
sys.path.insert(0,'/app')
import fitz
import ocr_pdf_api as A
p='/data/pdfs/2026-EROLLGEN-S28-40-SIR-FinalRoll-Revision1-HIN-116-WI.pdf'
d=fitz.open(p)
print('pages',len(d))
print('cover_entries',json.dumps(A._extract_anubhag_entries_from_cover(d[0]),ensure_ascii=False))
for i in [2,3,4,5,6,7,8,9,10,11,12,13,14,15,16,17,18,19,20]:
 if i>=len(d): continue
 page=d[i]
 loc=A._find_anubhag_header_band(page)
 print('page',i+1,'band',repr(loc[1]) if loc else None,'english_code',repr(A._anubhag_code_from_english_band(page,loc[0]) if loc else None),'fallback',repr(A._extract_anubhag_code_from_header_fallback(page)))

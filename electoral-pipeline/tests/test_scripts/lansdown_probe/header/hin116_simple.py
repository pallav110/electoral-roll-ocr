import sys, json, traceback
sys.path.insert(0,'/app')
try:
 import fitz
 import ocr_pdf_api as A
 p='/data/pdfs/2026-EROLLGEN-S28-40-SIR-FinalRoll-Revision1-HIN-116-WI.pdf'
 d=fitz.open(p)
 result={'pages':len(d),'cover':A._extract_anubhag_entries_from_cover(d[0])}
 for i in [2,3,4,10]:
  if i<len(d):
   x=A._find_anubhag_header_band(d[i])
   result['page'+str(i+1)]={'text':x[1] if x else None,'fallback':A._extract_anubhag_code_from_header_fallback(d[i])}
 open('/data/hin116_simple.json','w',encoding='utf-8').write(json.dumps(result,ensure_ascii=True,indent=2))
except Exception:
 open('/data/hin116_simple.err','w').write(traceback.format_exc())

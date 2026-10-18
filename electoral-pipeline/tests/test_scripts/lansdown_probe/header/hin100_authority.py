import sys, json
sys.stdout=open('/data/hin100_authority.txt','w',encoding='utf-8')
sys.path.insert(0,'/app')
import fitz
import ocr_pdf_api as A
path='/tmp/hin100.pdf'
d=fitz.open(path)
cover=A._extract_anubhag_entries_from_cover(d[0])
meta=A._extract_roll_header_metadata(d[2])
print(json.dumps({'cover_entries':cover,'metadata':{k:meta.get(k) for k in ('ac_code','booth_code','anubhag_code','anubhag_name','anubhag_review_reason')}},ensure_ascii=False,indent=2))

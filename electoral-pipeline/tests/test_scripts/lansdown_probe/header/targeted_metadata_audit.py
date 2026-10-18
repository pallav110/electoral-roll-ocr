import sys, json, os, traceback
sys.path.insert(0, '/app')
import fitz
import ocr_pdf_api as A

names = [
 '2026-EROLLGEN-S28-40-SIR-FinalRoll-Revision1-HIN-102-WI.pdf',
 '2026-EROLLGEN-S28-40-SIR-FinalRoll-Revision1-HIN-104-WI.pdf',
 '2026-EROLLGEN-S28-40-SIR-FinalRoll-Revision1-HIN-112-WI.pdf',
 '2026-EROLLGEN-S28-40-SIR-FinalRoll-Revision1-HIN-117-WI.pdf',
 '2026-EROLLGEN-S28-40-SIR-FinalRoll-Revision1-HIN-118-WI.pdf',
 '2026-EROLLGEN-S28-40-SIR-FinalRoll-Revision1-HIN-124-WI.pdf',
 '2026-EROLLGEN-S28-40-SIR-FinalRoll-Revision1-HIN-126-WI.pdf',
]
path='/data/audit_targeted.jsonl'
with open(path,'w',encoding='utf-8') as out:
 for name in names:
  p='/data/pdfs/'+name
  row={'file':name}
  try:
   d=fitz.open(p)
   row['pages']=len(d)
   row['cover']=A._extract_anubhag_entries_from_cover(d[0])
   md=A._extract_roll_header_metadata(d[2] if len(d)>2 else d[0])
   row['metadata']={k:md.get(k) for k in ('ac_code','booth_code','anubhag_code','anubhag_name','anubhag_review_reason')}
   row['pages_sample']=[]
   for i in [2,3,min(10,len(d)-1),len(d)-1]:
    if i >= len(d): continue
    loc=A._find_anubhag_header_band(d[i])
    row['pages_sample'].append({'page':i+1,'header':loc[1] if loc else '', 'fallback':A._extract_anubhag_code_from_header_fallback(d[i])})
   d.close()
  except Exception as e:
   row['error']=type(e).__name__+': '+str(e)
  out.write(json.dumps(row,ensure_ascii=False)+'\n'); out.flush()
  print(name, json.dumps(row,ensure_ascii=False), flush=True)
print('DONE',flush=True)

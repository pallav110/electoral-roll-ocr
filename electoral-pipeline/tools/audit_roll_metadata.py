"""Audit stored anubhag/booth metadata against real PDF covers.

Outputs only roll metadata and filenames, not voter records or PII.
"""
import json, os, re, sys
from collections import defaultdict
sys.path.insert(0, '/app')
import fitz
import psycopg
import ocr_pdf_api as A

conn=psycopg.connect('host=db port=5432 dbname=electoral user=electoral password=electoral')
with conn.cursor() as cur:
    cur.execute("""
      SELECT d.id::text,d.file_name,d.source_path,
             er.anubhag_code,er.anubhag_name,er.booth_code,count(*)
      FROM documents d JOIN electoral_records er ON er.document_id=d.id
      GROUP BY d.id,d.file_name,d.source_path,er.anubhag_code,er.anubhag_name,er.booth_code
      ORDER BY d.file_name
    """)
    rows=cur.fetchall()
conn.close()
by=defaultdict(lambda: {'file_name':'','source_path':'','values':[]})
for did,name,path,code,aname,booth,n in rows:
    by[did]['file_name']=name; by[did]['source_path']=path
    by[did]['values'].append({'code':code,'name':aname,'booth':booth,'records':n})

out=[]
for did,item in by.items():
    vals=item['values']
    suspicious=any((v['code'] is not None and (int(v['code']) >= 10 or int(v['code']) <= 0)) for v in vals if str(v['code'] or '').isdigit())
    booth_values={str(v['booth']) for v in vals if v['booth']}
    m=re.search(r'HIN-(\d+)-WI',item['file_name'])
    expected=m.group(1).lstrip('0') or '0' if m else ''
    booth_suspicious=any(b.lstrip('0') != expected for b in booth_values if b.isdigit())
    if not suspicious and not booth_suspicious:
        continue
    path=item['source_path']
    if not os.path.exists(path):
        out.append({'file_name':item['file_name'],'stored':vals,'error':'source_missing'})
        continue
    try:
        doc=fitz.open(path)
        cover=A._extract_anubhag_entries_from_cover(doc[0])
        meta=A._extract_roll_header_metadata(doc[2] if len(doc)>2 else doc[0])
        out.append({'file_name':item['file_name'],'stored':vals,'expected_part':expected,
                    'cover_entries':cover,'page_meta':{k:meta.get(k) for k in ('ac_code','booth_code','anubhag_code','anubhag_name','anubhag_review_reason')}})
        doc.close()
    except Exception as exc:
        out.append({'file_name':item['file_name'],'stored':vals,'expected_part':expected,'error':type(exc).__name__+': '+str(exc)})

json.dump(out,open('/data/roll_metadata_audit.json','w',encoding='utf-8'),ensure_ascii=False,indent=2)
print('audited',len(out),'rolls')

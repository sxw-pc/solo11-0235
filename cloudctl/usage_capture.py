"""Import offline hypervisor usage pages."""
import hashlib,json

def import_page(db,page_id,rows):
    db.execute('CREATE TABLE IF NOT EXISTS meter_pages (id TEXT PRIMARY KEY,digest TEXT NOT NULL)')
    db.execute('CREATE TABLE IF NOT EXISTS meter_rows (page_id TEXT,line_no INTEGER,node TEXT,tenant TEXT,job TEXT,ticks INTEGER,PRIMARY KEY(page_id,line_no))')
    keys={'node','tenant','job','ticks'}
    if not isinstance(rows,list) or any(not isinstance(r,dict) or set(r)!=keys or not isinstance(r['ticks'],int) or r['ticks']<0 for r in rows):raise ValueError('invalid_meter_page')
    digest=hashlib.sha256(json.dumps(rows,sort_keys=True,separators=(',',':')).encode()).hexdigest()
    old=db.execute('SELECT digest FROM meter_pages WHERE id=?',(page_id,)).fetchone()
    if old:
        if old[0]!=digest:raise ValueError('meter_page_conflict')
        return {'status':'replayed','rows':len(rows)}
    with db:
        db.execute('INSERT INTO meter_pages VALUES (?,?)',(page_id,digest))
        for i,r in enumerate(rows,1):db.execute('INSERT INTO meter_rows VALUES (?,?,?,?,?,?)',(page_id,i,r['node'],r['tenant'],r['job'],r['ticks']))
    return {'status':'captured','rows':len(rows)}

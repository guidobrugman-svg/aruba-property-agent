"""Public diagnostics for partial coverage. No email secrets or production state."""
import sys,json,time
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from bs4 import BeautifulSoup
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import monitor,collectors
ROOT=Path('source-diagnostics');ROOT.mkdir(exist_ok=True)
names={'Ben Real Estate','Bluefin Realtors','Century 21 Aruba','Keller Williams Aruba','Berkshire Hathaway Aruba','HKG Real Estate Aruba','MPG Aruba','Home 4 Everyone'}
sources=[s for s in json.loads(Path('SOURCES.json').read_text())['sources'] if s['name'] in names]
real_get=monitor.get_page
def save_get(url,request_data=None):
    html,final=real_get(url,request_data)
    (ROOT/('page_'+str(abs(hash(url)))+'.html')).write_text(html)
    print('PAGE',json.dumps({'url':url,'file':'page_'+str(abs(hash(url)))+'.html','bytes':len(html)}),flush=True)
    return html,final
monitor.get_page=save_get
results=collectors.scan(sources,'deep',{},monitor)
for source,(items,observed,health) in results:
    print('RESULT',source['name'],json.dumps(health),flush=True)
    for obs in observed:
        if obs.get('needs_type_review'):print('UNCLASSIFIED',source['name'],json.dumps(obs),flush=True)
(ROOT/'report.json').write_text(json.dumps({s['name']:h for s,(_,_,h) in results},indent=2))
# Only public details of cards missing type; never historical/production records.
unknown=[(s,o) for s,(_,observed,_) in results for o in observed if o.get('needs_type_review')][:30]
def detail(pair):
    source,o=pair
    try:
        html,final=save_get(o['url'])
        soup=BeautifulSoup(html,'html.parser')
        print('DETAIL',source['name'],o['url'],json.dumps({'title':soup.title.get_text(' ',strip=True) if soup.title else '', 'headings':[x.get_text(' ',strip=True) for x in soup.select('h1,h2,h3')][:20]}),flush=True)
    except Exception as exc:print('DETAIL_ERROR',o['url'],str(exc)[:150],flush=True)
with ThreadPoolExecutor(max_workers=4) as pool:list(pool.map(detail,unknown))

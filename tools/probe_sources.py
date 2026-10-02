"""Public response diagnostics; no production state or email credentials."""
import sys,json,time
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
import requests
from bs4 import BeautifulSoup
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import monitor,collectors
ROOT=Path('source-diagnostics'); ROOT.mkdir(exist_ok=True)
sources=json.loads(Path('SOURCES.json').read_text())['sources']
names={'Home 4 Everyone','Smiley Real Estate','Aruba Happy Realty','Aruba Listings','RE/MAX Aruba','RES Aruba Realty'}
def probe(source):
    out=[]
    entries=source.get('urls',[source['url']])
    for turn in range(2 if source['name'] in ('Home 4 Everyone','Smiley Real Estate') else 1):
        for i,entry in enumerate(entries):
            url=entry['url'] if isinstance(entry,dict) else entry
            data=entry.get('data') if isinstance(entry,dict) else None
            result={'name':source['name'],'url':url,'turn':turn}
            started=time.monotonic()
            try:
                session=requests.Session();session.headers.update(monitor.HEADERS)
                r=session.post(url,data=data,timeout=(5,12)) if data else session.get(url,timeout=(5,12))
                result.update(status=r.status_code,final=r.url,bytes=len(r.content),content_type=r.headers.get('Content-Type'))
                soup=BeautifulSoup(r.text,'html.parser')
                result['title']=soup.title.get_text(' ',strip=True) if soup.title else ''
                filename=source['name'].replace(' ','_').replace('/','_')+f'_{turn}_{i}.html'
                (ROOT/filename).write_text(r.text)
                r.raise_for_status()
                batch,observations,count,_=collectors.parse_page(r.text,r.url,source,monitor)
                result.update(qualifying=len(batch),observations=len(observations),cards=count)
            except Exception as exc: result['error']=str(exc)[:350]
            result['seconds']=round(time.monotonic()-started,2)
            print(json.dumps(result),flush=True);out.append(result)
    return out
with ThreadPoolExecutor(max_workers=4) as pool:
    results=[r for group in pool.map(probe,[s for s in sources if s['name'] in names]) for r in group]
# Alternate inventory routes linked by the brokers' public websites.
alternates={'remax_listings':'https://remaxaruba.com/listings','res_residential':'https://resarubarealty.com/residential/','res_properties':'https://resarubarealty.com/property/','happy_objects':'https://www.arubahappyrealty.com/objects/','happy_apex':'https://arubahappyrealty.com/status/for-sale/','home_search':'https://homeforeveryonearuba.com/search-results/','smiley_sale':'https://smileyaruba.com/offer-type/for-sale/'}
for name,url in alternates.items():
    result={'name':name,'url':url}
    try:
        r=requests.get(url,headers=monitor.HEADERS,timeout=(5,12));soup=BeautifulSoup(r.text,'html.parser')
        result.update(status=r.status_code,final=r.url,bytes=len(r.content),title=soup.title.get_text(' ',strip=True) if soup.title else '',cards=len(soup.select('.item-listing-wrap')),myhome=bool('MyHomeListing' in r.text))
        (ROOT/(name+'.html')).write_text(r.text)
    except Exception as exc:result['error']=str(exc)[:350]
    print(json.dumps(result),flush=True);results.append(result)
(ROOT/'report.json').write_text(json.dumps(results,indent=2))

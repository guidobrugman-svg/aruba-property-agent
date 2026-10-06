"""Bounded read-only discovery of user-requested public broker inventories."""
import json,hashlib,sys,concurrent.futures
from pathlib import Path
from urllib.parse import urljoin,urlparse
from bs4 import BeautifulSoup
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import monitor as m
root=Path('source-diagnostics');root.mkdir(exist_ok=True)
candidates=json.load(open('tools/expansion_candidates.json'))
selectors=['.item-listing-wrap','article.property-item','a.property-card','.listing-item','article.card-listing','.rent-sec','a.properties-link','.property-item','.property-card']
def check(source):
 url=source['url'];result=dict(source)
 try:
  r=m.requests.get(url,headers=m.HEADERS,timeout=(4,8))
  result.update(status=r.status_code,final=r.url,bytes=len(r.content))
  soup=BeautifulSoup(r.text,'html.parser')
  result['title']=soup.title.get_text(' ',strip=True) if soup.title else ''
  result['selectors']={selector:len(soup.select(selector)) for selector in selectors if soup.select(selector)}
  result['links']=list(dict.fromkeys(urljoin(r.url,a['href']) for a in soup.select('a[href]') if any(p in a['href'].lower() for p in ('propert','listing','for-sale','for_sale','/sale','/buy','real_estate','/aruba','/residential','/land','/houses')) and not any(p in a['href'] for p in ('.css','.js','.png','.jpg'))))[:45]
  result['frameworks']=[f for f in ['houzez','myhome','__NEXT_DATA__','wp-content','wix','apimo','elementor'] if f.lower() in r.text.lower()]
  if r.status_code==200 and len(r.content)<3000000:
   filename=hashlib.sha256(url.encode()).hexdigest()[:12]+'.html'
   (root/filename).write_text(r.text)
   result['file']=filename
 except Exception as exc:result.update(error=type(exc).__name__,message=str(exc)[:220])
 return result
with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
 results=list(pool.map(check,candidates))
for result in results:print('DISCOVERY',json.dumps(result),flush=True)
(root/'discovery.json').write_text(json.dumps(results,indent=2))

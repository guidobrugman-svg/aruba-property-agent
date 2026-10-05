"""Bounded read-only public access diagnosis; TLS verification remains enabled."""
import json,sys,hashlib,concurrent.futures
from pathlib import Path
from bs4 import BeautifulSoup
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import monitor as m, collectors as c
root=Path('source-diagnostics');root.mkdir(exist_ok=True)
sources=json.load(open('SOURCES.json'))['sources']
urls=[
'https://www.arubahappyrealty.com/status/for-sale/',
'https://arubahappyrealty.com/status/for-sale/',
'https://www.arubahappyrealty.com/objects/',
'https://arubahappyrealty.com/',
'https://resarubarealty.com/status/for-sale/',
'https://resarubarealty.com/property/',
'https://resarubarealty.com/',
'https://remaxaruba.com/property/residential-for-sale',
'https://remaxaruba.com/property/land-for-sale',
'https://remaxaruba.com/',
'https://arubalistings.com/listings',
'https://arubalistings.com/sale/all',
'https://arubalistings.com/']
def check(url):
 try:
  response=m.requests.get(url,headers=m.HEADERS,timeout=(5,10))
  soup=BeautifulSoup(response.text,'html.parser')
  title=soup.title.get_text(' ',strip=True) if soup.title else ''
  data=dict(url=url,final=response.url,status=response.status_code,title=title,bytes=len(response.content),cards=len(soup.select('.item-listing-wrap,article.card-listing')),listing_links=len({a.get('href') for a in soup.select('a[href]') if any(p in a['href'] for p in ('/property/','/sale/','/listing','/objects/'))}))
  if response.status_code==200 and title.lower() not in ('just a moment...','access denied','attention required! | cloudflare'):
   (root/(hashlib.sha256(url.encode()).hexdigest()[:12]+'.html')).write_text(response.text)
   data['links']=sorted({a.get('href') for a in soup.select('a[href]') if any(p in a['href'] for p in ('/property/','/sale/','/listing','/objects/'))})[:30]
  return data
 except Exception as exc:return dict(url=url,error=type(exc).__name__,message=str(exc)[:350])
with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
 for result in pool.map(check,urls):print('ACCESS',json.dumps(result),flush=True)
for source in sources:
 if source['name'] in ('Home 4 Everyone','Smiley Real Estate'):
  items,obs,health=c.scrape(source,'deep',m,{})
  print('COVERAGE',source['name'],json.dumps(health),flush=True)

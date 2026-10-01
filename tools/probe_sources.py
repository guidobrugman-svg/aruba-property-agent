"""Read-only source diagnostics. Never uses email credentials or production state."""
import json
import time
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
import requests
from bs4 import BeautifulSoup

URLS = {
    'home_root': 'https://homeforeveryonearuba.com/',
    'home_sale': 'https://homeforeveryonearuba.com/status/for-sale/',
    'home_search': 'https://homeforeveryonearuba.com/search-results/',
    'smiley_root': 'https://smileyaruba.com/',
    'smiley_sale': 'https://smileyaruba.com/offer-type/for-sale/',
    'smiley_house': 'https://smileyaruba.com/property-type/house/',
    'remax_root': 'https://remaxaruba.com/',
    'remax_residential': 'https://remaxaruba.com/property/residential-for-sale',
    'remax_www': 'https://www.remaxaruba.com/',
    'res_root': 'https://resarubarealty.com/',
    'res_land': 'https://resarubarealty.com/property-type/land/',
    'happy_root': 'https://arubahappyrealty.com/',
    'happy_www': 'https://www.arubahappyrealty.com/',
    'listings_root': 'https://arubalistings.com/',
    'listings_sale': 'https://arubalistings.com/sale/all',
    'able': 'https://ablerealtyaruba.com/',
    'prima': 'https://aruba-realty.com/listings',
    'bhhs': 'https://bhhsaruba.com/for-sale',
    'rog': 'https://rogaruba.com/listings/for-sale',
    'realestate': 'https://www.arubarealestate.com/aruba-houses-for-sale/',
    'objective': 'https://www.objective-realty.com/',
    'hkg': 'https://hkgrealestatearuba.com/search/',
    'cc': 'https://www.ccrealestatearuba.com/',
    'bold': 'https://bold.realestate/',
    'mpg': 'https://www.mpgaruba.com/',
    'allproperty': 'https://www.allpropertyaruba.com/',
    'happyhomes': 'https://arubahappyhomes.com/',
    'elixir': 'https://elixirrealtyaruba.com/properties/for-sale',
    'solito': 'https://solitogroup.com/index.php',
}
ROOT=Path('source-diagnostics')
ROOT.mkdir(exist_ok=True)

def probe(pair):
    name,url=pair
    start=time.monotonic()
    result={'name':name,'url':url}
    try:
        r=requests.get(url,timeout=(5,10),headers={'User-Agent':'Mozilla/5.0'})
        (ROOT/(name+'.html')).write_text(r.text)
        soup=BeautifulSoup(r.text,'html.parser')
        result.update(status=r.status_code,final=r.url,bytes=len(r.content),title=soup.title.get_text() if soup.title else '',preview=soup.get_text(' ',strip=True)[:280])
        result['links']=[{'text':a.get_text(' ',strip=True)[:90],'href':a['href']} for a in soup.select('a[href]') if any(x in a['href'].lower() for x in ('sale','listing','property','properties','land','feed','sitemap','buy'))][:80]
        result['cards']={sel:len(soup.select(sel)) for sel in ('.item-listing-wrap','.property-item','.properties-grid > .card','article','[class*=property-card]','.search_result_row','.w-dyn-item')}
    except Exception as exc:
        result['error']=str(exc)[:250]
    result['seconds']=round(time.monotonic()-start,2)
    print(json.dumps({k:v for k,v in result.items() if k!='links'}),flush=True)
    return result

with ThreadPoolExecutor(max_workers=4) as pool:
    results=list(pool.map(probe,URLS.items()))
# Public front-end listing query (read-only POST), already advertised by Smiley.
data={'data[offer-type][compare]':'=','data[offer-type][key]':'offer-type','data[offer-type][slug]':'offer-type','data[offer-type][values][0][name]':'Sale Property','data[offer-type][values][0][value]':'for-sale','page':'1','limit':'50','sortBy':'newest','currency':'price'}
try:
    r=requests.post('https://smileyaruba.com/wp-json/myhome/v1/estates?currency=price',data=data,timeout=(5,10),headers={'User-Agent':'Mozilla/5.0'})
    (ROOT/'smiley_api.json').write_text(r.text)
    print('SMILEY_API',r.status_code,len(r.content),r.text[:120],flush=True)
except Exception as exc:
    print('SMILEY_API_ERROR',str(exc)[:200],flush=True)
(ROOT/'report.json').write_text(json.dumps(results,indent=2))

# Compare ordinary public request headers with the monitor's headers.
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import monitor
for label, headers in [('monitor',monitor.HEADERS),('cache_default',{k:v for k,v in monitor.HEADERS.items() if k not in ('Cache-Control','Pragma')}),('identified',{'User-Agent':'ArubaPropertyMonitor/2.0 (+https://github.com/guidobrugman-svg/aruba-property-agent)'})]:
    for name,url in [('home',URLS['home_sale']),('smiley','https://smileyaruba.com/wp-json/myhome/v1/estates?currency=price')]:
        try:
            r=requests.post(url,data=data,headers=headers,timeout=(5,10)) if name=='smiley' else requests.get(url,headers=headers,timeout=(5,10))
            (ROOT/(name+'_'+label+'.html')).write_text(r.text)
            print('HEADER_CHECK',name,label,r.status_code,len(r.content),r.text[:180],flush=True)
        except Exception as exc:
            print('HEADER_CHECK_ERROR',name,label,str(exc)[:180],flush=True)

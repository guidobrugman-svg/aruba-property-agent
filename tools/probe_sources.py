"""Read-only public Happy Homes commercial inventory diagnosis."""
import sys,json
from pathlib import Path
from bs4 import BeautifulSoup
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import monitor
root=Path('source-diagnostics');root.mkdir(exist_ok=True)
url='https://arubahappyhomes.com/listings/for-sale/commercial'
html,final=monitor.get_page(url)
(root/'happy_commercial.html').write_text(html)
soup=BeautifulSoup(html,'html.parser')
print('CARDS',len(soup.select('.rent-sec')),flush=True)
for n in soup.select('.rent-sec'):
 a=n.select_one('a.link-cover');title=n.select_one('.rent-contain > .position-relative')
 print('CARD',title.get_text(' ',strip=True) if title else '',n.get_text(' ',strip=True),flush=True)
 if a:
  from urllib.parse import urljoin
  detail=urljoin(final,a['href'])
  try:
   body,end=monitor.get_page(detail);(root/('detail_'+str(abs(hash(detail)))+'.html')).write_text(body)
   d=BeautifulSoup(body,'html.parser')
   print('DETAIL',detail,[(x.name,x.get('class'),x.get('id'),x.get_text(' ',strip=True)[:300]) for x in d.select('h1,h2,h3')],flush=True)
  except Exception as exc:print('DETAIL_ERROR',str(exc),flush=True)

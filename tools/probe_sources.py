"""Read-only public inventory diagnosis; normal TLS and no authentication."""
import concurrent.futures,hashlib,json
from pathlib import Path
from urllib.parse import urljoin
import requests
from bs4 import BeautifulSoup
root=Path("source-diagnostics");root.mkdir(exist_ok=True)
def check(entry):
 url=entry["url"]; result=dict(entry)
 try:
  response=requests.get(url,timeout=(5,12),headers={"User-Agent":"Mozilla/5.0 (compatible; ArubaPropertyMonitor/1.0)"})
  soup=BeautifulSoup(response.text,"html.parser")
  result.update(status=response.status_code,final=response.url,title=soup.title.get_text(" ",strip=True) if soup.title else "",bytes=len(response.content))
  if response.status_code==200 and result["title"].lower() not in ("just a moment...","access denied","attention required! | cloudflare"):
   key=hashlib.sha256(url.encode()).hexdigest()[:12]
   (root/(key+".html")).write_text(response.text)
   result["file"]=key+".html"
   result["links"]=[dict(text=a.get_text(" ",strip=True)[:90],href=urljoin(response.url,a["href"])) for a in soup.select("a[href]")][:180]
   result["scripts"]=[urljoin(response.url,s["src"]) for s in soup.select("script[src]")][-15:]
   result["text"]=soup.get_text(" ",strip=True)[:1600]
 except Exception as exc:result.update(error=type(exc).__name__,message=str(exc)[:300])
 return result
with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
 results=list(pool.map(check,json.load(open("tools/recheck_candidates.json"))))
(root/"discovery.json").write_text(json.dumps(results,indent=2))
for r in results:print(json.dumps({k:v for k,v in r.items() if k not in ("links","scripts","text")}),flush=True)

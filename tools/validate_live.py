"""Read-only live smoke test plus two replayed scans of the same production history."""
import json
import copy
import os
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import monitor as m
import collectors
import development

mode = sys.argv[1] if len(sys.argv)>1 else 'fast'
started=time.monotonic()
sources=json.load(open(m.SOURCES_FILE))['sources']
results=collectors.scan(sources,mode,{},m)
current=[]
for source,(items,observations,health) in results:
    current.extend(items)
    print(source['name'],json.dumps(health),flush=True)
current=m.cross_source_dedupe(current)
scan_seconds=round(time.monotonic()-started,2)
production=json.load(open('state.json'))
detail_started=time.monotonic()
current=development.enrich(current,production['properties'],mode,m)
current=[p for p in current if not p.get('residence_complex')]
detail_seconds=round(time.monotonic()-detail_started,2)
with tempfile.TemporaryDirectory() as folder:
    path=str(Path(folder)/'state.json')
    Path(path).write_text(json.dumps(production))
    with patch.object(m,'STATE_FILE',path),patch.object(m,'DRY_RUN',True),patch.dict(os.environ,{'SCAN_MODE':mode}),patch.object(collectors,'scan',return_value=results),patch.object(development,'enrich',side_effect=lambda *args:copy.deepcopy(current)):
        m.main()
        first=json.load(open(path))
        assert first['last_scan']['new']==0, 'Migration generated NEW alerts'
        assert first['daily_activity']==production.get('daily_activity',m.empty_daily_activity()), 'Migration discarded digest activity'
        assert set(production['properties']) <= set(first['properties']), 'History keys were lost'
        m.main()
        second=json.load(open(path))
        assert second['last_scan']['new']==0, 'Replayed inventory generated NEW alerts'
        assert second['last_scan']['major_changes']==0, 'Replayed inventory generated false major changes'
        assert second['last_scan']['reductions']==0, 'Replayed inventory generated false reductions'
        assert first['last_scan']['qualifying_observed']==second['last_scan']['qualifying_observed']
report={'mode':mode,'duration_seconds':round(time.monotonic()-started,2),'scan_seconds':scan_seconds,'detail_seconds':detail_seconds,'qualifying_count':len(current),
        'sources':{source['name']:health for source,(_,_,health) in results},
        'migration_false_new':0,'replay_false_new':0,'production_history_preserved':len(production['properties']),
        'properties':current}
Path('live-validation.json').write_text(json.dumps(report,indent=2))
print('VALIDATION SUMMARY',json.dumps({k:v for k,v in report.items() if k not in ('properties','sources')}))

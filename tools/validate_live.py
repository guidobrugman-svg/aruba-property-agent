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
sources=[s for s in json.load(open(m.SOURCES_FILE))['sources'] if s.get('enabled')]
results=collectors.scan(sources,mode,{},m)
current=[]
for source,(items,observations,health) in results:
    current.extend(items)
    print(source['name'],json.dumps(health),flush=True)
unavailable_urls={m.canonical_url(o['url']) for _,(_,observations,_) in results for o in observations if o.get('status')}
current=[p for p in current if m.canonical_url(p['url']) not in unavailable_urls]
current=m.cross_source_dedupe(current)
scan_seconds=round(time.monotonic()-started,2)
production=json.load(open('state.json'))
detail_started=time.monotonic()
current=development.enrich(current,production['properties'],mode,m)
current=[p for p in current if not p.get('residence_complex')]
current=m.history_dedupe(production['properties'],current)
detail_seconds=round(time.monotonic()-detail_started,2)
def replay(seed, passes):
    with tempfile.TemporaryDirectory() as folder:
        path=str(Path(folder)/'state.json')
        Path(path).write_text(json.dumps(seed))
        states=[]
        with patch.object(m,'STATE_FILE',path),patch.object(m,'DRY_RUN',True),patch.dict(os.environ,{'SCAN_MODE':mode,'DEFER_DELIVERY':'1'}),patch.object(collectors,'scan',return_value=results),patch.object(development,'enrich',side_effect=lambda *args:copy.deepcopy(current)):
            for _ in range(passes):
                m.main()
                state=json.load(open(path))
                assert set(production['properties']) <= set(state['properties']), 'History keys were lost'
                states.append(state)
        return states

# Exercise legacy migration even when production has already migrated to schema 2.
legacy=copy.deepcopy(production)
legacy['schema_version']=1
first,second=replay(legacy,2)
assert first['last_scan']['new']==0, 'Migration generated NEW alerts'
assert first['daily_activity']==production.get('daily_activity',m.empty_daily_activity()), 'Migration discarded digest activity'
for field in ('new','major_changes','reductions'):
    assert second['last_scan'][field]==0, 'Migration replay generated false '+field

# Also replay the actual current schema. Live price/metadata changes may correctly
# confirm on pass two; a third identical observation must generate no new event.
actual=replay(production,3)
print('REPLAY_CHANGE_FIELDS',json.dumps([{label:sum(label in e.get('changes',{}) for e in s.get('pending_major_changes',[])) for label in ('Bedrooms','Bathrooms','Size','Location','Status')} for s in actual]))
Path('live-validation.json').write_text(json.dumps({'sources':{source['name']:health for source,(_,_,health) in results}},indent=2))
for field in ('new','major_changes','reductions'):
    assert actual[-1]['last_scan'][field]==0, 'Settled inventory generated false '+field
assert len({s['last_scan']['qualifying_observed'] for s in actual})==1
report={'mode':mode,'duration_seconds':round(time.monotonic()-started,2),'scan_seconds':scan_seconds,'detail_seconds':detail_seconds,'qualifying_count':len(current),
        'sources':{source['name']:health for source,(_,_,health) in results},
        'migration_false_new':0,'replay_false_new':0,'production_replay_first':actual[0]['last_scan'],'production_replay_settled':actual[-1]['last_scan'],'production_history_preserved':len(production['properties']),
        'replay_change_fields':[{label:sum(label in e.get('changes',{}) for e in s.get('pending_major_changes',[])) for label in ('Bedrooms','Bathrooms','Size','Location','Status')} for s in actual]}
Path('live-validation.json').write_text(json.dumps(report,indent=2))
print('VALIDATION SUMMARY',json.dumps({k:v for k,v in report.items() if k not in ('properties','sources')}))

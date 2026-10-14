"""Read-only local crawler benchmarks; no HTTP or browser activity."""
import cProfile
import io
import json
import pstats
import sqlite3
import time
from pathlib import Path

from source_adapters import extract, host

def database_benchmark():
    c = sqlite3.connect('file:data/frontend_archive/state.sqlite?mode=ro', uri=True)
    queries = [
        ('target_dedup', "SELECT 1 FROM jobs WHERE kind='site' AND url=? LIMIT 1", ('https://not-existing-benchmark.example/',)),
        ('discovery_running', "SELECT COUNT(*) FROM jobs WHERE source=? AND kind IN ('discovery','sitemap','payload') AND status='running'", ('uiverse',)),
        ('source_targets', 'SELECT COUNT(DISTINCT target) FROM provenance WHERE source=?', ('neocities',)),
        ('running', "SELECT COUNT(*) FROM jobs WHERE status='running'", ()),
    ]
    result=[]
    for name, q, params in queries:
        start=time.perf_counter()
        for _ in range(50):
            c.execute(q,params).fetchall()
        result.append(dict(name=name,ms=(time.perf_counter()-start)*20,
                           plan=c.execute('EXPLAIN QUERY PLAN '+q,params).fetchall()))
    print(json.dumps({'sql':result,'jobs':c.execute('SELECT COUNT(*) FROM jobs').fetchone()[0]}))
    c.close()

def extraction_benchmark():
    sources=json.loads(Path('frontend_sources_config.json').read_text(encoding='utf-8'))['sources']
    hosts={host(s['url']) for s in sources}
    fixtures=[]
    for sid in ('neocities','uiverse','flowbite','onepagelove','framer','recent','maxibestof','httpster'):
        source=next(s for s in sources if s['id']==sid)
        files=sorted((Path('data/frontend_archive/directory')/sid).glob('*.html'),key=lambda p:p.stat().st_size,reverse=True)
        for path in files[:2]:
            meta=json.loads(path.with_suffix('.html.json').read_text(encoding='utf-8'))
            fixtures.append((source,path.read_bytes(),meta['final_url'],str(path)))
    profile=cProfile.Profile()
    start=time.perf_counter()
    profile.enable()
    results=[dict(file=p,bytes=len(raw),result=extract(s,raw,url,hosts)) for s,raw,url,p in fixtures]
    profile.disable()
    elapsed=time.perf_counter()-start
    result_path=Path('data/local_benchmark_results.json')
    if result_path.exists():
        previous=json.loads(result_path.read_text(encoding='utf-8'))
        print(json.dumps({'extraction_identical_to_previous':previous==results}))
    result_path.write_text(json.dumps(results,ensure_ascii=False),encoding='utf-8')
    output=io.StringIO()
    pstats.Stats(profile,stream=output).sort_stats('cumtime').print_stats(18)
    print(json.dumps({'parsing_seconds':elapsed,'pages':len(fixtures),'html_bytes':sum(len(raw) for _,raw,_,_ in fixtures)}))
    print(output.getvalue())

if __name__=='__main__':
    database_benchmark()
    extraction_benchmark()

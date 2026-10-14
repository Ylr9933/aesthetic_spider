"""Resumable HTTP archive of listed gallery targets, public components and repos.

The database queues discovery separately from target downloads. Gallery pages
are kept under directory/ and are never exported as downloaded target examples.
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat
import sys
import time
import zipfile
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path, PurePosixPath
from urllib.parse import urljoin, urlsplit

import aiohttp
from bs4 import BeautifulSoup

from crawl_onepagelove import AGENT, Robots, FetchError, capture, now, normalized, atomic_json
from source_adapters import extract, extract_payload, host, directory_url, repository_url, is_detail

SUCCESS = {'downloaded','downloaded_partial','captured','captured_partial','code_saved','repository_saved'}


def key(url):
    return hashlib.sha256(url.encode()).hexdigest()[:24]


class Archive:
    def __init__(self, out, sources):
        self.out, self.sources = out, {s['id']: s for s in sources}
        self.db = sqlite3.connect(out / 'state.sqlite')
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS jobs (
              kind TEXT, source TEXT, url TEXT, status TEXT DEFAULT 'pending',
              priority INTEGER DEFAULT 2, depth INTEGER DEFAULT 0, title TEXT,
              folder TEXT, error TEXT, result TEXT, updated TEXT,
              PRIMARY KEY(kind,source,url));
            CREATE INDEX IF NOT EXISTS queue ON jobs(kind,status,source,priority);
            CREATE TABLE IF NOT EXISTS sites (
              target TEXT PRIMARY KEY, source TEXT, title TEXT, status TEXT DEFAULT 'pending',
              folder TEXT, final_url TEXT, html_sha256 TEXT, updated TEXT);
            CREATE TABLE IF NOT EXISTS provenance (
              target TEXT, source TEXT, entry_url TEXT, evidence TEXT,
              PRIMARY KEY(target,source,entry_url));
            CREATE TABLE IF NOT EXISTS components (
              source TEXT, page_url TEXT, sha256 TEXT, language TEXT, file TEXT, evidence TEXT,
              PRIMARY KEY(source,page_url,sha256));
            CREATE TABLE IF NOT EXISTS source_state (
              source TEXT PRIMARY KEY, seed_url TEXT, status TEXT, error TEXT, updated TEXT);
        ''')
        self.db.execute("UPDATE jobs SET status='pending' WHERE status='running'")
        self.db.execute("UPDATE sites SET status='pending' WHERE status='running'")
        self.db.commit()
        self.started, self.phase, self.completed = now(), 'starting', 0
        self.offsets = Counter()

    def add_job(self, kind, source, url, depth=0, priority=2, title=''):
        self.db.execute('INSERT OR IGNORE INTO jobs(kind,source,url,depth,priority,title,updated) VALUES(?,?,?,?,?,?,?)',
                        (kind,source,normalized(url),depth,priority,title,now()))

    def add_site(self, source, record, existing_folder=None, existing_status=None):
        target = normalized(record['target'])
        self.db.execute('INSERT OR IGNORE INTO sites(target,source,title,updated) VALUES(?,?,?,?)',
                        (target,source,record['title'],now()))
        self.db.execute('INSERT OR REPLACE INTO provenance VALUES(?,?,?,?)',
                        (target,source,record['entry_url'],record.get('evidence','')))
        if not self.db.execute("SELECT 1 FROM jobs WHERE kind='site' AND url=? LIMIT 1",(target,)).fetchone():
            self.add_job('site',source,target,title=record['title'])
        if existing_folder and existing_status in SUCCESS and (Path(existing_folder)/'source.html').exists():
            self.db.execute('UPDATE sites SET status=?,folder=?,updated=? WHERE target=?',
                            (existing_status,str(Path(existing_folder).resolve()),now(),target))
            self.db.execute("UPDATE jobs SET status=?,folder=? WHERE kind='site' AND url=?",
                            (existing_status,str(Path(existing_folder).resolve()),target))

    def add_repo(self, source, url):
        url = repository_url(url)
        if url and not self.db.execute("SELECT 1 FROM jobs WHERE kind='repo' AND url=? LIMIT 1",(url,)).fetchone():
            self.add_job('repo',source,url)

    def source_status(self, source, status, error=''):
        self.db.execute('INSERT OR REPLACE INTO source_state VALUES(?,?,?,?,?)',
                        (source,self.sources[source]['url'],status,error,now()))

    def claim(self, kind, discovery_limit=0):
        ids = list(self.sources)
        for _ in ids:
            index = self.offsets[kind] % len(ids)
            self.offsets[kind] += 1
            source = ids[index]
            if kind in {'discovery','sitemap','payload'} and discovery_limit and self.db.execute(
                "SELECT COUNT(*) FROM jobs WHERE source=? AND kind='discovery' AND status NOT IN ('pending','running')",
                (source,)).fetchone()[0] >= discovery_limit:
                continue
            if kind in {'discovery','sitemap','payload'} and self.db.execute(
                "SELECT COUNT(*) FROM jobs WHERE source=? AND kind IN ('discovery','sitemap','payload') AND status='running'",
                (source,)).fetchone()[0] >= (1 if source == 'neocities' else 2):
                continue
            row = self.db.execute('SELECT url,title,depth FROM jobs WHERE kind=? AND source=? AND status=\'pending\' ORDER BY priority,rowid LIMIT 1',
                                  (kind,source)).fetchone()
            if row:
                url,title,depth = row
                self.db.execute("UPDATE jobs SET status='running',updated=? WHERE kind=? AND source=? AND url=?",
                                (now(),kind,source,url))
                if kind == 'site':
                    self.db.execute("UPDATE sites SET status='running',updated=? WHERE target=?",(now(),url))
                self.db.commit()
                return {'kind':kind,'source':source,'url':url,'title':title,'depth':depth}
        return None

    def has_eligible(self,kind,discovery_limit=0):
        for source in self.sources:
            if kind in {'discovery','sitemap','payload'} and discovery_limit and self.db.execute(
                "SELECT COUNT(*) FROM jobs WHERE source=? AND kind='discovery' AND status NOT IN ('pending','running')",
                (source,)).fetchone()[0]>=discovery_limit:
                continue
            if self.db.execute("SELECT 1 FROM jobs WHERE kind=? AND source=? AND status='pending' LIMIT 1",(kind,source)).fetchone():
                return True
        return False

    def finish(self, job, status, folder=None, error=None, result=None):
        self.db.execute('UPDATE jobs SET status=?,folder=?,error=?,result=?,updated=? WHERE kind=? AND source=? AND url=?',
                        (status,str(folder) if folder else None,error,json.dumps(result or {},ensure_ascii=False),now(),
                         job['kind'],job['source'],job['url']))
        if job['kind'] == 'site':
            self.db.execute('UPDATE sites SET status=?,folder=?,final_url=?,html_sha256=?,updated=? WHERE target=?',
                            (status,str(folder) if folder else None,(result or {}).get('final_url'),
                             (result or {}).get('html_sha256'),now(),job['url']))
        self.db.commit()
        if status != 'pending':
            self.completed += 1

    def save_snippets(self, source, page_url, snippets):
        for snippet in snippets:
            digest = hashlib.sha256(snippet['code'].encode()).hexdigest()
            ext = {'css':'.css','javascript':'.js','jsx':'.jsx','code':'.txt'}.get(snippet['language'],'.html')
            folder = self.out/'components'/source/key(page_url)
            folder.mkdir(parents=True,exist_ok=True)
            path = folder/(digest[:24]+ext)
            path.write_text(snippet['code'],encoding='utf-8')
            self.db.execute('INSERT OR IGNORE INTO components VALUES(?,?,?,?,?,?)',
                            (source,page_url,digest,snippet['language'],str(path.resolve()),snippet['evidence']))
            atomic_json(path.with_suffix(path.suffix+'.json'),{
                'source':source,'page_url':page_url,'code_kind':'public_component_source',
                'language':snippet['language'],'sha256':digest,'evidence':snippet['evidence'],
                'rendered':False,'screenshots':False,'license':'not_verified',
                'dependencies':'not_evaluated','collected_at':now()})

    def export(self, full=False):
        report = {'started_at':self.started,'updated_at':now(),'phase':self.phase,'pid':os.getpid(),
                  'mode':'http_only','browser':False,'screenshots':False,'source_count':len(self.sources),
                  'completed_jobs_this_run':self.completed,
                  'sites':dict(self.db.execute('SELECT status,COUNT(*) FROM sites GROUP BY status')),
                  'jobs':[dict(kind=k,status=s,count=n) for k,s,n in self.db.execute('SELECT kind,status,COUNT(*) FROM jobs GROUP BY kind,status')],
                  'components':self.db.execute('SELECT COUNT(*) FROM components').fetchone()[0],
                  'component_unique_hashes':self.db.execute('SELECT COUNT(DISTINCT sha256) FROM components').fetchone()[0],
                  'sources':{sid:{'url':source['url'],'kind':source['kind'],
                      'state':self.db.execute('SELECT status,error FROM source_state WHERE source=?',(sid,)).fetchone(),
                      'discovery':dict(self.db.execute("SELECT status,COUNT(*) FROM jobs WHERE source=? AND kind IN ('discovery','sitemap','payload') GROUP BY status",(sid,))),
                      'targets':self.db.execute('SELECT COUNT(DISTINCT target) FROM provenance WHERE source=?',(sid,)).fetchone()[0]}
                    for sid,source in self.sources.items()},
                  'output':str(self.out),'stop_file':str(self.out/'STOP')}
        atomic_json(self.out/'progress.json',report)
        if not full:
            return report
        with (self.out/'sites_manifest.csv.tmp').open('w',encoding='utf-8-sig',newline='') as f:
            writer=csv.writer(f)
            writer.writerow(['target_url','final_url','title','status','sources','local_html','local_assets','metadata','html_sha256'])
            for target,final,title,status,folder,digest in self.db.execute('SELECT target,final_url,title,status,folder,html_sha256 FROM sites'):
                if status not in SUCCESS or not folder or not (Path(folder)/'source.html').exists():
                    continue
                sources=';'.join(r[0] for r in self.db.execute('SELECT DISTINCT source FROM provenance WHERE target=?',(target,)))
                writer.writerow([target,final,title,status,sources,str(Path(folder)/'source.html'),str(Path(folder)/'assets'),str(Path(folder)/'metadata.json'),digest])
        (self.out/'sites_manifest.csv.tmp').replace(self.out/'sites_manifest.csv')
        with (self.out/'components_manifest.jsonl.tmp').open('w',encoding='utf-8') as f:
            for row in self.db.execute('SELECT source,page_url,sha256,language,file,evidence FROM components'):
                f.write(json.dumps(dict(zip(['source','page_url','sha256','language','file','evidence'],row)),ensure_ascii=False)+'\n')
        (self.out/'components_manifest.jsonl.tmp').replace(self.out/'components_manifest.jsonl')
        with (self.out/'catalog.jsonl.tmp').open('w',encoding='utf-8') as f:
            for row in self.db.execute('SELECT kind,source,url,status,folder,error,result FROM jobs'):
                f.write(json.dumps(dict(zip(['kind','source','url','status','folder','error','result'],row)),ensure_ascii=False)+'\n')
        (self.out/'catalog.jsonl.tmp').replace(self.out/'catalog.jsonl')
        with (self.out/'provenance.jsonl.tmp').open('w',encoding='utf-8') as f:
            for row in self.db.execute('SELECT target,source,entry_url,evidence FROM provenance'):
                f.write(json.dumps(dict(zip(['target','source','entry_url','evidence'],row)),ensure_ascii=False)+'\n')
        (self.out/'provenance.jsonl.tmp').replace(self.out/'provenance.jsonl')
        atomic_json(self.out/'source_report.json',report['sources'])
        with (self.out/'repositories_manifest.jsonl.tmp').open('w',encoding='utf-8') as f:
            for source,url,folder,result in self.db.execute("SELECT source,url,folder,result FROM jobs WHERE kind='repo' AND status='repository_saved'"):
                f.write(json.dumps({'source':source,'repository_url':url,'local_source':str(Path(folder)/'source'),
                                    'metadata':str(Path(folder)/'metadata.json'),'details':json.loads(result)},ensure_ascii=False)+'\n')
        (self.out/'repositories_manifest.jsonl.tmp').replace(self.out/'repositories_manifest.jsonl')
        return report


def import_onepagelove(archive, root):
    """Reuse actual downloaded files and the existing public discovery queue."""
    database = root/'state.sqlite'
    if not database.exists() or 'onepagelove' not in archive.sources:
        return 0
    old = sqlite3.connect(database.resolve().as_uri()+'?mode=ro',uri=True)
    count = 0
    try:
        for url,title,target,status,folder in old.execute('SELECT url,title,target,status,folder FROM entries'):
            if target:
                archive.add_site('onepagelove',{'target':target,'title':title,'entry_url':url,'evidence':'existing_opl_catalog'},
                                 root/folder if folder else None,status)
                count += 1
            elif status in {'pending','running','failed','http_failed'}:
                archive.add_job('discovery','onepagelove',url,priority=3,title=title)
        archive.db.commit()
    finally:
        old.close()
    return count


async def discover(job, archive, robots, args, gallery_hosts):
    source = archive.sources[job['source']]
    response = await robots.fetch(job['url'],max_bytes=12*1024**2,minimum=args.directory_delay,timeout=40)
    final = response['final_url']
    folder = archive.out/'directory'/source['id']
    folder.mkdir(parents=True,exist_ok=True)
    raw = response['body']
    path = folder/(key(job['url'])+('.xml' if job['kind']=='sitemap' else '.json' if job['kind']=='payload' else '.html'))
    path.write_bytes(raw)
    atomic_json(path.with_suffix(path.suffix+'.json'),{'requested_url':job['url'],'final_url':final,'fetched_at':now(),'http_status':response['status']})
    if host(final) != host(source['url']):
        raise RuntimeError('directory_redirected_to_other_host: '+final)
    if job['kind']=='payload':
        if source['id']=='fwa' and urlsplit(final).path=='/api/cases/':
            data=json.loads(raw)
            targets=0
            for case in data.get('cases',[]):
                if not isinstance(case,dict):
                    continue
                if case.get('url'):
                    from source_adapters import target_url
                    if target_url(source,case['url'],gallery_hosts):
                        archive.add_site(source['id'],{'target':case['url'],'title':case.get('title') or case.get('slug') or case['url'],
                            'entry_url':urljoin(source['url'],'/cases/'+case['slug']) if case.get('slug') else final,
                            'evidence':'public_fwa_cases_endpoint'})
                        targets+=1
                elif case.get('slug'):
                    archive.add_job('discovery',source['id'],urljoin(source['url'],'/cases/'+case['slug']),priority=2)
            next_url=data.get('next')
            if isinstance(next_url,str) and host(next_url)==host(source['url']) and urlsplit(next_url).path=='/api/cases/' and data.get('cases'):
                archive.add_job('payload',source['id'],next_url,priority=1)
            archive.db.commit()
            return 'discovered',path,{'targets':targets,'reported_total':data.get('totalCases'),'next':next_url}
        result=extract_payload(source,raw,final,gallery_hosts)
        for record in result['targets']:
            archive.add_site(source['id'],record)
        for url in result['follow']:
            archive.add_job('discovery',source['id'],url,priority=2)
        archive.db.commit()
        return 'discovered',path,{k:len(v) for k,v in result.items()}
    if job['kind'] == 'sitemap':
        root = ET.fromstring(raw)
        if root.tag.rsplit('}',1)[-1] == 'sitemapindex':
            for node in root.iter():
                if node.tag.rsplit('}',1)[-1] == 'loc' and node.text:
                    url = urljoin(final,node.text.strip())
                    if host(url) == host(source['url']) and job['depth'] < 8:
                        # Restrict huge mixed sitemaps to relevant namespaces when obvious.
                        if source['id'] == 'framer' and '/marketplace/' not in urlsplit(url).path:
                            continue
                        archive.add_job('sitemap',source['id'],url,job['depth']+1,priority=1)
        else:
            for node in root.iter():
                if node.tag.rsplit('}',1)[-1] in {'loc','link'}:
                    url = (node.text or node.attrib.get('href','')).strip()
                    if url and directory_url(source,url):
                        archive.add_job('discovery',source['id'],url,priority=2 if is_detail(source,url) else 1)
                    elif source['id'] == 'neocities' and host(url).endswith('.neocities.org'):
                        archive.add_site(source['id'],{'target':url,'title':host(url),'entry_url':job['url'],'evidence':'public_sitemap'})
        archive.db.commit()
        return 'discovered',path,{'format':'sitemap'}
    soup = BeautifulSoup(raw,'html.parser')
    title = soup.title.get_text(' ',strip=True).lower() if soup.title else ''
    if any(t in title for t in ('just a moment','verify you are human','access denied','attention required')):
        raise RuntimeError('challenge_page')
    result = extract(source,raw,final,gallery_hosts)
    for record in result['targets']:
        archive.add_site(source['id'],record)
    for url in result['follow']:
        if job['depth'] < args.max_discovery_depth and directory_url(source,url):
            archive.add_job('discovery',source['id'],url,job['depth']+1,priority=2 if is_detail(source,url) else 1)
    for repo in result['repositories']:
        archive.add_repo(source['id'],repo)
    for url in result['payloads']:
        archive.add_job('payload',source['id'],url,priority=1)
    archive.save_snippets(source['id'],final,result['snippets'])
    # Public sitemap discovery is performed once per source, including robots hints.
    if normalized(job['url']) == normalized(source['url']):
        policy = await robots.rule(final)
        sitemap_urls = policy.site_maps() or [urljoin(final,'/sitemap.xml')]
        for url in sitemap_urls:
            if host(url) == host(source['url']) and not any(t in urlsplit(url).path for t in ('/blog/','/profiles/')):
                archive.add_job('sitemap',source['id'],url,priority=1)
        archive.source_status(source['id'],'active')
    archive.db.commit()
    info = {k:len(result[k]) for k in ('targets','follow','repositories','snippets')}
    info['detail'],info['final_url'] = result['detail'],final
    status = 'code_saved' if result['snippets'] else 'discovered' if result['targets'] or result['follow'] or result['repositories'] else 'no_public_target'
    return status,path,info


async def download_site(job, archive, robots, args, gallery_hosts):
    rows = archive.db.execute('SELECT source,entry_url,evidence FROM provenance WHERE target=?',(job['url'],)).fetchall()
    record = {'url':job['url'],'target':job['url'],'title':job['title'] or host(job['url']),
              'source':job['source'],'provenance':[dict(source=s,entry_url=u,evidence=e) for s,u,e in rows]}
    try:
        async with asyncio.timeout(args.site_timeout):
            meta,folder = await capture(record,robots,archive.out,args)
    except TimeoutError:
        slug = re.sub(r'[^a-zA-Z0-9._-]+','-',urlsplit(job['url']).path.strip('/'))[:90] or 'site'
        folder = archive.out/'sites'/(slug+'-'+hashlib.sha256(job['url'].encode()).hexdigest()[:8])
        meta_path = folder/'metadata.json'
        meta = json.loads(meta_path.read_text(encoding='utf-8')) if meta_path.exists() else {'status':'failed'}
        meta['status'],meta['error'] = 'failed','site_timeout'
    final = meta.get('final_url','')
    if meta['status'] in SUCCESS and final and host(final) in gallery_hosts:
        if not (job['source']=='codrops' and re.match(r'^/(?:Tutorials|Development)/',urlsplit(final).path)):
            meta['status'],meta['error'] = 'redirect_to_gallery','Target redirected back to a discovery source'
    raw = folder/'source.html'
    if raw.exists():
        meta['html_sha256'] = hashlib.sha256(raw.read_bytes()).hexdigest()
    atomic_json(folder/'metadata.json',meta)
    return meta['status'],folder,meta


async def download_repo(job, archive, robots, args):
    parts = urlsplit(job['url']).path.strip('/').split('/')[:2]
    folder = archive.out/'repositories'/(parts[0]+'--'+parts[1])
    folder.mkdir(parents=True,exist_ok=True)
    url = 'https://codeload.github.com/'+'/'.join(parts)+'/zip/HEAD'
    archive_file,temp = folder/'source.zip',folder/'source.zip.part'
    if not archive_file.exists() or not zipfile.is_zipfile(archive_file):
        try:
            await robots.fetch(url,max_bytes=args.max_repo_bytes,minimum=1,destination=temp,timeout=300)
            temp.replace(archive_file)
        finally:
            temp.unlink(missing_ok=True)
    files,bytes_written,licenses = 0,0,[]
    dest = (folder/'source').resolve()
    dest.mkdir(exist_ok=True)
    with zipfile.ZipFile(archive_file) as bundle:
        members = bundle.infolist()
        if len(members) > 150000 or sum(m.file_size for m in members) > args.max_repo_bytes*8:
            raise RuntimeError('repository_extract_budget')
        roots = {PurePosixPath(m.filename).parts[0] for m in members if PurePosixPath(m.filename).parts}
        single_root = len(roots) == 1
        for entry in members:
            relative = PurePosixPath(entry.filename)
            if single_root:
                relative = PurePosixPath(*relative.parts[1:])
            if entry.is_dir() or not relative.parts:
                continue
            if relative.is_absolute() or any(p in {'..','.'} or ':' in p or '\\' in p for p in relative.parts):
                raise RuntimeError('unsafe_archive_path')
            if stat.S_ISLNK(entry.external_attr >> 16):
                continue
            if any(p in {'.git','node_modules','.venv','__pycache__'} for p in relative.parts):
                continue
            target = dest.joinpath(*relative.parts)
            if not target.resolve().is_relative_to(dest):
                raise RuntimeError('unsafe_archive_path')
            target.parent.mkdir(parents=True,exist_ok=True)
            with bundle.open(entry) as input_file,target.open('wb') as output_file:
                shutil.copyfileobj(input_file,output_file,65536)
            files += 1
            bytes_written += entry.file_size
            if re.match(r'^(?:LICENSE|COPYING|NOTICE)',target.name,re.I):
                licenses.append(target.relative_to(dest).as_posix())
            await asyncio.sleep(0)
        revision = bundle.comment.decode('ascii','replace')
    meta = {'repository_url':job['url'],'archive_url':url,'revision_comment':revision,
            'source':job['source'],'code_kind':'public_repository_source','original_project_source':True,
            'file_count':files,'extracted_bytes':bytes_written,'license_files':licenses,
            'license':'inspect_license_files' if licenses else 'not_found',
            'dependencies':'not_evaluated','rendered':False,'screenshots':False,'collected_at':now()}
    atomic_json(folder/'metadata.json',meta)
    return 'repository_saved',folder,meta


async def main(args):
    sources = json.loads(Path(args.config).read_text(encoding='utf-8'))['sources']
    if args.sources:
        requested=set(args.sources.split(','))
        unknown=requested-{s['id'] for s in sources}
        if unknown:
            raise ValueError('Unknown source ids: '+','.join(sorted(unknown)))
        sources=[s for s in sources if s['id'] in requested]
    out=Path(args.output).resolve()
    out.mkdir(parents=True,exist_ok=True)
    if (out/'STOP').exists():
        raise RuntimeError('STOP exists: remove it explicitly before resuming')
    for name in ('sites','directory','components','repositories'):
        (out/name).mkdir(exist_ok=True)
    lock=out/'crawl.lock'
    with lock.open('x',encoding='utf-8') as handle:
        handle.write(str(os.getpid()))
    archive=Archive(out,sources)
    gallery_hosts={host(s['url']) for s in json.loads(Path(args.config).read_text(encoding='utf-8'))['sources']}
    stop=asyncio.Event()
    jobs=[]
    start=time.monotonic()
    try:
        for source in sources:
            archive.add_job('discovery',source['id'],source['url'],priority=0)
            for url in source.get('seed_urls',[]):
                archive.add_job('discovery',source['id'],url,priority=0)
            for url in source.get('public_json_urls',[]):
                archive.add_job('payload',source['id'],url,priority=0)
            for repo in source.get('repositories',[]):
                archive.add_repo(source['id'],'https://github.com/'+repo)
        reused=import_onepagelove(archive,Path(args.opl_archive).resolve())
        if args.retry_failed:
            archive.db.execute("UPDATE jobs SET status='pending',error=NULL WHERE status IN ('failed','http_failed','robots_skipped','discovery_failed')")
        archive.db.commit()
        atomic_json(out/'config.json',dict(vars(args),sources_config=sources))
        archive.phase='crawling'
        print(json.dumps({'event':'start','pid':os.getpid(),'output':str(out),'sources':len(sources),'imported_opl_targets':reused}),flush=True)
        connector=aiohttp.TCPConnector(limit=args.connections,limit_per_host=args.per_host,ttl_dns_cache=300)
        async with aiohttp.ClientSession(connector=connector,headers={'User-Agent':AGENT}) as session:
            robots=Robots(session,out,connections=args.connections)
            claimed=Counter()

            async def worker(kind):
                while not stop.is_set():
                    if kind=='site' and args.site_limit and claimed[kind]>=args.site_limit:
                        return
                    if kind=='repo' and args.repo_limit and claimed[kind]>=args.repo_limit:
                        return
                    job=archive.claim(kind,args.discovery_limit)
                    if job is None:
                        await asyncio.sleep(.5)
                        continue
                    claimed[kind]+=1
                    try:
                        if kind in {'discovery','sitemap','payload'}:
                            async with asyncio.timeout(70):
                                status,folder,result=await discover(job,archive,robots,args,gallery_hosts)
                        elif kind=='site':
                            status,folder,result=await download_site(job,archive,robots,args,gallery_hosts)
                        else:
                            async with asyncio.timeout(600):
                                status,folder,result=await download_repo(job,archive,robots,args)
                        archive.finish(job,status,folder,result.get('error'),result)
                        print(json.dumps({'event':kind,'source':job['source'],'url':job['url'],'status':status},ensure_ascii=False),flush=True)
                    except asyncio.CancelledError:
                        archive.finish(job,'pending',error='paused_before_completion')
                        raise
                    except Exception as e:
                        error=str(e) or type(e).__name__
                        status='robots_skipped' if 'robots_' in error else 'discovery_failed' if kind in {'discovery','sitemap','payload'} else 'failed'
                        archive.finish(job,status,error=error)
                        if kind=='discovery' and normalized(job['url'])==normalized(archive.sources[job['source']]['url']):
                            archive.source_status(job['source'],'unavailable',error)
                            archive.db.commit()
                        print(json.dumps({'event':kind+'_error','source':job['source'],'url':job['url'],'error':error},ensure_ascii=False),flush=True)

            async def control():
                idle=0
                while not stop.is_set():
                    if (out/'STOP').exists():
                        archive.phase='stopped'
                        stop.set()
                        return
                    if args.run_seconds and time.monotonic()-start>=args.run_seconds:
                        archive.phase='sample_complete'
                        stop.set()
                        return
                    if shutil.disk_usage(out).free<10*1024**3:
                        archive.phase='low_disk_stopped'
                        stop.set()
                        return
                    running=archive.db.execute("SELECT COUNT(*) FROM jobs WHERE status='running'").fetchone()[0]
                    eligible=any(archive.has_eligible(k,args.discovery_limit) for k in ('discovery','sitemap','payload'))
                    eligible=eligible or (not args.site_limit or claimed['site']<args.site_limit) and archive.has_eligible('site')
                    eligible=eligible or (not args.repo_limit or claimed['repo']<args.repo_limit) and archive.has_eligible('repo')
                    idle=idle+1 if not running and not eligible else 0
                    if idle>=4:
                        archive.phase='sample_complete' if args.discovery_limit or args.site_limit or args.repo_limit else 'frontier_exhausted'
                        stop.set()
                        return
                    await asyncio.sleep(.5)

            async def reporter():
                tick=0
                while not stop.is_set():
                    archive.export(full=tick%6==0)
                    tick+=1
                    await asyncio.sleep(10)

            jobs=[asyncio.create_task(worker('discovery')) for _ in range(args.discovery_workers)]
            jobs += [asyncio.create_task(worker('sitemap')) for _ in range(4)]
            jobs += [asyncio.create_task(worker('payload')) for _ in range(4)]
            jobs += [asyncio.create_task(worker('site')) for _ in range(args.workers)]
            jobs += [asyncio.create_task(worker('repo')) for _ in range(args.repo_workers)]
            jobs += [asyncio.create_task(control()),asyncio.create_task(reporter())]
            await stop.wait()
            for task in jobs:
                task.cancel()
            await asyncio.gather(*jobs,return_exceptions=True)
    finally:
        for task in jobs:
            if not task.done():
                task.cancel()
        if jobs:
            await asyncio.gather(*jobs,return_exceptions=True)
        archive.db.execute("UPDATE jobs SET status='pending' WHERE status='running'")
        archive.db.execute("UPDATE sites SET status='pending' WHERE status='running'")
        archive.db.commit()
        if archive.phase=='crawling':
            archive.phase='interrupted'
        report=archive.export(full=True)
        report['pid']=None
        atomic_json(out/'progress.json',report)
        archive.db.close()
        lock.unlink(missing_ok=True)
        print(json.dumps({'event':'finished','phase':report['phase'],'sites':report['sites'],'components':report['components']},ensure_ascii=False),flush=True)


def arguments():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config',default='frontend_sources_config.json')
    parser.add_argument('--output',default='data/frontend_archive')
    parser.add_argument('--opl-archive',default='data/onepagelove')
    parser.add_argument('--sources',default='',help='Comma-separated ids; empty means all configured sources')
    parser.add_argument('--workers',type=int,default=96)
    parser.add_argument('--connections',type=int,default=384)
    parser.add_argument('--per-host',type=int,default=4)
    parser.add_argument('--asset-workers',type=int,default=8)
    parser.add_argument('--discovery-workers',type=int,default=24)
    parser.add_argument('--repo-workers',type=int,default=2)
    parser.add_argument('--directory-delay',type=float,default=.8)
    parser.add_argument('--max-resources',type=int,default=500)
    parser.add_argument('--site-timeout',type=int,default=120)
    parser.add_argument('--max-discovery-depth',type=int,default=2000)
    parser.add_argument('--max-repo-bytes',type=int,default=512*1024**2)
    parser.add_argument('--run-seconds',type=int,default=0,help='Timed verification run; zero means no time limit')
    parser.add_argument('--site-limit',type=int,default=0)
    parser.add_argument('--repo-limit',type=int,default=0)
    parser.add_argument('--discovery-limit',type=int,default=0,help='Maximum processed HTML pages per source; zero means unlimited')
    parser.add_argument('--retry-failed',action='store_true')
    args=parser.parse_args()
    if min(args.workers,args.connections,args.per_host,args.asset_workers,args.discovery_workers,args.repo_workers,args.max_resources,args.site_timeout)<1:
        parser.error('Concurrency and resource limits must be positive')
    if min(args.run_seconds,args.site_limit,args.repo_limit,args.discovery_limit,args.max_discovery_depth)<0 or args.directory_delay<0:
        parser.error('Limits and delays cannot be negative')
    return args


if __name__=='__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    asyncio.run(main(arguments()))

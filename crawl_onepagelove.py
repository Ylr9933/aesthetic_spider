"""Resumable One Page Love directory + published frontend archive.

Captures public landing pages and delivered assets, not original app source.
No logins, purchases, form submissions, or anti-bot circumvention.
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import hashlib
import html
import ipaddress
import json
import mimetypes
import os
import re
import shutil
import sqlite3
import sys
import time
import urllib.robotparser
import xml.etree.ElementTree as ET
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

import aiohttp
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright

ROOT = 'https://onepagelove.com'
AGENT = 'FrontendArchive/1.0'
LIMIT_FILE = 12 * 1024 * 1024
LIMIT_SITE = 96 * 1024 * 1024
TRACKING = {'ref', 'utm_source', 'utm_medium', 'utm_campaign', 'utm_content', 'utm_term'}


def now():
    return datetime.now(timezone.utc).isoformat()


def normalized(url):
    p = urlsplit(url)
    return urlunsplit((p.scheme, p.netloc, p.path or '/',
                      urlencode([(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True)
                                 if k not in TRACKING]), ''))


def public_url(url):
    p = urlsplit(url)
    if p.scheme not in {'https', 'http'} or not p.hostname or p.username or p.password:
        return False
    if p.hostname.lower() in {'localhost', 'localhost.localdomain'}:
        return False
    try:
        return ipaddress.ip_address(p.hostname).is_global
    except ValueError:
        return True


def atomic_json(path, value):
    tmp = path.with_suffix(path.suffix + '.tmp')
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    tmp.replace(path)


class Robots:
    def __init__(self, session, out):
        self.session, self.out = session, out
        self.rules, self.locks, self.last = {}, {}, {}
        self.backoff_origins = set()
        (out / 'robots').mkdir(exist_ok=True)

    async def rule(self, url):
        p = urlsplit(url)
        origin = f'{p.scheme}://{p.netloc}'
        lock = self.locks.setdefault(origin + ':robots', asyncio.Lock())
        async with lock:
            if origin in self.rules:
                return self.rules[origin]
            parser = urllib.robotparser.RobotFileParser(origin + '/robots.txt')
            result = {'origin': origin, 'checked_at': now()}
            try:
                async with self.session.get(origin + '/robots.txt', allow_redirects=True,
                                            timeout=aiohttp.ClientTimeout(total=15)) as r:
                    result['status'] = r.status
                    if r.status == 200:
                        raw = await r.read()
                        if len(raw) > 512 * 1024:
                            raise RuntimeError('robots response too large')
                        result['text'] = raw.decode('utf-8', 'replace')
                        parser.parse(result['text'].splitlines())
                    elif r.status in {404, 410}:
                        parser.parse([])
                    else:
                        parser.disallow_all = True
                        result['error'] = 'robots unavailable; skipped conservatively'
            except Exception as e:
                parser.disallow_all = True
                result['error'] = str(e)[:500]
            atomic_json(self.out / 'robots' / (hashlib.sha256(origin.encode()).hexdigest()[:20] + '.json'), result)
            self.rules[origin] = parser
            return parser

    async def allowed(self, url):
        p = urlsplit(url)
        origin = f'{p.scheme}://{p.netloc}'
        return (public_url(url) and origin not in self.backoff_origins
                and (await self.rule(url)).can_fetch(AGENT, url))

    async def pace(self, url, minimum=.04):
        p = urlsplit(url)
        origin = f'{p.scheme}://{p.netloc}'
        rule = await self.rule(url)
        delay = max(minimum, rule.crawl_delay(AGENT) or rule.crawl_delay('*') or 0)
        rate = rule.request_rate(AGENT) or rule.request_rate('*')
        if rate and rate.requests:
            delay = max(delay, rate.seconds / rate.requests)
        lock = self.locks.setdefault(origin + ':pace', asyncio.Lock())
        async with lock:
            await asyncio.sleep(max(0, self.last.get(origin, 0) + delay - time.monotonic()))
            self.last[origin] = time.monotonic()

    async def get(self, url, max_bytes=4 * 1024 * 1024, minimum=.8):
        for hop in range(8):
            if not await self.allowed(url):
                raise RuntimeError('robots_disallowed_or_unavailable: ' + url)
            await self.pace(url, minimum)
            async with self.session.get(url, allow_redirects=False,
                                        timeout=aiohttp.ClientTimeout(total=30)) as r:
                if r.status in {301, 302, 303, 307, 308}:
                    url = urljoin(url, r.headers['Location'])
                    continue
                if r.status in {429, 503}:
                    p = urlsplit(url)
                    self.backoff_origins.add(f'{p.scheme}://{p.netloc}')
                    raise RuntimeError(f'origin_backoff_required: HTTP {r.status}: {url}')
                r.raise_for_status()
                chunks, size = [], 0
                async for chunk in r.content.iter_chunked(65536):
                    size += len(chunk)
                    if size > max_bytes:
                        raise RuntimeError('response too large: ' + url)
                    chunks.append(chunk)
                raw = b''.join(chunks)
                return raw
        raise RuntimeError('too many redirects')


class Store:
    def __init__(self, out):
        self.out = out
        self.db = sqlite3.connect(out / 'state.sqlite')
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.executescript('''
          CREATE TABLE IF NOT EXISTS entries (
            url TEXT PRIMARY KEY, title TEXT, target TEXT, data TEXT,
            status TEXT DEFAULT 'pending', folder TEXT, error TEXT, updated TEXT);
          CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
        ''')
        self.db.execute("UPDATE entries SET status='pending' WHERE status='running'")
        self.db.commit()
        self.started, self.phase = now(), 'starting'

    def setting(self, key, value=None):
        if value is not None:
            self.db.execute('INSERT OR REPLACE INTO settings VALUES (?,?)', (key, str(value)))
            self.db.commit()
        row = self.db.execute('SELECT value FROM settings WHERE key=?', (key,)).fetchone()
        return row[0] if row else None

    def put(self, record, commit=True):
        self.db.execute('''INSERT INTO entries(url,title,target,data,updated) VALUES (?,?,?,?,?)
          ON CONFLICT(url) DO UPDATE SET title=excluded.title,
          target=excluded.target,data=excluded.data,updated=excluded.updated''',
          (record['url'], record['title'], record.get('target'), json.dumps(record, ensure_ascii=False), now()))
        if commit:
            self.db.commit()

    def status(self, url, status, folder=None, error=None):
        self.db.execute('UPDATE entries SET status=?,folder=?,error=?,updated=? WHERE url=?',
                        (status, folder, error, now(), url))
        self.db.commit()

    def pending(self):
        row = self.db.execute("SELECT data FROM entries WHERE status='pending' ORDER BY rowid LIMIT 1").fetchone()
        return json.loads(row[0]) if row else None

    def export(self):
        counts = dict(self.db.execute('SELECT status,COUNT(*) FROM entries GROUP BY status'))
        report = {'started_at': self.started, 'updated_at': now(), 'phase': self.phase,
                  'pid': os.getpid(), 'discovered': sum(counts.values()), 'counts': counts,
                  'last_catalog_page': self.setting('last_catalog_page'),
                  'catalog_complete': self.setting('catalog_complete') == '1',
                  'sitemap_candidate_count': self.setting('sitemap_candidate_count'),
                  'scope': 'Public listed landing pages and delivered frontend assets; not original project source',
                  'stop': 'Create STOP file in this output directory to stop gracefully'}
        atomic_json(self.out / 'progress.json', report)
        rows = list(self.db.execute('SELECT url,title,target,status,folder,error FROM entries ORDER BY rowid'))
        tmp = self.out / 'catalog.jsonl.tmp'
        with tmp.open('w', encoding='utf-8') as f:
            for url, title, target, status, folder, error in rows:
                f.write(json.dumps(dict(url=url, title=title, target=target, status=status,
                                       folder=folder, error=error), ensure_ascii=False) + '\n')
        tmp.replace(self.out / 'catalog.jsonl')
        with (self.out / 'catalog.csv').open('w', encoding='utf-8-sig', newline='') as f:
            writer = csv.writer(f)
            writer.writerow(['opl_url', 'title', 'target_url', 'status', 'folder', 'error'])
            writer.writerows(rows)
        cards = []
        for url, title, target, status, folder, error in rows:
            if folder and (self.out / folder / 'desktop.png').exists():
                rel = html.escape(folder.replace('\\', '/'), quote=True)
                cards.append(f'<article><a href="{rel}/offline.html"><img loading="lazy" src="{rel}/desktop.png">'
                             f'<h3>{html.escape(title)}</h3></a><p>{html.escape(status)}</p>'
                             f'<a href="{rel}/metadata.json">Metadata</a></article>')
        (self.out / 'index.html').write_text(
            '<!doctype html><meta charset="utf-8"><title>One Page Love archive</title>'
            '<style>body{font:16px system-ui;margin:32px;background:#f4f4f4}section{display:grid;'
            'grid-template-columns:repeat(auto-fill,minmax(260px,1fr));gap:20px}article{background:white;'
            'padding:12px;border-radius:12px}img{width:100%;height:220px;object-fit:cover;object-position:top}'
            'a{color:inherit}</style><h1>One Page Love frontend archive</h1><p>' + html.escape(json.dumps(counts))
            + '</p><p>Published frontend snapshots. Offline previews are static; original app source and full functionality are not implied.</p>'
            + '<section>' + ''.join(cards) + '</section>', encoding='utf-8')
        return report


def cards_from_page(raw, page_url):
    soup = BeautifulSoup(raw, 'html.parser')
    root = soup.select_one('#content-archive')
    if not root:
        raise RuntimeError('Unexpected gallery markup; stop instead of silently losing entries')
    cards = []
    for card in root.select('.thumb-inspiration'):
        name = card.select_one('.thumb-name a')
        if not name:
            continue
        launch = card.select_one('.thumb-link a')
        image = card.select_one('.thumb-image img')
        video = card.select_one('.thumb-image video')
        cards.append({'url': urljoin(page_url, name['href']), 'title': name.get_text(' ', strip=True),
                      'target': normalized(launch['href']) if launch else None,
                      'tags': [a.get_text(' ', strip=True) for a in card.select('.thumb-category a')],
                      'reference_image_url': (image.get('data-src') or image.get('src')) if image else
                                             (video.get('poster') if video else None),
                      'gallery_page': page_url, 'discovered_at': now()})
    next_link = soup.select_one('a.pagination__next')
    return cards, urljoin(page_url, next_link['href']) if next_link else None


async def catalog(store, robots, args, done):
    try:
        if store.setting('catalog_complete') == '1':
            return
        store.setting('catalog_error', '')
        url = store.setting('next_catalog_page') or ROOT + '/inspiration'
        count_pages = 0
        while url and not (store.out / 'STOP').exists():
            raw = await robots.get(url)
            records, next_url = cards_from_page(raw, url)
            if not records:
                raise RuntimeError('Empty gallery page: ' + url)
            slug = 'page-' + str(count_pages + 1) + '-' + hashlib.sha256(url.encode()).hexdigest()[:8]
            (store.out / 'directory' / (slug + '.html')).write_bytes(raw)
            for record in records:
                store.put(record)
            store.setting('last_catalog_page', url)
            store.setting('next_catalog_page', next_url or '')
            count_pages += 1
            if count_pages % 10 == 0:
                print(json.dumps({'event': 'catalog', 'page': url, 'entries': store.export()['discovered']}), flush=True)
            if not next_url:
                # Verify gallery coverage against the published posts sitemap.
                candidates = []
                for sitemap in ['post_part1.xml', 'post_part2.xml']:
                    xml = await robots.get(ROOT + '/' + sitemap)
                    (store.out / 'directory' / sitemap).write_bytes(xml)
                    candidates.extend(e.text for e in ET.fromstring(xml).iter()
                                      if e.tag.endswith('}loc') and e.text)
                known = {r[0] for r in store.db.execute('SELECT url FROM entries')}
                missing = [u for u in candidates if u not in known]
                atomic_json(store.out / 'sitemap_unlisted.json', missing)
                # Some public posts are templates/articles, not inspiration. Inspect to classify.
                for detail_url in missing:
                    if (store.out / 'STOP').exists():
                        return
                    try:
                        detail = await robots.get(detail_url)
                        soup = BeautifulSoup(detail, 'html.parser')
                        visit = next((a for a in soup.find_all('a', href=True)
                                      if a.get_text(' ', strip=True).startswith('Visit Website')), None)
                        if visit:
                            title = soup.select_one('h1')
                            store.put({'url': detail_url, 'title': title.get_text(' ', strip=True) if title else detail_url,
                                       'target': normalized(urljoin(detail_url, visit['href'])),
                                       'discovered_at': now(), 'sitemap_recovered': True})
                    except Exception as e:
                        with (store.out / 'catalog_errors.jsonl').open('a', encoding='utf-8') as f:
                            f.write(json.dumps({'url': detail_url, 'error': str(e), 'at': now()}) + '\n')
                store.setting('catalog_complete', '1')
                break
            if args.limit and store.db.execute('SELECT COUNT(*) FROM entries').fetchone()[0] >= args.limit:
                break
            url = next_url
    except Exception as e:
        store.setting('catalog_error', str(e))
        print(json.dumps({'event': 'catalog_error', 'error': str(e)}), flush=True)
    finally:
        done.set()


def offline_preview(folder, raw_dom, base_url, resources):
    mapping = {r['url']: r['file'] for r in resources if r.get('file') and r.get('status') == 200}

    def local(value, base):
        full = urljoin(base, value)
        return mapping.get(full, full)

    def css_urls(text, base, from_assets=False):
        def sub(match):
            value = match.group(1).strip(' \"\'')
            if value.startswith(('data:', '#')):
                return match.group(0)
            path = local(value, base)
            if from_assets and path.startswith('assets/'):
                path = path[7:]
            return 'url("' + path.replace('"', '%22') + '")'
        return re.sub(r'url\(\s*([^)]*?)\s*\)', sub, text, flags=re.I)

    for r in resources:
        if r.get('file') and 'text/css' in r.get('mime', ''):
            path = folder / r['file']
            original = path.read_text(encoding='utf-8', errors='replace')
            # Preserve delivered bytes; use a separate rewritten stylesheet for the preview.
            preview_path = path.with_name(path.stem + '.preview.css')
            preview_path.write_text(css_urls(original, r['url'], True), encoding='utf-8')
            mapping[r['url']] = str(preview_path.relative_to(folder)).replace('\\', '/')
    soup = BeautifulSoup(raw_dom, 'html.parser')
    for tag in soup.find_all(['script', 'base']):
        tag.decompose()
    for tag in soup.find_all(True):
        for attr in list(tag.attrs):
            if attr.lower().startswith('on') or attr in {'integrity', 'crossorigin'}:
                del tag.attrs[attr]
        if tag.name == 'meta' and tag.get('http-equiv', '').lower() in {'content-security-policy', 'refresh'}:
            tag.decompose()
            continue
        for attr in ['src', 'poster']:
            if tag.get(attr):
                tag[attr] = local(tag[attr], base_url)
        if tag.name == 'link' and tag.get('href'):
            tag['href'] = local(tag['href'], base_url)
            if 'modulepreload' in tag.get('rel', []):
                tag.decompose()
                continue
        if tag.get('srcset'):
            tag['srcset'] = ', '.join(' '.join([local(part.strip().split()[0], base_url)] + part.strip().split()[1:])
                                      for part in tag['srcset'].split(',') if part.strip())
        if tag.get('style'):
            tag['style'] = css_urls(tag['style'], base_url)
        if tag.name == 'style' and tag.string:
            tag.string.replace_with(css_urls(str(tag.string), base_url))
    (folder / 'offline.html').write_text(str(soup), encoding='utf-8')


async def capture(browser, record, robots, out):
    slug = re.sub(r'[^a-zA-Z0-9._-]+', '-', urlsplit(record['url']).path.strip('/'))[:90] or 'site'
    folder = out / 'sites' / (slug + '-' + hashlib.sha256(record['url'].encode()).hexdigest()[:8])
    (folder / 'assets').mkdir(parents=True, exist_ok=True)
    meta = dict(record, captured_at=now(), code_kind='published_frontend',
                original_project_source=False, resources=[], blocked=[], errors=[],
                limitations=['Only the listed landing page is captured.',
                             'Offline preview is a static DOM snapshot; original delivered JS is saved separately.',
                             'Media streams, login flows and interactions requiring backend services are not captured.'])
    target = record.get('target')
    if not target:
        detail = await robots.get(record['url'])
        detail_dir = out / 'directory' / 'details'
        detail_dir.mkdir(exist_ok=True)
        (detail_dir / (folder.name + '.html')).write_bytes(detail)
        soup = BeautifulSoup(detail, 'html.parser')
        heading = soup.select_one('h1')
        if heading:
            meta['title'] = heading.get_text(' ', strip=True)
        visit = next((a for a in soup.find_all('a', href=True)
                      if a.get_text(' ', strip=True).startswith('Visit Website')), None)
        if visit:
            target = normalized(urljoin(record['url'], visit['href']))
            meta['target'] = target
        else:
            meta['status'] = 'no_public_target'
            atomic_json(folder / 'metadata.json', meta)
            return meta, folder
    if not await robots.allowed(target):
        meta['status'] = 'robots_skipped'
        atomic_json(folder / 'metadata.json', meta)
        return meta, folder

    context = await browser.new_context(viewport={'width': 1440, 'height': 1000},
                                        device_scale_factor=1, user_agent=AGENT,
                                        service_workers='block', accept_downloads=False)
    total = 0
    resource_count = 0

    async def route_handler(route):
        nonlocal total, resource_count
        request = route.request
        url = request.url
        try:
            if request.method not in {'GET', 'HEAD'}:
                meta['blocked'].append({'url': url, 'reason': 'non_read_request'})
                await route.abort()
                return
            if request.resource_type in {'media', 'websocket'}:
                meta['blocked'].append({'url': url, 'reason': 'streaming_media'})
                await route.abort()
                return
            if total >= LIMIT_SITE or resource_count >= 500:
                meta['blocked'].append({'url': url, 'reason': 'site_budget'})
                await route.abort()
                return
            if not await robots.allowed(url):
                meta['blocked'].append({'url': url, 'reason': 'robots'})
                await route.abort()
                return
            await robots.pace(url, .06)
            response = await route.fetch(max_redirects=0, timeout=25000)
            if response.status in {429, 503}:
                p = urlsplit(url)
                robots.backoff_origins.add(f'{p.scheme}://{p.netloc}')
                meta['blocked'].append({'url': url, 'reason': f'http_{response.status}'})
                await route.abort()
                return
            mime = response.headers.get('content-type', '')
            if int(response.headers.get('content-length', '0') or 0) > LIMIT_FILE:
                meta['blocked'].append({'url': url, 'reason': 'file_budget'})
                await route.abort()
                return
            body = await response.body()
            if len(body) > LIMIT_FILE or total + len(body) > LIMIT_SITE:
                meta['blocked'].append({'url': url, 'reason': 'file_or_site_budget'})
                await route.abort()
                return
            resource_count += 1
            total += len(body)
            resource = {'url': url, 'status': response.status, 'mime': mime,
                        'type': request.resource_type, 'bytes': len(body)}
            if response.status == 200 and body:
                ext = mimetypes.guess_extension(mime.split(';')[0].strip()) or '.bin'
                if 'javascript' in mime:
                    ext = '.js'
                digest = hashlib.sha256(body).hexdigest()
                resource['file'] = 'assets/' + hashlib.sha256(url.encode()).hexdigest()[:24] + ext
                resource['sha256'] = digest
                (folder / resource['file']).write_bytes(body)
                if request.is_navigation_request() and request.frame == page.main_frame and 'html' in mime:
                    (folder / 'source.html').write_bytes(body)
            meta['resources'].append(resource)
            await route.fulfill(response=response, body=body)
        except Exception as e:
            meta['errors'].append({'url': url, 'error': str(e)[:400]})
            try:
                await route.abort()
            except Exception:
                pass

    page = await context.new_page()
    await context.route('**/*', route_handler)
    page.on('pageerror', lambda e: meta['errors'].append({'javascript': str(e)[:500]}))
    page.on('dialog', lambda dialog: dialog.dismiss())
    try:
        response = await page.goto(target, wait_until='domcontentloaded', timeout=45000)
        meta['final_url'] = page.url
        meta['http_status'] = response.status if response else None
        if not response or response.status >= 400:
            meta['status'] = 'http_failed'
            return meta, folder
        await page.wait_for_timeout(2000)
        await page.evaluate('''async () => {
          for (let i=0; i<16; i++) {
            window.scrollBy(0, Math.max(650, innerHeight));
            await new Promise(r => setTimeout(r, 120));
            if (scrollY + innerHeight >= document.documentElement.scrollHeight) break;
          }
          window.scrollTo(0,0);
        }''')
        await page.wait_for_timeout(1000)
        meta['page_title'] = await page.title()
        body_text = await page.locator('body').inner_text(timeout=8000)
        (folder / 'page_text.txt').write_text(body_text, encoding='utf-8')
        if any(t in (meta['page_title'] + '\n' + body_text[:2000]).lower()
               for t in ['just a moment', 'verify you are human', 'checking your browser', 'domain is for sale', 'buy this domain']):
            meta['status'] = 'blocked_or_parked'
            return meta, folder
        dom = await page.content()
        (folder / 'rendered.html').write_text(dom, encoding='utf-8')
        meta['dimensions'] = await page.evaluate('({width:innerWidth,height:document.documentElement.scrollHeight})')
        height = min(meta['dimensions']['height'], 32000)
        await page.screenshot(path=str(folder / 'desktop.png'),
                              clip={'x': 0, 'y': 0, 'width': 1440, 'height': max(1, height)},
                              timeout=20000, animations='disabled')
        await page.set_viewport_size({'width': 390, 'height': 844})
        await page.wait_for_timeout(1000)
        height = min(await page.evaluate('document.documentElement.scrollHeight'), 32000)
        await page.screenshot(path=str(folder / 'mobile.png'),
                              clip={'x': 0, 'y': 0, 'width': 390, 'height': max(1, height)},
                              timeout=20000, animations='disabled')
        meta['status'] = 'captured_partial' if meta['blocked'] or meta['errors'] else 'captured'
    except Exception as e:
        meta['status'] = 'failed'
        meta['error'] = str(e)[:1500]
    finally:
        await context.close()
        meta['bytes_received'] = total
        meta['resource_count'] = resource_count
        if (folder / 'rendered.html').exists():
            try:
                offline_preview(folder, (folder / 'rendered.html').read_text(encoding='utf-8'),
                                meta.get('final_url', target), meta['resources'])
            except Exception as e:
                meta['preview_error'] = str(e)
        atomic_json(folder / 'metadata.json', meta)
    return meta, folder


async def main(args):
    out = Path(args.output).resolve()
    out.mkdir(parents=True, exist_ok=True)
    for name in ['sites', 'directory']:
        (out / name).mkdir(exist_ok=True)
    lock_path = out / 'crawl.lock'
    if lock_path.exists():
        raise RuntimeError('crawl.lock exists: check that no crawler is running before removing it')
    lock_path.write_text(str(os.getpid()), encoding='utf-8')
    store = Store(out)
    stop_event, done = asyncio.Event(), asyncio.Event()
    jobs = []
    connector = aiohttp.TCPConnector(limit=40, limit_per_host=8)
    try:
        async with aiohttp.ClientSession(connector=connector, headers={'User-Agent': AGENT}) as session:
            robots = Robots(session, out)
            # The two public post sitemaps provide a concrete coverage inventory immediately.
            if not (out / 'sitemap_candidates.json').exists():
                candidates = []
                for name in ['post_part1.xml', 'post_part2.xml']:
                    xml = await robots.get(ROOT + '/' + name)
                    (out / 'directory' / name).write_bytes(xml)
                    candidates.extend(e.text for e in ET.fromstring(xml).iter() if e.tag.endswith('}loc') and e.text)
                atomic_json(out / 'sitemap_candidates.json', candidates)
            candidates = json.loads((out / 'sitemap_candidates.json').read_text(encoding='utf-8'))
            store.setting('sitemap_candidate_count', len(candidates))
            known = {r[0] for r in store.db.execute('SELECT url FROM entries')}
            for candidate in candidates:
                if candidate not in known:
                    store.put({'url': candidate, 'title': urlsplit(candidate).path.strip('/'),
                               'target': None, 'entry_kind': 'sitemap_candidate', 'discovered_at': now()},
                              commit=False)
            store.db.commit()
            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=True)
                completed, claimed = 0, 0
                store.phase = 'catalog_and_capture'

                async def worker():
                    nonlocal completed, claimed
                    while not stop_event.is_set():
                        if (out / 'STOP').exists():
                            stop_event.set()
                            return
                        if shutil.disk_usage(out).free < 10 * 1024**3:
                            store.setting('stop_reason', 'less_than_10GB_free')
                            stop_event.set()
                            return
                        if args.limit and claimed >= args.limit:
                            return
                        record = store.pending()
                        if not record:
                            if done.is_set():
                                return
                            await asyncio.sleep(1)
                            continue
                        store.status(record['url'], 'running')
                        claimed += 1
                        folder = None
                        try:
                            async with asyncio.timeout(130):
                                meta, folder = await capture(browser, record, robots, out)
                            record['title'] = meta['title']
                            record['target'] = meta.get('target', record.get('target'))
                            store.put(record)
                            store.status(record['url'], meta['status'], str(folder.relative_to(out)), meta.get('error'))
                        except Exception as e:
                            store.status(record['url'], 'failed', str(folder.relative_to(out)) if folder else None,
                                         str(e) or type(e).__name__)
                        completed += 1
                        row = store.db.execute('SELECT status FROM entries WHERE url=?', (record['url'],)).fetchone()
                        print(json.dumps({'event': 'capture', 'title': record['title'], 'status': row[0],
                                          'completed_this_run': completed}, ensure_ascii=False), flush=True)

                async def heartbeat():
                    while not stop_event.is_set():
                        store.export()
                        await asyncio.sleep(10)

                producer = asyncio.create_task(catalog(store, robots, args, done))
                reporter = asyncio.create_task(heartbeat())
                jobs = [asyncio.create_task(worker()) for _ in range(args.workers)]
                await asyncio.gather(*jobs)
                if not producer.done():
                    producer.cancel()
                await asyncio.gather(producer, return_exceptions=True)
                stop_event.set()
                reporter.cancel()
                await asyncio.gather(reporter, return_exceptions=True)
                await browser.close()
                if (out / 'STOP').exists() or store.setting('stop_reason'):
                    store.phase = 'stopped'
                elif args.limit:
                    store.phase = 'sample_complete'
                elif store.setting('catalog_error'):
                    store.phase = 'catalog_failed'
                else:
                    store.phase = 'complete'
                print(json.dumps(store.export(), ensure_ascii=False), flush=True)
    finally:
        store.export()
        store.db.close()
        lock_path.unlink(missing_ok=True)


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', default='data/onepagelove')
    parser.add_argument('--workers', type=int, default=4)
    parser.add_argument('--limit', type=int, default=0, help='0 means all publicly listed inspiration pages')
    args = parser.parse_args()
    asyncio.run(main(args))

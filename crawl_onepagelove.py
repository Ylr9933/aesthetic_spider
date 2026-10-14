"""Resumable One Page Love directory + published frontend archive.

Captures public landing pages and delivered assets, not original app source.
HTTP only: no browser, rendering, JavaScript execution, or screenshots.
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


class FetchError(RuntimeError):
    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


class Robots:
    def __init__(self, session, out, connections=384):
        self.session, self.out = session, out
        self.rules, self.locks, self.last = {}, {}, {}
        self.backoff_origins = set()
        self.requests = asyncio.Semaphore(connections)
        (out / 'robots').mkdir(exist_ok=True)

    async def rule(self, url):
        p = urlsplit(url)
        origin = f'{p.scheme}://{p.netloc}'
        lock = self.locks.setdefault(origin + ':robots', asyncio.Lock())
        async with lock:
            if origin in self.rules:
                return self.rules[origin]
            parser = urllib.robotparser.RobotFileParser(origin + '/robots.txt')
            report_path = self.out / 'robots' / (hashlib.sha256(origin.encode()).hexdigest()[:20] + '.json')
            # Only reuse recently confirmed rules, never failed/denied fetches.
            if report_path.exists():
                try:
                    cached = json.loads(report_path.read_text(encoding='utf-8'))
                    age = (datetime.now(timezone.utc) - datetime.fromisoformat(cached['checked_at'])).total_seconds()
                    if cached.get('origin') == origin and 0 <= age < 3600 and not cached.get('error'):
                        if cached.get('status') == 200 and isinstance(cached.get('text'), str):
                            parser.parse(cached['text'].splitlines())
                            self.rules[origin] = parser
                            return parser
                        if cached.get('status') in {404, 410}:
                            parser.parse([])
                            self.rules[origin] = parser
                            return parser
                except (ValueError, KeyError, TypeError, OSError):
                    pass
            result = {'origin': origin, 'checked_at': now()}
            for attempt in range(2):
                try:
                    robots_url = origin + '/robots.txt'
                    for hop in range(5):
                        async with self.requests, self.session.get(robots_url, allow_redirects=False,
                                                timeout=aiohttp.ClientTimeout(total=15)) as redirect_response:
                            if redirect_response.status in {301, 302, 303, 307, 308}:
                                robots_url = urljoin(robots_url, redirect_response.headers['Location'])
                                if not public_url(robots_url):
                                    raise FetchError('non_public_robots_redirect')
                                continue
                            result['status'] = redirect_response.status
                            if redirect_response.status == 200:
                                chunks, size = [], 0
                                async for chunk in redirect_response.content.iter_chunked(65536):
                                    size += len(chunk)
                                    if size > 512 * 1024:
                                        raise FetchError('robots response too large')
                                    chunks.append(chunk)
                                result['text'] = b''.join(chunks).decode('utf-8', 'replace')
                                parser.parse(result['text'].splitlines())
                            elif redirect_response.status in {404, 410}:
                                parser.parse([])
                            else:
                                parser.disallow_all = True
                                result['error'] = 'robots unavailable; skipped conservatively'
                            break
                    else:
                        raise FetchError('too many robots redirects')
                    break
                except Exception as e:
                    if attempt == 0 and isinstance(e, (aiohttp.ClientError, TimeoutError)):
                        await asyncio.sleep(.5)
                        continue
                    parser.disallow_all = True
                    result['error'] = str(e)[:500] or type(e).__name__
            atomic_json(report_path, result)
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

    async def fetch(self, url, max_bytes=LIMIT_FILE, minimum=.06, destination=None, budget=None, timeout=25):
        requested = url
        for hop in range(8):
            if budget is not None and budget['bytes'] >= LIMIT_SITE:
                raise FetchError('site_budget: ' + url)
            if not await self.allowed(url):
                raise FetchError('robots_disallowed_or_unavailable: ' + url)
            # All directory, detail and resource requests share the same origin limits.
            await self.pace(url, max(minimum, .8) if urlsplit(url).hostname == 'onepagelove.com' else minimum)
            async with self.requests, self.session.get(url, allow_redirects=False,
                                        timeout=aiohttp.ClientTimeout(total=timeout, connect=10)) as r:
                if r.status in {301, 302, 303, 307, 308}:
                    url = urljoin(url, r.headers['Location'])
                    continue
                if r.status in {429, 503}:
                    p = urlsplit(url)
                    self.backoff_origins.add(f'{p.scheme}://{p.netloc}')
                    raise FetchError(f'origin_backoff_required: HTTP {r.status}: {url}', r.status)
                if r.status >= 400:
                    raise FetchError(f'HTTP {r.status}: {url}', r.status)
                if int(r.headers.get('content-length', '0') or 0) > max_bytes:
                    raise FetchError('file_budget: ' + url)
                chunks, size = [], 0
                handle = destination.open('wb') if destination else None
                try:
                    async for chunk in r.content.iter_chunked(65536):
                        size += len(chunk)
                        if size > max_bytes:
                            raise FetchError('file_budget: ' + url)
                        if budget is not None:
                            if budget['bytes'] + len(chunk) > LIMIT_SITE:
                                raise FetchError('site_budget: ' + url)
                            budget['bytes'] += len(chunk)
                        if handle:
                            handle.write(chunk)
                        else:
                            chunks.append(chunk)
                except BaseException:
                    if handle:
                        handle.close()
                        destination.unlink(missing_ok=True)
                    raise
                finally:
                    if handle:
                        handle.close()
                return {'url': requested, 'final_url': str(r.url), 'mime': r.headers.get('content-type', ''),
                        'charset': r.charset or 'utf-8', 'status': r.status, 'bytes': size,
                        'body': b''.join(chunks) if not destination else None}
        raise FetchError('too many redirects: ' + requested)

    async def get(self, url, max_bytes=4 * 1024 * 1024, minimum=.8):
        return (await self.fetch(url, max_bytes, minimum))['body']


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
        # A directory page may be discovered while this record is being downloaded.
        old = self.db.execute('SELECT data FROM entries WHERE url=?', (record['url'],)).fetchone()
        if old:
            previous = json.loads(old[0])
            record = {**previous, **record, 'target': record.get('target') or previous.get('target')}
        if record.get('target'):
            self.db.execute("UPDATE entries SET status='pending',error=NULL WHERE url=? AND status='no_public_target'",
                            (record['url'],))
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
        row = self.db.execute("SELECT data FROM entries WHERE status='pending' ORDER BY (target IS NULL OR target=''),rowid LIMIT 1").fetchone()
        return json.loads(row[0]) if row else None

    def export(self):
        counts = dict(self.db.execute('SELECT status,COUNT(*) FROM entries GROUP BY status'))
        report = {'started_at': self.started, 'updated_at': now(), 'phase': self.phase,
                  'pid': os.getpid(), 'discovered': sum(counts.values()), 'counts': counts,
                  'last_catalog_page': self.setting('last_catalog_page'),
                  'catalog_complete': self.setting('catalog_complete') == '1',
                  'sitemap_candidate_count': self.setting('sitemap_candidate_count'),
                  'mode': 'http_only', 'browser': False, 'screenshots': False,
                  'workers': self.setting('workers'), 'connections': self.setting('connections'),
                  'asset_workers_per_site': self.setting('asset_workers'),
                  'completed_this_run': getattr(self, 'completed', 0),
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
        manifest = []
        for url, title, target, status, folder, error in rows:
            if folder and (self.out / folder / 'source.html').exists():
                if status in {'captured', 'captured_partial', 'downloaded', 'downloaded_partial'}:
                    manifest.append([title, target, status, str((self.out / folder / 'source.html').resolve()),
                                     str((self.out / folder / 'assets').resolve()),
                                     str((self.out / folder / 'metadata.json').resolve()), url])
                rel = html.escape(folder.replace('\\', '/'), quote=True)
                thumbnail = f'<img loading="lazy" src="{rel}/desktop.png">' if (self.out / folder / 'desktop.png').exists() else ''
                preview = 'offline.html' if (self.out / folder / 'offline.html').exists() else 'source.html'
                cards.append(f'<article><a href="{rel}/{preview}">{thumbnail}'
                             f'<h3>{html.escape(title)}</h3></a><p>{html.escape(status)}</p>'
                             f'<a href="{rel}/metadata.json">Metadata</a></article>')
        with (self.out / 'sites_manifest.csv').open('w', encoding='utf-8-sig', newline='') as f:
            writer = csv.writer(f)
            writer.writerow(['title', 'target_url', 'status', 'local_html', 'local_assets', 'metadata', 'opl_entry_url'])
            writer.writerows(manifest)
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


CSS_URL = re.compile(r'''url\(\s*(?:"([^"]*)"|'([^']*)'|([^)]*?))\s*\)''', re.I)
CSS_IMPORT = re.compile(r'''@import\s+["']([^"']+)["']''', re.I)
JS_IMPORT = re.compile(r'''(?:\b(?:import|export)\s+(?:[^;\n]*?\s+from\s*)?|\bimport\s*\(\s*)["']([^"']+)["']''')
JS_URL = re.compile(r'''new\s+URL\(\s*["']([^"']+)["']\s*,\s*import\.meta\.url''')
MEDIA_EXT = {'.mp4', '.webm', '.mp3', '.m3u8', '.ogg', '.wav', '.mov', '.avi'}


def css_references(text):
    # Remove comments first; preserve original downloaded bytes separately.
    text = re.sub(r'/\*.*?\*/', '', text, flags=re.S)
    yield from CSS_IMPORT.findall(text)
    for match in CSS_URL.finditer(text):
        yield next((part for part in match.groups() if part is not None), '').strip()


def html_references(soup, base):
    base_tag = soup.find('base', href=True)
    if base_tag:
        base = urljoin(base, base_tag['href'])
    for tag in soup.find_all(True):
        if tag.name in {'script', 'img', 'source', 'input', 'iframe', 'audio', 'video'}:
            for attribute in ('src', 'data-src', 'data-lazy-src', 'data-original', 'poster'):
                if tag.get(attribute):
                    if tag.name in {'audio', 'video', 'source'} and attribute == 'src' and (
                            tag.name != 'source' or tag.get('type', '').startswith(('audio/', 'video/'))):
                        continue
                    yield urljoin(base, tag[attribute])
            for attribute in ('srcset', 'data-srcset'):
                for part in tag.get(attribute, '').split(','):
                    candidate = part.strip().split()
                    if candidate and not candidate[0].startswith('data:'):
                        yield urljoin(base, candidate[0])
        if tag.name == 'link' and tag.get('href'):
            rel = set(tag.get('rel', []))
            if rel & {'stylesheet', 'icon', 'apple-touch-icon', 'preload', 'modulepreload', 'manifest'}:
                if tag.get('as') not in {'video', 'audio'}:
                    yield urljoin(base, tag['href'])
        if tag.get('style'):
            for value in css_references(tag['style']):
                yield urljoin(base, value)
        if tag.name == 'style':
            for value in css_references(tag.get_text()):
                yield urljoin(base, value)
        if tag.name == 'script' and not tag.get('src'):
            for value in js_references(tag.get_text()):
                yield urljoin(base, value)


def js_references(text):
    for value in list(JS_IMPORT.findall(text)) + list(JS_URL.findall(text)):
        # Bare package specifiers require a resolver/build pipeline and aren't URLs.
        if value.startswith(('./', '../', '/', 'https://', 'http://')):
            yield value


async def capture(record, robots, out, args):
    started = time.monotonic()
    slug = re.sub(r'[^a-zA-Z0-9._-]+', '-', urlsplit(record['url']).path.strip('/'))[:90] or 'site'
    folder = out / 'sites' / (slug + '-' + hashlib.sha256(record['url'].encode()).hexdigest()[:8])
    (folder / 'assets').mkdir(parents=True, exist_ok=True)
    meta = dict(record, captured_at=now(), code_kind='published_frontend', mode='http_only',
                original_project_source=False, rendered=False, screenshots=False,
                resources=[], blocked=[], errors=[], limitations=[
                    'Only the listed landing page and statically discoverable referenced assets are downloaded.',
                    'JavaScript is saved but never executed. Runtime-generated content, fetches and dynamic assets may be missing.',
                    'No browser, rendering or screenshots. No claim of visual or functional validation.',
                    'Media streams, logins, backend interactions and original private project source are not captured.',
                    'JS/CSS reference parsing is best effort; bundler runtime chunk maps and import maps are not fully resolved.'])
    tasks = []
    budget = {'bytes': 0}
    target = record.get('target')
    try:
        if not target:
            detail_dir = out / 'directory' / 'details'
            detail_dir.mkdir(exist_ok=True)
            detail_path = detail_dir / (folder.name + '.html')
            # Reuse public detail HTML already saved by previous runs.
            detail = detail_path.read_bytes() if detail_path.exists() else await robots.get(record['url'])
            if not detail_path.exists():
                detail_path.write_bytes(detail)
            soup = BeautifulSoup(detail, 'html.parser')
            heading = soup.select_one('h1')
            if heading:
                meta['title'] = heading.get_text(' ', strip=True)
            visit = next((a for a in soup.find_all('a', href=True)
                          if a.get_text(' ', strip=True).startswith(('Visit Website', 'Launch Website'))), None)
            if not visit:
                meta['status'] = 'no_public_target'
                return meta, folder
            target = normalized(urljoin(record['url'], visit['href']))
            meta['target'] = target
        if not await robots.allowed(target):
            meta['status'] = 'robots_skipped'
            return meta, folder
        page = await robots.fetch(target, max_bytes=4 * 1024**2, budget=budget)
        meta['final_url'], meta['http_status'] = page['final_url'], page['status']
        (folder / 'source.html').write_bytes(page['body'])
        soup = BeautifulSoup(page['body'], 'html.parser')
        meta['page_title'] = soup.title.get_text(' ', strip=True) if soup.title else ''
        text = soup.get_text(' ', strip=True)
        (folder / 'page_text.txt').write_text(text, encoding='utf-8')
        if any(value in (meta['page_title'] + '\n' + text[:2000]).lower() for value in (
                'just a moment', 'verify you are human', 'checking your browser', 'domain is for sale', 'buy this domain')):
            meta['status'] = 'blocked_or_parked'
            return meta, folder
        if page['mime'] and not any(t in page['mime'] for t in ('html', 'text/plain')):
            meta['status'] = 'non_html'
            return meta, folder
        base = page['final_url']
        queue = asyncio.Queue()
        seen = {target, base}

        def enqueue(url, depth=0):
            full = urljoin(base, url)
            parsed = urlsplit(full)
            full = urlunsplit((parsed.scheme, parsed.netloc, parsed.path, parsed.query, ''))
            if full in seen or not public_url(full):
                return
            seen.add(full)
            if Path(parsed.path).suffix.lower() in MEDIA_EXT:
                meta['blocked'].append({'url': full, 'reason': 'streaming_media'})
                return
            if len(seen) > args.max_resources + 2 or depth > 8:
                meta['blocked'].append({'url': full, 'reason': 'resource_or_depth_budget'})
                return
            queue.put_nowait((full, depth))

        for ref in html_references(soup, base):
            enqueue(ref)

        async def asset_worker():
            while True:
                url, depth = await queue.get()
                temp = folder / 'assets' / (hashlib.sha256(url.encode()).hexdigest()[:24] + '.part')
                try:
                    result = await robots.fetch(url, destination=temp, budget=budget)
                    mime = result['mime'].split(';')[0].strip().lower()
                    url_ext = Path(urlsplit(result['final_url']).path).suffix
                    ext = url_ext if mime in {'', 'application/octet-stream', 'text/plain'} else mimetypes.guess_extension(mime)
                    ext = ext or url_ext or '.bin'
                    if len(ext) > 10 or not re.fullmatch(r'\.[a-zA-Z0-9]+', ext):
                        ext = '.bin'
                    if 'javascript' in mime:
                        ext = '.js'
                    path = temp.with_suffix(ext)
                    temp.replace(path)
                    # Hash in bounded chunks; never hold multiple large binary assets in RAM.
                    digest = hashlib.sha256()
                    with path.open('rb') as handle:
                        for chunk in iter(lambda: handle.read(65536), b''):
                            digest.update(chunk)
                    resource = {k: result[k] for k in ('url', 'final_url', 'status', 'mime', 'bytes')}
                    resource.update(file=path.relative_to(folder).as_posix(), sha256=digest.hexdigest())
                    meta['resources'].append(resource)
                    asset_text = None
                    if 'css' in mime or ext == '.css':
                        asset_text = path.read_text(encoding=result['charset'], errors='replace')
                        refs = css_references(asset_text)
                    elif 'javascript' in mime or ext in {'.js', '.mjs'}:
                        asset_text = path.read_text(encoding=result['charset'], errors='replace')
                        refs = js_references(asset_text)
                    elif 'html' in mime and depth < 2:
                        refs = html_references(BeautifulSoup(path.read_bytes(), 'html.parser'), result['final_url'])
                    else:
                        refs = ()
                    for ref in refs:
                        enqueue(urljoin(result['final_url'], ref), depth + 1)
                except Exception as e:
                    message = str(e) or type(e).__name__
                    if any(t in message for t in ('robots_', 'budget', 'backoff_')):
                        meta['blocked'].append({'url': url, 'reason': message[:400]})
                    else:
                        meta['errors'].append({'url': url, 'error': message[:400]})
                finally:
                    temp.unlink(missing_ok=True)
                    queue.task_done()

        tasks = [asyncio.create_task(asset_worker()) for _ in range(args.asset_workers)]
        await queue.join()
        meta['status'] = 'downloaded_partial' if meta['blocked'] or meta['errors'] else 'downloaded'
    except FetchError as e:
        meta['status'] = 'robots_skipped' if 'robots_' in str(e) else 'http_failed' if e.status else 'failed'
        meta['error'] = str(e)
        meta['http_status'] = e.status
    except Exception as e:
        meta['status'], meta['error'] = 'failed', str(e) or type(e).__name__
    except asyncio.CancelledError:
        meta['status'], meta['error'] = 'failed', 'site_timeout_or_interrupted'
        raise
    finally:
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        meta['bytes_received'] = budget['bytes']
        meta['resource_count'] = len(meta['resources'])
        meta['elapsed_seconds'] = round(time.monotonic() - started, 3)
        atomic_json(folder / 'metadata.json', meta)
    return meta, folder


async def main(args):
    out = Path(args.output).resolve()
    out.mkdir(parents=True, exist_ok=True)
    for name in ['sites', 'directory']:
        (out / name).mkdir(exist_ok=True)
    lock_path = out / 'crawl.lock'
    try:
        with lock_path.open('x', encoding='utf-8') as lock:
            lock.write(str(os.getpid()))
    except FileExistsError:
        raise RuntimeError('crawl.lock exists: check that no crawler is running before removing it') from None
    store = Store(out)
    stop_event, done = asyncio.Event(), asyncio.Event()
    jobs = []
    connector = aiohttp.TCPConnector(limit=args.connections, limit_per_host=args.per_host, ttl_dns_cache=300)
    try:
        async with aiohttp.ClientSession(connector=connector, headers={'User-Agent': AGENT}) as session:
            robots = Robots(session, out, args.connections)
            store.setting('workers', args.workers)
            store.setting('connections', args.connections)
            store.setting('asset_workers', args.asset_workers)
            store.setting('stop_reason', '')
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
            if args.retry_failed:
                store.db.execute("UPDATE entries SET status='pending',error=NULL WHERE status IN ('failed','http_failed')")
                store.db.commit()
            completed, claimed = 0, 0
            store.phase = 'catalog_and_download'

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
                        async with asyncio.timeout(args.site_timeout):
                            meta, folder = await capture(record, robots, out, args)
                        record['title'] = meta['title']
                        record['target'] = meta.get('target', record.get('target'))
                        store.put(record)
                        store.status(record['url'], meta['status'], str(folder.relative_to(out)), meta.get('error'))
                    except Exception as e:
                        # capture writes its metadata even on timeout; preserve its folder/target.
                        slug = re.sub(r'[^a-zA-Z0-9._-]+', '-', urlsplit(record['url']).path.strip('/'))[:90] or 'site'
                        folder = out / 'sites' / (slug + '-' + hashlib.sha256(record['url'].encode()).hexdigest()[:8])
                        meta_path = folder / 'metadata.json'
                        if meta_path.exists():
                            interrupted = json.loads(meta_path.read_text(encoding='utf-8'))
                            record['title'] = interrupted.get('title', record['title'])
                            record['target'] = interrupted.get('target', record.get('target'))
                            store.put(record)
                        store.status(record['url'], 'failed', str(folder.relative_to(out)) if folder else None,
                                     str(e) or type(e).__name__)
                    completed += 1
                    store.completed = completed
                    row = store.db.execute('SELECT status FROM entries WHERE url=?', (record['url'],)).fetchone()
                    print(json.dumps({'event': 'download', 'title': record['title'], 'status': row[0],
                                      'completed_this_run': completed}, ensure_ascii=False), flush=True)

            async def heartbeat():
                while not stop_event.is_set():
                    store.export()
                    await asyncio.sleep(10)

            producer = asyncio.create_task(catalog(store, robots, args, done))
            reporter = asyncio.create_task(heartbeat())
            jobs = [asyncio.create_task(worker()) for _ in range(args.workers)]
            try:
                await asyncio.gather(*jobs)
            finally:
                for job in jobs:
                    if not job.done():
                        job.cancel()
                await asyncio.gather(*jobs, return_exceptions=True)
            if not producer.done():
                producer.cancel()
            await asyncio.gather(producer, return_exceptions=True)
            stop_event.set()
            reporter.cancel()
            await asyncio.gather(reporter, return_exceptions=True)
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
        for task in (locals().get('producer'), locals().get('reporter')):
            if task and not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        store.export()
        store.db.close()
        lock_path.unlink(missing_ok=True)


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', default='data/onepagelove')
    parser.add_argument('--workers', type=int, default=96, help='Concurrent website downloads; no browser')
    parser.add_argument('--connections', type=int, default=384, help='Global HTTP connection/request limit')
    parser.add_argument('--per-host', type=int, default=4, help='Connections per endpoint')
    parser.add_argument('--asset-workers', type=int, default=8, help='Asset downloads per website')
    parser.add_argument('--max-resources', type=int, default=500)
    parser.add_argument('--site-timeout', type=int, default=120)
    parser.add_argument('--retry-failed', action='store_true', help='Retry previous failed/http_failed entries once in this run')
    parser.add_argument('--limit', type=int, default=0, help='0 means all publicly listed inspiration pages')
    args = parser.parse_args()
    if min(args.workers, args.connections, args.per_host, args.asset_workers, args.max_resources, args.site_timeout) < 1 or args.limit < 0:
        parser.error('Concurrency, budget and timeout must be positive; limit must be nonnegative')
    asyncio.run(main(args))

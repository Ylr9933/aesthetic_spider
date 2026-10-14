"""HTTP integration tests; local fixtures only, no browser or external requests."""
import asyncio
import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import aiohttp
from aiohttp import web

import crawl_onepagelove as crawler


class HttpArchiveTests(unittest.IsolatedAsyncioTestCase):
    async def test_valid_robots_cache_avoids_second_http_request(self):
        await self.robots.rule(self.base+'/first')
        before=len(self.paths)
        again=crawler.Robots(self.session,self.out,connections=2)
        self.assertTrue(await again.allowed(self.base+'/second'))
        self.assertFalse(await again.allowed(self.base+'/private/example'))
        self.assertEqual(len(self.paths),before)

    async def test_failed_robots_cache_is_rechecked(self):
        import hashlib
        report=self.out/'robots'/(hashlib.sha256(self.base.encode()).hexdigest()[:20]+'.json')
        crawler.atomic_json(report,{'origin':self.base,'checked_at':crawler.now(),'status':403,'error':'failed'})
        self.assertTrue(await self.robots.allowed(self.base+'/public'))
        self.assertIn('/robots.txt',self.paths)

    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.out = Path(self.temp.name)
        (self.out / 'sites').mkdir()
        (self.out / 'directory').mkdir()
        self.paths = []
        self.active = self.peak = 0

        async def endpoint(request):
            self.paths.append(request.path)
            if request.path == '/robots.txt':
                return web.Response(text='User-agent: *\nDisallow: /private\n')
            if request.path == '/entry':
                raise web.HTTPFound('/nested/index.html')
            if request.path == '/nested/index.html':
                return web.Response(text='''<!doctype html><title>Fixture</title><base href="/static/">
                    <link rel="stylesheet" href="main.css"><script type="module" src="app.js"></script>
                    <img src="logo.png" srcset="small.png 1x, big.png 2x" data-src="lazy.png">
                    <img src="/private/secret.png"><video src="movie.mp4" poster="poster.png"></video>
                    <div style="background:url(inline.png)">Hello</div>''', content_type='text/html')
            if request.path == '/static/main.css':
                return web.Response(text='@import "nested/extra.css"; .x{background:url("bg.png")}', content_type='text/css')
            if request.path == '/static/nested/extra.css':
                return web.Response(text='@import "../main.css"; @font-face{src:url("../font.woff2")}', content_type='text/css')
            if request.path == '/static/app.js':
                return web.Response(text='import "./chunk.js"; import("./dynamic.js"); new URL("./data.json", import.meta.url); import "react";', content_type='text/javascript')
            if request.path == '/limit':
                return web.Response(body=b'x' * 32)
            if request.path.startswith('/slow'):
                self.active += 1
                self.peak = max(self.peak, self.active)
                try:
                    await asyncio.sleep(.08)
                    return web.Response(body=b'ok')
                finally:
                    self.active -= 1
            return web.Response(body=b'fixture-resource', content_type='application/octet-stream')

        app = web.Application()
        app.router.add_route('GET', '/{tail:.*}', endpoint)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, '127.0.0.1', 0)
        await site.start()
        self.base = 'http://127.0.0.1:' + str(site._server.sockets[0].getsockname()[1])
        # Only tests permit local fixture addresses. Production keeps its public URL check.
        self.url_patch = patch.object(crawler, 'public_url', lambda u: u.startswith(self.base))
        self.url_patch.start()
        self.session = aiohttp.ClientSession()
        self.robots = crawler.Robots(self.session, self.out, connections=2)
        self.io_pool=ThreadPoolExecutor(max_workers=2)
        self.robots.io_pool=self.io_pool
        self.args = SimpleNamespace(asset_workers=4, max_resources=40)

    async def asyncTearDown(self):
        await asyncio.to_thread(self.io_pool.shutdown,wait=True,cancel_futures=True)
        await self.session.close()
        await self.runner.cleanup()
        self.url_patch.stop()
        self.temp.cleanup()

    async def test_redirect_css_modules_lazy_images_and_no_screenshots(self):
        meta, folder = await crawler.capture({'url': 'https://onepagelove.com/fixture', 'title': 'Fixture',
                                              'target': self.base + '/entry'}, self.robots, self.out, self.args)
        self.assertEqual(meta['status'], 'downloaded_partial')  # denied resource is recorded
        urls = {r['url'] for r in meta['resources']}
        for path in ('/static/nested/extra.css', '/static/font.woff2', '/static/chunk.js',
                     '/static/dynamic.js', '/static/data.json', '/static/lazy.png', '/static/big.png'):
            self.assertIn(self.base + path, urls)
        self.assertNotIn('/private/secret.png', self.paths)
        self.assertNotIn('/static/movie.mp4', self.paths)
        self.assertEqual(self.paths.count('/static/main.css'), 1)  # cyclic CSS import deduplicated
        self.assertIn(b'<base href="/static/">', (folder / 'source.html').read_bytes())
        self.assertFalse(list(folder.glob('*.png')))
        self.assertFalse((folder / 'rendered.html').exists())
        self.assertFalse(meta['rendered'])
        for resource in meta['resources']:
            self.assertTrue((folder / resource['file']).is_file())
        self.assertFalse(list((folder / 'assets').glob('*.part')))

    async def test_stream_budget_removes_partial_files(self):
        part = self.out / 'budget.part'
        with patch.object(crawler, 'LIMIT_SITE', 8):
            with self.assertRaisesRegex(crawler.FetchError, 'site_budget'):
                await self.robots.fetch(self.base + '/limit', destination=part, budget={'bytes': 0})
        self.assertFalse(part.exists())

    async def test_global_request_concurrency_bound(self):
        await asyncio.gather(*(self.robots.get(self.base + f'/slow{i}', minimum=0) for i in range(8)))
        self.assertLessEqual(self.peak, 2)
        self.assertGreater(self.peak, 1)

    async def test_cancellation_leaves_metadata_and_no_tasks(self):
        original = self.robots.fetch

        async def slow_asset(url, **kwargs):
            if url.endswith('main.css'):
                await asyncio.Event().wait()
            return await original(url, **kwargs)

        with patch.object(self.robots, 'fetch', slow_asset):
            with self.assertRaises(TimeoutError):
                async with asyncio.timeout(.15):
                    await crawler.capture({'url': 'https://onepagelove.com/timeout', 'title': 'Timeout',
                                           'target': self.base + '/entry'}, self.robots, self.out, self.args)
        meta_file = next((self.out / 'sites').glob('*/metadata.json'))
        self.assertEqual(json.loads(meta_file.read_text())['error'], 'site_timeout_or_interrupted')
        self.assertFalse(list(self.out.rglob('*.part')))

    async def test_resume_and_catalog_enrichment_preserve_target(self):
        store = crawler.Store(self.out)
        try:
            record = {'url': 'https://onepagelove.com/test', 'title': 'Test', 'target': self.base + '/entry'}
            store.put(record)
            store.status(record['url'], 'downloaded', 'sites/test')
            store.put({**record, 'target': None, 'tags': ['Portfolio']})
            self.assertIsNone(store.pending())
            self.assertEqual(store.db.execute('SELECT target FROM entries').fetchone()[0], record['target'])
            folder = self.out / 'sites' / 'test'
            folder.mkdir()
            (folder / 'source.html').write_text('<title>Target website</title>')
            store.export()
            manifest = (self.out / 'sites_manifest.csv').read_text(encoding='utf-8-sig')
            self.assertIn(record['target'], manifest)
            self.assertIn(str((folder / 'source.html').resolve()), manifest)
            self.assertIn('Target website', (folder / 'source.html').read_text())
            store.status(record['url'], 'no_public_target')
            store.put(record)
            self.assertEqual(store.pending()['target'], record['target'])
            store.status(record['url'], 'running')
        finally:
            store.db.close()
        store = crawler.Store(self.out)
        try:
            self.assertIsNotNone(store.pending())
        finally:
            store.db.close()


if __name__ == '__main__':
    unittest.main()

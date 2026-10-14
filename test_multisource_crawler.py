"""Meaningful extraction, deduplication, archive and pause regressions."""
import asyncio
import json
import tempfile
import threading
import unittest
import zipfile
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import crawl_frontend_sources as multi
from crawl_onepagelove import Robots, analyze_page
from source_adapters import extract, extract_payload, decode_nuxt


class AdapterTests(unittest.TestCase):
    def test_gallery_target_not_gallery_footer_or_purchase(self):
        source={'id':'framer','url':'https://www.framer.com/marketplace/','kind':'marketplace'}
        raw='''<h1>Example</h1><a href="https://demo.example/">Show Preview</a>
          <a href="https://buy.polar.sh/checkout">Buy now</a>
          <footer><a href="https://sponsor.example/" title="Visit Website">Sponsor</a></footer>
          <a href="/marketplace/templates/another/">Another</a>'''
        result=extract(source,raw,'https://www.framer.com/marketplace/templates/example/',{'framer.com'})
        self.assertEqual([r['target'] for r in result['targets']],['https://demo.example/'])
        self.assertIn('https://www.framer.com/marketplace/templates/another/',result['follow'])

    def test_components_from_remix_and_iframe(self):
        source={'id':'uiverse','url':'https://uiverse.io/','kind':'components'}
        markup='<button class="example">Some reusable example</button>'
        css='.example { background: linear-gradient(red, blue); }'
        script='window.__remixContext = '+json.dumps({'state':{'loaderData':{'post':{'html':markup,'css':css}}}})+';'
        result=extract(source,'<script>'+script+'</script>','https://uiverse.io/author/example',{'uiverse.io'})
        self.assertEqual({s['code'] for s in result['snippets']},{markup,css})
        self.assertEqual(result['targets'],[])

    def test_nuxt_references_resolve_only_website_records(self):
        table=[{'data':1},['ShallowReactive',2],{'website':3,'advert':10},
               {'id':4,'title':5,'url':6,'image':7,'slug':8,'tags':9},123,'Example',
               'https://demo.example/','example.jpg','example',[],{'url':11},'https://ads.example/']
        source={'id':'maxibestof','url':'https://maxibestof.one/','kind':'gallery'}
        result=extract_payload(source,json.dumps(table),'https://maxibestof.one/websites/example/_payload.json',{'maxibestof.one'})
        self.assertEqual([v['target'] for v in result['targets']],['https://demo.example/'])
        self.assertEqual(result['targets'][0]['title'],'Example')
        cyclic=decode_nuxt([{'self':0}])
        self.assertIs(cyclic['self'],cyclic)

    def test_codrops_same_host_demo_is_target_and_repo_is_source(self):
        source={'id':'codrops','url':'https://tympanus.net/codrops/hub/','kind':'demos'}
        raw='''<a class="demo-card__thumb-img" href="/Tutorials/Example/">Demo</a>
          <a href="https://github.com/codrops/Example/tree/main/src">Source</a>'''
        result=extract(source,raw,source['url'],{'tympanus.net'})
        self.assertEqual([v['target'] for v in result['targets']],['https://tympanus.net/Tutorials/Example/'])
        self.assertNotIn('https://tympanus.net/Tutorials/Example/',result['follow'])
        self.assertEqual(result['repositories'],['https://github.com/codrops/Example'])


class ArchiveTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.out=Path(self.temp.name)
        self.sources=[{'id':'httpster','url':'https://httpster.net/','kind':'gallery'},
                      {'id':'siiimple','url':'https://siiimple.com/','kind':'gallery'}]
        self.archive=multi.Archive(self.out,self.sources)

    async def asyncTearDown(self):
        self.archive.db.close()
        self.temp.cleanup()

    async def test_cross_source_dedup_preserves_both_attributions(self):
        for source in self.sources:
            self.archive.add_site(source['id'],{'target':'https://demo.example/?utm_source=test','title':'Example',
                                               'entry_url':source['url'],'evidence':'visit'})
        self.assertEqual(self.archive.db.execute('SELECT COUNT(*) FROM sites').fetchone()[0],1)
        self.assertEqual(self.archive.db.execute("SELECT COUNT(*) FROM jobs WHERE kind='site'").fetchone()[0],1)
        self.assertEqual(self.archive.db.execute('SELECT COUNT(*) FROM provenance').fetchone()[0],2)

    async def test_manifest_export_allows_download_completion_and_closes_reader(self):
        self.archive.db.commit()
        entered,release=threading.Event(),threading.Event()
        original=self.archive.export

        def slow_export(*args,**kwargs):
            entered.set()
            release.wait(3)
            return original(*args,**kwargs)

        with patch.object(self.archive,'export',slow_export):
            task=asyncio.create_task(self.archive.export_async(full=True))
            try:
                await asyncio.wait_for(asyncio.to_thread(entered.wait),2)
                self.archive.add_site('httpster',{'target':'https://new.example/','title':'New',
                                                'entry_url':'https://httpster.net/','evidence':'visit'})
                self.archive.db.commit()
                self.assertFalse(task.done())
            finally:
                release.set()
            await task
        self.assertEqual((await self.archive.export_async())['sites'],{'pending':1})

    async def test_process_analysis_preserves_html_references_and_text(self):
        raw=b'<title>Demo</title><base href="https://cdn.example/assets/"><img data-src="lazy.png"><style>.a{background:url(bg.png)}</style>'
        expected=analyze_page(raw,'https://demo.example/')
        pool=ProcessPoolExecutor(max_workers=1)
        robots=Robots(None,self.out)
        robots.process_pool=pool
        robots.process_slots=asyncio.Semaphore(2)
        try:
            actual=await asyncio.wait_for(robots.process(analyze_page,raw,'https://demo.example/'),15)
            self.assertEqual(actual,expected)
            self.assertEqual(actual['references'],['https://cdn.example/assets/lazy.png','https://cdn.example/assets/bg.png'])
        finally:
            await asyncio.to_thread(pool.shutdown,wait=True,cancel_futures=True)

    async def test_cancelled_analysis_still_limits_unfinished_work(self):
        entered,release,second_entered=threading.Event(),threading.Event(),threading.Event()
        pool=ThreadPoolExecutor(max_workers=2)
        robots=Robots(None,self.out)
        robots.process_pool=pool
        robots.process_slots=asyncio.Semaphore(1)

        def slow_analysis():
            entered.set()
            release.wait(3)

        try:
            first=asyncio.create_task(robots.process(slow_analysis))
            await asyncio.wait_for(asyncio.to_thread(entered.wait),2)
            first.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await first
            second=asyncio.create_task(robots.process(second_entered.set))
            await asyncio.sleep(.05)
            self.assertFalse(second_entered.is_set())
            release.set()
            await asyncio.wait_for(second,2)
            self.assertTrue(second_entered.is_set())
        finally:
            release.set()
            await asyncio.to_thread(pool.shutdown,wait=True,cancel_futures=True)

    async def test_cancelled_export_finishes_before_final_manifest(self):
        self.archive.db.commit()
        entered,release=threading.Event(),threading.Event()
        original=self.archive.export

        def slow_export(*args,**kwargs):
            entered.set()
            release.wait(3)
            return original(*args,**kwargs)

        with patch.object(self.archive,'export',slow_export):
            task=asyncio.create_task(self.archive.export_async(full=True))
            await asyncio.wait_for(asyncio.to_thread(entered.wait),2)
            task.cancel()
            await asyncio.sleep(.05)
            self.assertFalse(task.done())
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.archive.phase='stopped'
        await self.archive.export_async(full=True)
        self.assertEqual(json.loads((self.out/'progress.json').read_text(encoding='utf-8'))['phase'],'stopped')

    async def test_unsafe_zip_cannot_escape_repository_folder(self):
        folder=self.out/'repositories'/'owner--repo'
        folder.mkdir(parents=True)
        with zipfile.ZipFile(folder/'source.zip','w') as z:
            z.writestr('repo-HEAD/../../outside.html','unsafe')
        with self.assertRaisesRegex(RuntimeError,'unsafe_archive_path'):
            await multi.download_repo({'url':'https://github.com/owner/repo','source':'httpster'},
                                      self.archive,None,SimpleNamespace(max_repo_bytes=1024**2))
        self.assertFalse((self.out/'outside.html').exists())

    async def test_cancel_repository_extraction_without_background_writes(self):
        folder=self.out/'repositories'/'owner--repo'
        folder.mkdir(parents=True)
        with zipfile.ZipFile(folder/'source.zip','w') as z:
            z.writestr('repo-HEAD/index.html','<title>Example</title>')
        entered=threading.Event()
        pool=ThreadPoolExecutor(max_workers=2)
        robots=Robots(None,self.out)
        robots.io_pool=pool

        def blocked_extract(job,folder,archive_file,url,max_bytes,cancelled):
            entered.set()
            cancelled.wait(3)
            raise RuntimeError('repository_extraction_interrupted')

        try:
            with patch.object(multi,'extract_repository',blocked_extract):
                task=asyncio.create_task(multi.download_repo({'url':'https://github.com/owner/repo','source':'httpster'},
                    self.archive,robots,SimpleNamespace(max_repo_bytes=1024**2)))
                await asyncio.wait_for(asyncio.to_thread(entered.wait),2)
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await asyncio.wait_for(task,1)
            self.assertFalse((folder/'metadata.json').exists())
        finally:
            await asyncio.to_thread(pool.shutdown,wait=True,cancel_futures=True)

    async def test_legacy_gallery_redirect_indexes_real_landing_url(self):
        self.archive.sources['onepagelove']={'id':'onepagelove','url':'https://onepagelove.com/','kind':'gallery'}
        self.archive.gallery_hosts.add('onepagelove.com')
        folder=self.out/'old-site'
        folder.mkdir()
        (folder/'source.html').write_text('<title>Actual target</title>',encoding='utf-8')
        (folder/'metadata.json').write_text(json.dumps({'final_url':'https://actual.example/'}),encoding='utf-8')
        self.archive.add_site('onepagelove',{'target':'https://onepagelove.com/go/example','title':'Example',
            'entry_url':'https://onepagelove.com/example','evidence':'legacy'},folder,'downloaded')
        self.assertEqual(self.archive.db.execute('SELECT target,final_url FROM sites').fetchall(),
                         [('https://actual.example/','https://actual.example/')])
        self.archive.add_site('onepagelove',{'target':'https://onepagelove.com/go/unresolved','title':'Other',
            'entry_url':'https://onepagelove.com/other','evidence':'legacy'})
        self.assertEqual(self.archive.db.execute('SELECT COUNT(*) FROM sites').fetchone()[0],1)
        self.assertEqual(self.archive.db.execute("SELECT url FROM jobs WHERE kind='resolve'").fetchone()[0],
                         'https://onepagelove.com/go/unresolved')

    async def test_interrupted_legacy_metadata_requeues_without_losing_files(self):
        folder=self.out/'interrupted'
        folder.mkdir()
        (folder/'metadata.json').write_text('',encoding='utf-8')
        (folder/'source.html').write_text('<html>incomplete</html>',encoding='utf-8')
        self.archive.add_site('httpster',{'target':'https://actual.example/','title':'Example',
            'entry_url':'https://httpster.net/website/example/','evidence':'legacy'},folder,'downloaded')
        self.assertEqual(self.archive.db.execute('SELECT status FROM sites').fetchone()[0],'pending')
        self.assertTrue((folder/'source.html').exists())

    async def test_stop_cancels_blocked_http_and_preserves_queue(self):
        root=self.out/'pause-test'
        root.mkdir()
        config=root/'sources.json'
        config.write_text(json.dumps({'sources':[self.sources[0]]}),encoding='utf-8')
        started=asyncio.Event()

        async def never_finishes(*args,**kwargs):
            started.set()
            await asyncio.Event().wait()

        argv=['crawl','--config',str(config),'--output',str(root),'--opl-archive',str(root/'missing'),
              '--workers','1','--discovery-workers','1','--repo-workers','1','--connections','2']
        with patch('sys.argv',argv),patch.object(multi.Robots,'fetch',never_finishes):
            args=multi.arguments()
            task=asyncio.create_task(multi.main(args))
            await asyncio.wait_for(started.wait(),3)
            (root/'STOP').write_text('pause',encoding='utf-8')
            await asyncio.wait_for(task,3)
        progress=json.loads((root/'progress.json').read_text(encoding='utf-8'))
        self.assertEqual(progress['phase'],'stopped')
        self.assertIsNone(progress['pid'])
        self.assertFalse((root/'crawl.lock').exists())
        self.assertEqual(progress['jobs'],[{'kind':'discovery','status':'pending','count':1}])


if __name__=='__main__':
    unittest.main()

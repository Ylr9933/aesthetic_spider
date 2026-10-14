"""Read public source pages through the same HTTP/robots policy as the crawler."""
import asyncio
import json
import re
from collections import Counter
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import aiohttp
from bs4 import BeautifulSoup
from crawl_onepagelove import Robots, AGENT, atomic_json


async def main():
    out = Path('data/multisource_probe').resolve()
    out.mkdir(parents=True, exist_ok=True)
    config = json.loads(Path('frontend_sources_config.json').read_text(encoding='utf-8'))
    sem = asyncio.Semaphore(12)
    async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(limit=48, limit_per_host=2),
                                     headers={'User-Agent': AGENT}) as session:
        robots = Robots(session, out, connections=48)

        async def probe(source):
            async with sem:
                item = dict(source)
                try:
                    cache = out / (source['id'] + '.html')
                    if cache.exists():
                        response = {'body': cache.read_bytes(), 'status': 200, 'final_url': source['url']}
                        await robots.rule(source['url'])
                    else:
                        response = await robots.fetch(source['url'], max_bytes=8*1024**2, minimum=.8)
                    raw = response['body']
                    (out / (source['id'] + '.html')).write_bytes(raw)
                    soup = BeautifulSoup(raw, 'html.parser')
                    host = urlsplit(response['final_url']).hostname.removeprefix('www.')
                    links = []
                    for a in soup.select('a[href]'):
                        url = urljoin(response['final_url'], a['href'])
                        if urlsplit(url).scheme not in {'http', 'https'}:
                            continue
                        links.append({'url': url, 'text': a.get_text(' ', strip=True)[:120],
                                      'class': a.get('class'), 'rel': a.get('rel'),
                                      'image': bool(a.select_one('img,picture,video'))})
                    item.update(status=response['status'], final_url=response['final_url'], bytes=len(raw),
                                title=soup.title.get_text(' ', strip=True) if soup.title else '',
                                scripts=[s.get('src') for s in soup.select('script[src]')][:15],
                                internal=Counter(urlsplit(a['url']).path.strip('/').split('/')[0] for a in links
                                                 if urlsplit(a['url']).hostname.removeprefix('www.') == host).most_common(15),
                                external=[a for a in links if urlsplit(a['url']).hostname.removeprefix('www.') != host][:40],
                                links=links,
                                robots=(robots.rules.get(urlsplit(source['url']).scheme+'://'+urlsplit(source['url']).netloc).site_maps() or []))
                except Exception as e:
                    item['error'] = str(e) or type(e).__name__
                atomic_json(out / (source['id'] + '.json'), item)
                print(json.dumps({k:v for k,v in item.items() if k not in {'links','scripts','repositories'}}, ensure_ascii=True), flush=True)
                return item
        results = await asyncio.gather(*(probe(s) for s in config['sources']))
        atomic_json(out / 'results.json', results)


if __name__ == '__main__':
    asyncio.run(main())

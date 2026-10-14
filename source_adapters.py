"""Conservative, source-specific extraction of public demos, sites and code.

Gallery HTML is discovery evidence, never automatically a training example.
No JavaScript execution, authenticated APIs or purchase/remix actions.
"""
from __future__ import annotations

import json
import re
from urllib.parse import parse_qsl, urljoin, urlsplit

from bs4 import BeautifulSoup
from crawl_onepagelove import normalized, public_url


# detail regex, listing regex, verified direct target selectors
RULES = {
    'lapa': (r'^/post/[^/]+/?$', r'^/(?:page/\d+/|category/[^/]+/)?$', ''),
    'cssdesignawards': (r'^/sites/[^/]+/\d+/?$', r'^/(?:wotd-award-winners|website-gallery|nominees)/?$',
                        'a.sp__project-link,a.single-website__thumbnail'),
    'fwa': (r'^/cases/[^/]+/?$', r'^/awards/?$', ''),
    'siteinspire': (r'^/websites/\d+[^/]*', r'^/websites/?$', ''),
    'httpster': (r'^/website/[^/]+/?$', r'^/page/\d+/?$', 'a.Preview__ext,a.WebsiteView__ext'),
    'onepagelove': (r'^/[^/]+/?$', r'^/inspiration(?:/page/\d+)?/?$', '.thumb-link a'),
    'neocities': (r'^/site/[^/]+/?$', r'^/browse/?$', 'a.neo-Screen-Shot'),
    'codrops': (r'^/codrops/(?:\d{4}/\d{2}/\d{2}/[^/]+|hub/[^/]+)/?$',
                r'^/codrops/(?:hub(?:/page/\d+)?|webzibition(?:/page/\d+)?)/?$',
                'a.demo-card__thumb-img,a[href*="/Tutorials/"],a[href*="/Development/"]'),
    'landinglove': (r'^/sites/[^/]+/?$', r'^/(?:sites(?:/page/\d+)?|categories/[^/]+(?:/page/\d+)?)/?$', ''),
    'recent': (r'^/i/[^/]+/?$', r'^/websites/?$', ''),
    'minimalgallery': (r'^/(?:website|site)/[^/]+/?$', r'^/(?:page/\d+|websites)/?$', ''),
    'saaslandingpage': (r'^/[^/]+/?$', r'^/page/\d+/?$', ''),
    'landingfolio': (r'^/inspiration/post/[^/]+/?$', r'^/inspiration/(?:landing-page|pricing|about)(?:/[^/]+)?/?$', ''),
    'csswinner': (r'^/details/[^/]+/\d+/?$', r'^/(?:winners|nominees|stars)(?:/[^/]+)*/?$', 'a.view-site'),
    'minimalwebsites': (r'^/[^/]+/?$', r'^/page/\d+/?$', ''),
    'webdesigninspiration': (r'^/inspiration/[^/]+/?$', r'^/(?:web-designs|page/\d+|website/[^/]+|web-design/[^/]+)/?$', 'a.imglink'),
    'csslight': (r'^/website/\d+/[^/]+/?$', r'^/(?:featured-sites|sites|gallery|category/[^/]+)(?:/\d+)?/?$', 'a.go'),
    'sitesee': (r'^/entry/[^/]+/?$', r'^/page/\d+/?$', ''),
    'nicelydone': (r'^/(?:apps/[^/]+|products/[^/]+|examples/apps/[^/]+)/?$', r'^/(?:products|apps|examples|examples/apps)/?$', ''),
    'darkmodedesign': (r'^/site/[^/]+/?$', r'^/$', 'a.screenshot'),
    'darkdesign': (r'^/site/[^/]+/?$', r'^/(?:sites|templates)/?$', ''),
    'brutalist': (r'^/[^/]+/?$', r'^/(?:raw|page/\d+)?/?$', 'a'),
    'maxibestof': (r'^/websites/\d+-[^/]+/?$', r'^/websites(?:/[^/]+)?/?$', ''),
    'siiimple': (r'^/[^/]+/?$', r'^/page/\d+/?$', ''),
    'admiretheweb': (r'^/inspiration/[^/]+/?$', r'^/(?:page/\d+|category/[^/]+)/?$', 'article.c-item--full .c-item__image a,article.c-item--full .c-item__title a'),
    'uiverse': (r'^/[^/]+/[^/]+/?$', r'^/(?:elements|buttons|cards|loaders|inputs|forms|toggles|checkboxes|radio-buttons|patterns|tooltips)/?$', ''),
    'hyperui': (r'^/components/(?:application|marketing|neobrutalism|templates)/[^/]+/?$', r'^/components/[^/]+/?$', ''),
    'flowbite': (r'^/docs/(?:components|forms|typography)/[^/]+/?$', r'^/docs/(?:getting-started/introduction|components)/?$', ''),
    'preline': (r'^/(?:docs|examples)/[^/]+(?:\.html)?$', r'^/(?:docs|examples)/?$', ''),
    'merakiui': (r'^/components/[^/]+/[^/]+/?$', r'^/components/[^/]+/?$', ''),
    'mambaui': (r'^/components/[^/]+/?$', r'^/components/?$', ''),
    'floatui': (r'^/(?:components|ui)/[^/]+/?$', r'^/(?:components|ui)/?$', ''),
    'bootdey': (r'^/(?:snippets/view|tailwind-components/view)/[^/]+/?$', r'^/(?:bootstrap-snippets|tailwind-components|snippets/tagged/[^/]+)(?:/\d+)?/?$', ''),
    'framer': (r'^/marketplace/templates/[^/]+/?$', r'^/marketplace/templates(?:/categories/[^/]+)?/?$', ''),
}

SKIP_PATH = re.compile(r'^/(?:about|contact|privacy|terms|legal|login|sign-?up|register|auth|account|checkout|submit|advertis|sponsor|pricing|faq|search|blog|font|fonts|tag|tags|color|colors|profile|profiles|author|authors|feed|api)(?:/|-|$)', re.I)
ASSET_PATH = re.compile(r'\.(?:png|jpe?g|webp|avif|svg|gif|mp4|webm|css|js|woff2?|zip|pdf|ico)(?:$|\?)', re.I)
SOCIAL_HOSTS = {'twitter.com','x.com','facebook.com','instagram.com','pinterest.com','pinterest.fr',
                'linkedin.com','youtube.com','youtu.be','discord.gg','discord.com','bsky.app','threads.com',
                'dribbble.com','behance.net','paypal.me','patreon.com','buymeacoffee.com','ko-fi.com',
                'github.com','opensource.org','feeds.feedburner.com','disqus.com','chatgpt.com','lovable.dev',
                'v0.dev','producthunt.com','chrome.google.com','addons.mozilla.org','stumbleupon.com',
                'framer.link','dub.sh','buy.polar.sh','gumroad.com','lemonsqueezy.com','try.webflow.com'}
VISIT = re.compile(r'\b(?:visit(?:\s+(?:the\s+)?(?:web)?site)?|view\s+(?:live\s+)?(?:site|website|demo)|launch\s+(?:site|website|demo)|live\s+(?:preview|demo)|show\s+preview|open\s+(?:website|site)|view\s+website)\b', re.I)


def host(url):
    return (urlsplit(url).hostname or '').lower().removeprefix('www.')


def is_detail(source, url):
    p = urlsplit(url).path
    return bool(re.search(RULES[source['id']][0], p) and not SKIP_PATH.search(p))


def directory_url(source, url):
    if not public_url(url) or host(url) != host(source['url']):
        return False
    p = urlsplit(url)
    if ASSET_PATH.search(p.path) or SKIP_PATH.search(p.path):
        # Component documentation and gallery namespaces are intentional exceptions.
        if not (source['kind'] == 'components' and p.path.startswith('/docs/')):
            return False
    if source['id'] == 'codrops' and re.match(r'^/(?:Tutorials|Development)/', p.path):
        return False  # This is a demo target, never a discovery page.
    if source['id'] == 'neocities' and p.path == '/browse' and p.query:
        if any(k != 'page' for k, _ in parse_qsl(p.query)):
            return False  # One stable browse ordering; avoid combinatorial tag/sort filters.
    if any(k not in {'page','p','offset','start','60a99e98_page'} and not k.endswith('_page')
           for k, _ in parse_qsl(p.query)):
        return False
    return normalized(url) == normalized(source['url']) or is_detail(source, url) or bool(re.search(RULES[source['id']][1], p.path))


def target_url(source, url, gallery_hosts):
    if not public_url(url) or ASSET_PATH.search(urlsplit(url).path):
        return False
    h = host(url)
    if h in gallery_hosts:
        return source['id'] == 'codrops' and bool(re.match(r'^/(?:Tutorials|Development)/', urlsplit(url).path))
    if h in SOCIAL_HOSTS or any(h.endswith('.'+v) for v in {'facebook.com','instagram.com','pinterest.com','lemonsqueezy.com','gumroad.com','polar.sh'}):
        return False
    return True


def repository_url(url):
    if host(url) != 'github.com':
        return None
    parts = urlsplit(url).path.strip('/').split('/')
    if len(parts) < 2 or parts[0] in {'topics','sponsors','orgs','settings','login'}:
        return None
    if not all(re.fullmatch(r'[A-Za-z0-9_.-]+', v) for v in parts[:2]):
        return None
    return 'https://github.com/' + '/'.join(parts[:2]).removesuffix('.git')


def json_documents(soup):
    """Decode data only, never eval/execute script. Handles JSON and Next RSC strings."""
    decoder = json.JSONDecoder()
    for script in soup.select('script'):
        if script.get('src'):
            continue
        text = script.get_text().strip()
        if not text:
            continue
        if script.get('type') in {'application/json','application/ld+json'} or script.get('id') in {'__NEXT_DATA__','__NUXT_DATA__'}:
            try:
                data = json.loads(text)
                yield decode_nuxt(data) if script.get('id') == '__NUXT_DATA__' else data
            except ValueError:
                pass
        if 'self.__next_f.push(' in text:
            try:
                arr, _ = decoder.raw_decode(text[text.index('(')+1:])
                if len(arr) > 1 and isinstance(arr[1], str):
                    for line in arr[1].splitlines():
                        _, _, value = line.partition(':')
                        try:
                            yield json.loads(value)
                        except ValueError:
                            pass
            except (ValueError, TypeError, IndexError):
                pass
        # FWA preloaded public cases.
        for match in re.finditer(r'\bcaseMenu\s*:\s*', text):
            try:
                value, _ = decoder.raw_decode(text[match.end():])
                yield value
            except ValueError:
                pass
        for match in re.finditer(r'window\.__remixContext\s*=\s*',text):
            try:
                value,_=decoder.raw_decode(text[match.end():])
                yield value
            except ValueError:
                pass


def decode_nuxt(values):
    """Nuxt devalue uses numeric references into a JSON table, not literal IDs."""
    if not isinstance(values,list) or not values or not isinstance(values[0],(dict,list)):
        return values
    memo={}

    def resolve(index):
        if index < 0 or index >= len(values):
            return None
        if index in memo:
            return memo[index]
        value=values[index]
        if isinstance(value,dict):
            result={}
            memo[index]=result
            result.update((k,resolve(v) if type(v) is int else v) for k,v in value.items())
            return result
        if isinstance(value,list):
            if len(value)==2 and isinstance(value[0],str) and value[0] in {'Reactive','ShallowReactive','Ref','ShallowRef'}:
                memo[index]=None
                result=resolve(value[1])
                memo[index]=result
                return result
            result=[]
            memo[index]=result
            result.extend(resolve(v) if type(v) is int else v for v in value)
            return result
        memo[index]=value
        return value
    return resolve(0)


def walk_objects(value):
    stack = [value]
    seen=set()
    while stack:
        current = stack.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        if isinstance(current, dict):
            yield current
            stack.extend(v for v in current.values() if isinstance(v, (dict,list)))
        elif isinstance(current, list):
            stack.extend(v for v in current if isinstance(v, (dict,list)))


def extract(source, raw, page_url, gallery_hosts):
    soup = BeautifulSoup(raw, 'html.parser')
    targets, follow, repos, snippets = {}, set(), set(), {}
    payloads=set()
    heading = soup.select_one('h1')
    title = heading.get_text(' ', strip=True) if heading else soup.title.get_text(' ', strip=True) if soup.title else page_url
    detail = is_detail(source, page_url)
    sid = source['id']

    def add(url, name='', evidence=''):
        try:
            full = normalized(urljoin(page_url, url))
            if target_url(source, full, gallery_hosts):
                targets[full] = {'target': full, 'title': name or (title if detail else host(full)), 'entry_url': page_url, 'evidence': evidence}
        except ValueError:
            pass

    selector = RULES[sid][2]
    if selector:
        for a in soup.select(selector):
            if a.get('href'):
                # Brutalist's raw grid: external thumbnail links are the projects.
                if sid == 'brutalist' and not a.select_one('img'):
                    continue
                image = a.select_one('img')
                name = a.get_text(' ', strip=True) or (image.get('alt') if image else '')
                add(a['href'], name, 'selector:'+selector)

    for a in soup.select('a[href]'):
        try:
            full = normalized(urljoin(page_url, a['href']))
            if directory_url(source, full):
                follow.add(full)
        except ValueError:
            continue
        text = ' '.join(str(a.get(k,'')) for k in ('title','aria-label'))+' '+a.get_text(' ',strip=True)
        chrome = a.find_parent(['footer','nav','aside']) or a.find_parent(class_=re.compile(r'(?:^|[-_ ])(?:sponsor|advert|ads|banner|partner)(?:[-_ ]|$)',re.I))
        visit_text = a.get_text(' ',strip=True) if sid == 'sitesee' else text
        if VISIT.search(visit_text) and not chrome:
            add(full, '', 'visit_or_preview_link')
        if sid == 'csswinner' and detail and 'cover' in a.get('class', []):
            add(full, '', 'detail_cover')
        if sid == 'landinglove' and not chrome and a.get('target') == '_blank' and 'ref=landing.love' in a['href']:
            add(full, '', 'site_card_external')
        if sid == 'darkdesign' and not chrome and a.find_parent('main') and a.get('target') == '_blank' and ('opacity-30' in a.get('class',[]) or detail and VISIT.search(text)):
            add(full, '', 'site_card_external')
        if sid == 'csslight' and detail and a.select_one('img'):
            if a.find_parent(class_=re.compile('(?:website|detail|screen|feature|site-info)', re.I)):
                add(full, '', 'detail_thumbnail')
        repo = repository_url(full)
        if repo and (source['kind'] in {'components','demos'}):
            repos.add(repo)

    # Demos can live on the same host as their directory.
    if sid == 'codrops':
        for frame in soup.select('iframe[src]'):
            add(frame['src'], '', 'demo_iframe')

    for document in json_documents(soup):
        for obj in walk_objects(document):
            name = obj.get('name') or obj.get('title') or title
            if not isinstance(name,str):
                name = title
            # Require semantic demo/site fields rather than arbitrary strings in hydration.
            for key in ('websiteUrl','website_url','siteUrl','site_url','demoUrl','demo_url','previewUrl','preview_url'):
                if isinstance(obj.get(key),str) and source['kind'] != 'components':
                    add(obj[key],name,'embedded:'+key)
            if sid == 'fwa' and obj.get('slug') and (obj.get('awards') is not None or obj.get('bestViewedOn')):
                if isinstance(obj.get('url'),str):
                    add(obj['url'],name,'fwa_case_data')
                follow.add(urljoin(page_url,'/cases/'+obj['slug']))
            if source['kind'] == 'components':
                for key in ('html','css','code','source','markup','jsx'):
                    code = obj.get(key)
                    if isinstance(code,str) and len(code) > 40 and ('<' in code or key == 'css'):
                        snippets[code] = {'language':key,'code':code,'evidence':'embedded:'+key}

    if sid=='recent':
        # TanStack serializes public source objects as JS object literals. Parse
        # the bounded JSON string fields; do not execute the hydration program.
        for script in soup.select('script:not([src])'):
            for match in re.finditer(r'type:"website",url:("(?:[^"\\]|\\.)*"),externalId:("(?:[^"\\]|\\.)*"),title:("(?:[^"\\]|\\.)*")',script.get_text()):
                try:
                    add(json.loads(match[1]),json.loads(match[3]),'public_website_source_object')
                except ValueError:
                    pass

    for script in soup.select('script[data-src]'):
        url=urljoin(page_url,script['data-src'])
        if host(url)==host(source['url']) and urlsplit(url).path.endswith('/_payload.json'):
            payloads.add(url)

    if source['kind'] == 'components' and detail:
        for pre in soup.select('pre,textarea'):
            code = pre.get_text()
            if len(code.strip()) < 40 or '<' not in code and '{' not in code:
                continue
            classes = ' '.join(pre.get('class',[]))
            inner = pre.select_one('code')
            if inner:
                classes += ' '+' '.join(inner.get('class',[]))
            language = 'css' if 'css' in classes else 'javascript' if re.search(r'javascript|jsx|tsx',classes) else 'html'
            snippets[code] = {'language':language,'code':code,'evidence':'public_code_block'}
        # Several component sites publish standalone iframe srcdoc examples.
        for frame in soup.select('iframe[srcdoc]'):
            code = frame.get('srcdoc','')
            if len(code) > 40:
                snippets[code] = {'language':'html','code':code,'evidence':'iframe_srcdoc'}
        for node in soup.select('[data-code],[data-html]'):
            code = node.get('data-code') or node.get('data-html')
            if code and len(code) > 40:
                snippets[code] = {'language':'html','code':code,'evidence':'data_code'}

    return {'title':title,'targets':list(targets.values()),'follow':sorted(follow),
            'repositories':sorted(repos),'snippets':list(snippets.values()),
            'payloads':sorted(payloads),'detail':detail,'has_next':bool(soup.select_one('a[rel="next"],a.next,a.w-pagination-next'))}


def extract_payload(source, raw, page_url, gallery_hosts):
    data=decode_nuxt(json.loads(raw))
    targets,follow=[],set()
    for obj in walk_objects(data):
        name=obj.get('title') or obj.get('name') or ''
        if source['id']=='maxibestof' and obj.get('id') and obj.get('image') and isinstance(obj.get('url'),str):
            url=normalized(obj['url'])
            if target_url(source,url,gallery_hosts):
                targets.append({'target':url,'title':name,'entry_url':page_url,'evidence':'public_nuxt_website_payload'})
        for k in ('canonical_path',):
            if isinstance(obj.get(k),str):
                url=urljoin(page_url,obj[k])
                if directory_url(source,url):
                    follow.add(url)
        if source['id']=='nicelydone' and obj.get('slug') and obj.get('logo') and obj.get('title'):
            url=urljoin(page_url,'/apps/'+obj['slug'])
            if directory_url(source,url):
                follow.add(url)
        for k in ('websiteUrl','website_url','siteUrl','site_url'):
            if isinstance(obj.get(k),str) and target_url(source,obj[k],gallery_hosts):
                targets.append({'target':normalized(obj[k]),'title':name,'entry_url':page_url,'evidence':'public_payload:'+k})
    return {'targets':targets,'follow':sorted(follow)}

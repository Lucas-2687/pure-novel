"""Read-only metadata discovery. No third-party credentials or file downloads."""
import re
import time
import threading
from collections import OrderedDict
from urllib.parse import urlencode, urlsplit

ORIGIN = 'https://z-lib.tv'
_cache = OrderedDict()
_lock = threading.Lock()


def parse_results(html, url, document, tidy):
    """Extract only explicitly labelled book metadata, not arbitrary page text."""
    if urlsplit(url).hostname != 'z-lib.tv':
        raise ValueError('书目来源跳转到其他域名，已停止处理。')
    nodes = list(document(html).root.walk())
    books, seen = [], set()
    for heading in nodes:
        if heading.tag != 'h3' or heading.attrs.get('itemprop') != 'name':
            continue
        title = tidy(heading.text())[:300]
        if not title:
            continue
        container = heading
        for _ in range(8):
            if container.parent is None or container.tag == 'table':
                break
            container = container.parent
        # Do not fall back to the whole page: that would mix different editions.
        if container.tag != 'table':
            continue
        children = list(container.walk())
        authors = list(dict.fromkeys(tidy(n.text()) for n in children if n.tag == 'a' and
                                     '/author/' in n.attrs.get('href', '') and tidy(n.text())))
        publishers = list(dict.fromkeys(tidy(n.text()) for n in children if n.tag == 'a' and
                                        '/publisher/' in n.attrs.get('href', '') and tidy(n.text())))
        text = tidy(container.text())
        def field(pattern):
            match = re.search(pattern, text, re.I)
            return match.group(1).strip()[:120] if match else ''
        language = field(r'(?:语言|Language)\s*:\s*(.*?)(?=\s+(?:File|文件|Year|年份|Your tags)\s*:|$)')
        fmt = field(r'(?:File|文件)\s*:\s*(EPUB|TXT|PDF|MOBI|AZW3|FB2|DJVU)\b').upper()
        year = field(r'(?:Year|年份)\s*:\s*(\d{4})\b')
        book = {'title': title, 'authors': authors, 'publishers': publishers,
                'language': language, 'format': fmt, 'year': year,
                'status': 'metadata_only', 'canDownload': False}
        key = (title.casefold(), tuple(authors), tuple(publishers), language, fmt, year)
        if key not in seen:
            seen.add(key)
            books.append(book)
        if len(books) >= 20:
            break
    return {'books': books, 'source': 'z-lib.tv（身份未独立核实）', 'searchUrl': url,
            'status': 'metadata_only' if books else 'no_metadata', 'canDownload': False,
            'notice': '仅取得匿名页面的书目信息，未核实文件授权或允许的获取接口；不代表文件可下载。'
                      '本功能不接收第三方账号或 Cookie，不获取或共享图书文件。'}


def search(query, fetch_page, document, tidy):
    if not isinstance(query, str) or not 2 <= len(query.strip()) <= 100 or re.search(r'[\x00-\x1f]', query):
        raise ValueError('请输入 2～100 字的书名、作者或 ISBN。')
    query = query.strip()
    with _lock:
        cached = _cache.get(query)
        if cached and time.monotonic() - cached[0] < 120:
            return cached[1]
    html, final = fetch_page(ORIGIN + '/zh/s?' + urlencode({'q': query}))
    result = parse_results(html, final, document, tidy)
    with _lock:
        _cache[query] = (time.monotonic(), result)
        _cache.move_to_end(query)
        while len(_cache) > 32:
            _cache.popitem(last=False)
    return result
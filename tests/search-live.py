"""Public search diagnostics, without credentials or stored response bodies."""
import sys
import time
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import reader


def probe(item):
    name, endpoint = item
    start = time.monotonic()
    try:
        html, final = reader.fetch_page(endpoint + quote('"斗破苍穹" 小说 目录'))
        results = reader.search_results(html, final, '斗破苍穹', name)
        print(name, 'seconds=', round(time.monotonic() - start, 1), 'bytes=', len(html),
              'title=', reader.re.search(r'<title[^>]*>(.*?)</title>', html, reader.re.S).group(1)[:100]
              if '<title' in html else '', 'results=', len(results), flush=True)
    except Exception as exc:
        print(name, 'seconds=', round(time.monotonic() - start, 1), str(exc), flush=True)


with ThreadPoolExecutor(max_workers=6) as pool:
    list(pool.map(probe, list(reader.SEARCH_ENGINES.items()) + [
        ('Bing CN', 'https://cn.bing.com/search?q='),
        ('360 www', 'https://www.so.com/s?ie=utf-8&q='),
    ]))
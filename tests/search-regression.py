"""Live search + readable chapter regression. Credentials only in environment."""
import http.cookiejar
import json
import os
import time
from urllib.request import HTTPCookieProcessor, Request, build_opener
from urllib.error import HTTPError

base = os.environ.get('DEPLOYMENT_URL', 'http://127.0.0.1:8000').rstrip('/')
client = build_opener(HTTPCookieProcessor(http.cookiejar.CookieJar()))


def request(path, body):
    start = time.monotonic()
    req = Request(base + path, data=json.dumps(body).encode(),
                  headers={'Content-Type': 'application/json', 'Origin': base})
    try:
        response = client.open(req, timeout=65)
    except HTTPError as exc:
        detail = exc.read().decode('utf-8', 'replace')[:500]
        raise RuntimeError(f'HTTP {exc.code}: {detail}') from None
    with response:
        result = json.load(response)
    print(path, 'seconds=', round(time.monotonic() - start, 1), flush=True)
    return result


authenticated = bool(os.environ.get('SMOKE_PASSWORD'))
try:
    if authenticated:
        request('/api/login', {'username': os.environ['SMOKE_USERNAME'], 'password': os.environ['SMOKE_PASSWORD']})
    query = os.environ.get('SMOKE_QUERY', '斗破苍穹')
    verified = None
    failures = []
    for round_number in range(2):
        result = request('/api/search', {'query': query, 'round': round_number})
        print('candidates=', len(result['results']), 'engines=', result['engines'], flush=True)
        for entry in result['results'][:8]:
            try:
                verified = request('/api/check', {'query': query, 'url': entry['url']})
                print('PASS sample:', verified, flush=True)
                page = request('/api/read', {'url': verified['sampleUrl']})
                assert page['kind'] == 'chapter' and page['paragraphs']
                print('PASS readable chapter:', page['title'], sum(map(len, page['paragraphs'])), flush=True)
                next_url = page.get('next')
                if not next_url and verified['kind'] == 'catalog':
                    catalog = request('/api/read', {'url': verified['url']})
                    chapters = catalog.get('chapters', [])
                    index = next((i for i, c in enumerate(chapters) if c['url'] == verified['sampleUrl']), -1)
                    if index >= 0 and index + 1 < len(chapters):
                        next_url = chapters[index + 1]['url']
                if next_url:
                    following = request('/api/read', {'url': next_url})
                    assert following['kind'] == 'chapter' and following['paragraphs']
                    print('PASS next chapter:', following['title'], flush=True)
                break
            except Exception as exc:
                verified = None
                failures.append(str(exc))
                print('Source failed:', entry['host'], str(exc), flush=True)
        if verified:
            break
    assert verified, f'No readable sample found: {failures}'
finally:
    if authenticated:
        request('/api/logout', {})
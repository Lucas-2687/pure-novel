"""Live HTTPS deployment checks; credentials come only from environment variables."""
import http.cookiejar
import json
import os
from urllib.error import HTTPError
from urllib.request import HTTPCookieProcessor, Request, build_opener


base = os.environ.get('DEPLOYMENT_URL', '').rstrip('/')
if not base.startswith('https://'):
    raise SystemExit('Set DEPLOYMENT_URL to your own HTTPS deployment before running this test.')
jar = http.cookiejar.CookieJar()
client = build_opener(HTTPCookieProcessor(jar))


def request(path, data=None, expected=200):
    headers = {'Origin': base}
    if data is not None:
        headers['Content-Type'] = 'application/json'
    req = Request(base + path, headers=headers,
                  data=json.dumps(data).encode() if data is not None else None)
    try:
        response = client.open(req, timeout=75)
    except HTTPError as exc:
        response = exc
    with response:
        body = response.read()
        assert response.code == expected, f'{path}: HTTP {response.code}, expected {expected}'
        print(f'PASS {req.get_method()} {path}: HTTP {response.code}')
        return json.loads(body) if 'application/json' in response.headers.get('Content-Type', '') else body


assert request('/api/health')['authRequired'] is True
for path in ('/', '/app.js', '/ebooks.js', '/style.css', '/sw.js', '/manifest.webmanifest', '/icon.svg'):
    assert request(path)
request('/api/session', expected=401)
request('/api/search', {'query': 'test'}, expected=401)
username = os.environ.get('SMOKE_USERNAME')
password = os.environ.get('SMOKE_PASSWORD')
if username and password:
    try:
        assert request('/api/login', {'username': username, 'password': password})['user']['username'] == username
        assert all(c.secure and c.has_nonstandard_attr('HttpOnly') for c in jar)
        assert len(jar) == 2
        assert request('/api/session')['user']['username'] == username
        state = request('/api/state')
        assert 'version' in state and 'payload' in state
        # Preserve existing data; an initially empty account gets an empty shelf.
        payload = state['payload'] if state['payload'] is not None else {'books': [], 'settings': {}}
        result = request('/api/state', {'version': state['version'], 'payload': payload})
        assert result['ok'] is True
        request('/api/state', {'version': state['version'], 'payload': payload}, expected=409)
        assert request('/api/state')['payload'] == payload
        search = request('/api/search', {'query': '\u7ea2\u697c\u68a6'})
        print('Search response keys:', ', '.join(search))
    finally:
        request('/api/logout', {})
    request('/api/session', expected=401)
else:
    print('Authenticated checks skipped: set SMOKE_USERNAME and SMOKE_PASSWORD.')
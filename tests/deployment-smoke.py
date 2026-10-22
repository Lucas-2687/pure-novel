"""Opt-in HTTPS checks. State writes need a dedicated account and SMOKE_WRITE_STATE=1."""
import http.cookiejar
import json
import os
import time
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import HTTPCookieProcessor, Request, build_opener


def main():
    base = os.environ.get('DEPLOYMENT_URL', '').rstrip('/')
    parsed = urlsplit(base)
    if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment:
        raise SystemExit('Set DEPLOYMENT_URL to your own HTTPS site origin; no credentials or path.')
    jar = http.cookiejar.CookieJar()
    client = build_opener(HTTPCookieProcessor(jar))

    def request(path, data=None, expected=200, method=None):
        headers = {'Origin': base}
        if data is not None:
            headers['Content-Type'] = 'application/json'
        req = Request(base + path, headers=headers, method=method,
                      data=json.dumps(data).encode() if data is not None else None)
        started = time.monotonic()
        try:
            response = client.open(req, timeout=60)
        except HTTPError as exc:
            response = exc
        with response:
            body = response.read(1024 * 1024 + 1)
            assert len(body) <= 1024 * 1024, 'Unexpected oversized response'
            assert response.code == expected, f'{path}: HTTP {response.code}, expected {expected}'
            if path.startswith('/api/'):
                assert 'no-store' in response.headers.get('Cache-Control', '').lower(), 'Personal API must not be cached'
            elif response.code == 200:
                assert response.headers.get('X-Content-Type-Options') == 'nosniff'
                assert "frame-ancestors 'none'" in response.headers.get('Content-Security-Policy', '')
            elapsed = round((time.monotonic() - started) * 1000, 1)
            print(json.dumps({'method': req.get_method(), 'path': path, 'status': response.code,
                              'milliseconds': elapsed, 'cache': response.headers.get('X-Vercel-Cache'),
                              'timing': response.headers.get('Server-Timing')}, ensure_ascii=True))
            if req.get_method() == 'HEAD':
                assert not body, 'HEAD must not send a response body'
                return None
            return json.loads(body) if 'application/json' in response.headers.get('Content-Type', '') else body

    assert request('/api/health')['authRequired'] is True
    request('/api/health', method='HEAD')
    for path in ('/', '/app.js', '/ebooks.js', '/storage.js', '/style.css', '/sw.js', '/manifest.webmanifest', '/icon.svg'):
        assert request(path)
        request(path, method='HEAD')
    request('/api/session', expected=401)
    request('/api/bootstrap', expected=401)
    request('/api/search', {'query': 'test'}, expected=401)
    request('/api/nonexistent', expected=404)
    username, password = os.environ.get('SMOKE_USERNAME'), os.environ.get('SMOKE_PASSWORD')
    if not username or not password:
        print('Authenticated checks skipped: set dedicated SMOKE_USERNAME / SMOKE_PASSWORD in the environment.')
        return
    try:
        assert request('/api/login', {'username': username, 'password': password})['user']['username'] == username.strip().lower()
        assert all(c.secure and c.has_nonstandard_attr('HttpOnly') for c in jar)
        assert len(jar) == 2
        state = request('/api/bootstrap')
        assert state['user']['username'] == username.strip().lower()
        assert type(state['version']) is int and 'payload' in state
        if os.environ.get('SMOKE_WRITE_STATE') == '1':
            payload = state['payload'] if state['payload'] is not None else {'books': [], 'settings': {}}
            result = request('/api/state', {'version': state['version'], 'payload': payload})
            assert result['ok'] is True
            assert request('/api/state')['payload'] == payload
            # Obtain a genuinely stale version even when identical saves are no-ops.
            if result['version'] > 0:
                request('/api/state', {'version': result['version'] - 1, 'payload': payload}, expected=409)
            else:
                print('Conflict check skipped: empty account still has version 0; use the dedicated two-device test.')
        else:
            print('State-write checks skipped: require a dedicated account and SMOKE_WRITE_STATE=1.')
        query = os.environ.get('SMOKE_QUERY')
        if query:
            assert 'results' in request('/api/search', {'query': query})
    finally:
        request('/api/logout', {})
    request('/api/session', expected=401)
    request('/api/bootstrap', expected=401)


if __name__ == '__main__':
    main()

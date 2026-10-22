"""Opt-in live checks using disposable users only; credentials never leave memory."""
import concurrent.futures
import http.cookiejar
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import time
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, HTTPCookieProcessor, Request, build_opener


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args):
        return None


def main():
    if os.environ.get('LIVE_ALLOW_TEMP_USERS') != '1':
        raise SystemExit('Requires explicit LIVE_ALLOW_TEMP_USERS=1 and an authorized project.')
    ref = os.environ.get('LIVE_PROJECT_REF', '')
    base = os.environ.get('DEPLOYMENT_URL', '').rstrip('/')
    origin = urlsplit(base)
    if not ref.isalnum() or len(ref) != 20 or origin.scheme != 'https' or not origin.hostname or origin.username or origin.password or origin.path or origin.query or origin.fragment:
        raise SystemExit('Set LIVE_PROJECT_REF and a trusted HTTPS DEPLOYMENT_URL origin.')
    supabase = 'https://' + ref + '.supabase.co'
    cli = ['cmd.exe', '/d', '/c', 'npx.cmd', '--yes', 'supabase@2.121.0']
    keys_result = subprocess.run(cli + ['projects', 'api-keys', '--project-ref', ref, '--reveal', '--output', 'json'], capture_output=True, text=True, timeout=60)
    if keys_result.returncode:
        raise SystemExit('Official CLI could not load authorized API keys; no response printed.')
    records = json.loads(keys_result.stdout)
    if isinstance(records, dict):
        records = records.get('api_keys', records.get('keys', []))
    def key(kind):
        names = ['service_role', 'secret'] if kind == 'admin' else ['anon', 'publishable']
        for name in names:
            for item in records:
                if item.get('name') == name or item.get('type') == name:
                    value = item.get('api_key') or item.get('key')
                    if value and not value.endswith('...'):
                        return value
        raise RuntimeError('Required authorized key was not returned.')
    public, admin = key('public'), key('admin')
    anonymous = build_opener(NoRedirect())
    created = []
    phase = 'preflight'
    passed = []
    browser_only = os.environ.get('LIVE_BROWSER_ONLY') == '1'
    metrics_only = os.environ.get('LIVE_METRICS_ONLY') == '1'
    legacy_only = os.environ.get('LIVE_LEGACY_ONLY') == '1'

    def check(ok, label):
        if not ok:
            raise AssertionError(label)
        passed.append(label)
        print(json.dumps({'passed': label}), flush=True)

    def call(opener, url, data=None, headers=None, method=None, expected=200):
        request = Request(url, data=json.dumps(data).encode() if data is not None else None,
                          headers={'Content-Type': 'application/json', **(headers or {})}, method=method)
        try:
            response = opener.open(request, timeout=35)
        except HTTPError as error:
            response = error
        with response:
            raw = response.read(2 * 1024 * 1024 + 1)
            if len(raw) > 2 * 1024 * 1024:
                raise RuntimeError('Unexpected oversized response.')
            if expected is not None and response.code != expected:
                raise AssertionError('Unexpected HTTP status ' + str(response.code))
            body = json.loads(raw) if raw else None
            return response.code, body, response.headers

    def sb(path, data=None, token=None, admin_call=False, method=None, expected=200):
        api_key = admin if admin_call else public
        headers = {'apikey': api_key}
        if token:
            headers['Authorization'] = 'Bearer ' + token
        elif admin_call and not admin.startswith('sb_secret_'):
            headers['Authorization'] = 'Bearer ' + admin
        return call(anonymous, supabase + path, data, headers, method, expected)

    def save_record():
        file = os.environ.get('LIVE_FIXTURE_RECORD')
        if file:
            Path(file).write_text(json.dumps({'project': ref, 'users': [{'id': u['id'], 'username': u['username']} for u in created]}), encoding='utf-8')

    try:
        check(call(anonymous, base + '/api/health')[1].get('authRequired') is True, 'auth-required')
        call(anonymous, base + '/api/bootstrap', expected=401)
        phase = 'creating disposable users'
        for _ in range(1 if browser_only or metrics_only or legacy_only else 2):
            username = 'pn_probe_' + secrets.token_hex(5)
            password = secrets.token_urlsafe(30)
            email = username + '@users.pure-novel.invalid'
            result = sb('/auth/v1/admin/users', {'email': email, 'password': password, 'email_confirm': True,
                                              'user_metadata': {'username': username}}, admin_call=True)[1]
            user = {'id': result['id'], 'username': username, 'email': email, 'password': password}
            created.append(user)
            save_record()
            sb('/rest/v1/members', {'user_id': user['id'], 'active': True}, admin_call=True, expected=201)
            user['jar'] = http.cookiejar.CookieJar()
            user['client'] = build_opener(NoRedirect(), HTTPCookieProcessor(user['jar']))
            response = call(user['client'], base + '/api/login', {'username': username, 'password': password})[1]
            check(response['user']['id'] == user['id'], 'temporary-account-login-' + str(len(created)))
            check(len(user['jar']) == 2 and all(c.secure and c.has_nonstandard_attr('HttpOnly') for c in user['jar']), 'secure-cookies-' + str(len(created)))
            user['token'] = sb('/auth/v1/token?grant_type=password', {'email': email, 'password': password})[1]['access_token']
        a = created[0]
        if metrics_only:
            for index in range(3):
                _, state, headers = call(a['client'], base + '/api/bootstrap')
                check(state['user']['id'] == a['id'], 'metrics-account-identity-' + str(index + 1))
                print(json.dumps({'sample': index + 1, 'bootstrap_timing': headers.get('Server-Timing')}), flush=True)
            return 0
        if legacy_only:
            phase = 'legacy deployment compatibility'
            legacy = os.environ.get('LIVE_LEGACY_URL', '').rstrip('/')
            parsed_legacy = urlsplit(legacy)
            if parsed_legacy.scheme != 'https' or not parsed_legacy.hostname or parsed_legacy.username or parsed_legacy.password or parsed_legacy.path or parsed_legacy.query or parsed_legacy.fragment:
                raise RuntimeError('Set a trusted LIVE_LEGACY_URL origin.')
            legacy_client = build_opener(NoRedirect(), HTTPCookieProcessor(http.cookiejar.CookieJar()))
            logged = call(legacy_client, legacy + '/api/login', {'username': a['username'], 'password': a['password']})[1]
            check(logged['user']['id'] == a['id'], 'legacy-login-compatible')
            old = call(legacy_client, legacy + '/api/state')[1]
            fixture = {'books': [], 'settings': {'theme': 'paper'}}
            saved = call(legacy_client, legacy + '/api/state', {'version': old['version'], 'payload': fixture})[1]
            check(saved['ok'] and saved['version'] == old['version'] + 1, 'legacy-changed-save-compatible')
            repeated = call(legacy_client, legacy + '/api/state', {'version': saved['version'], 'payload': fixture})[1]
            check(repeated['version'] == saved['version'], 'legacy-idempotent-save-compatible')
            call(legacy_client, legacy + '/api/state', {'version': old['version'], 'payload': fixture}, expected=409)
            call(legacy_client, legacy + '/api/logout', {})
            call(legacy_client, legacy + '/api/session', expected=401)
            check(True, 'legacy-conflict-and-logout-compatible')
            return 0
        if not browser_only:
            b = created[1]
            phase = 'bootstrap and synchronization'
            _, state, headers = call(a['client'], base + '/api/bootstrap')
            check(state['version'] == 0 and state['payload'] is None, 'new-account-empty-state')
            check('cloud_calls;desc="2"' in headers.get('Server-Timing', ''), 'bootstrap-two-cloud-calls')
            payload = {'books': [], 'settings': {'theme': 'paper', 'size': 23, 'line': '1.95', 'font': 'serif'}}
            saved = call(a['client'], base + '/api/state', {'version': 0, 'payload': payload})[1]
            check(saved['version'] == 1, 'changed-state-increments-once')
            same = call(a['client'], base + '/api/state', {'version': 1, 'payload': payload})[1]
            check(same['version'] == 1, 'identical-save-is-no-op')
            call(a['client'], base + '/api/state', {'version': 0, 'payload': payload}, expected=409)
            check(call(b['client'], base + '/api/bootstrap')[1]['payload'] is None, 'second-account-isolated')
            rows = sb('/rest/v1/reader_states?select=version&user_id=eq.' + a['id'], token=b['token'])[1]
            check(rows == [], 'real-rls-cross-account-read-denied')
            sb('/rest/v1/reader_states?user_id=eq.' + a['id'], {'version': 999}, token=b['token'], method='PATCH', expected=403)
            check(call(a['client'], base + '/api/bootstrap')[1]['version'] == 1, 'cross-account-write-did-not-change-state')

            phase = 'refresh and membership'
            for cookie in list(a['jar']):
                if cookie.name == 'pn_access':
                    a['jar'].clear(cookie.domain, cookie.path, cookie.name)
            check(call(a['client'], base + '/api/bootstrap')[1]['user']['id'] == a['id'] and len(a['jar']) == 2, 'real-refresh-cookie-rotation')
            sb('/rest/v1/members?user_id=eq.' + b['id'], {'active': False}, admin_call=True, method='PATCH', expected=204)
            call(b['client'], base + '/api/bootstrap', expected=403)
            sb('/rest/v1/rpc/reader_context_v2', {}, token=b['token'], expected=403)
            sb('/rest/v1/members?user_id=eq.' + b['id'], {'active': True}, admin_call=True, method='PATCH', expected=204)
            check(True, 'inactive-member-blocked-by-server-and-rpc')

            phase = 'concurrent quota and compare-and-swap'
            def gate(_):
                return sb('/rest/v1/rpc/reader_gate_v2', {'p_route': 'search'}, token=a['token'], expected=None)[0]
            with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
                outcomes = list(pool.map(gate, range(12)))
            check(outcomes.count(200) == 6 and outcomes.count(429) == 6, 'real-multi-connection-quota-exactly-six')
            def cas(progress):
                candidate = {'books': [], 'settings': {**payload['settings'], 'size': progress}}
                return sb('/rest/v1/rpc/reader_save_v2', {'p_version': 1, 'p_payload': candidate}, token=a['token'])[1]
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                writes = list(pool.map(cas, [24, 25]))
            check(sorted(w['ok'] for w in writes) == [False, True] and all(w['version'] == 2 for w in writes), 'real-concurrent-cas-one-winner')

        if os.environ.get('LIVE_BROWSER') == '1':
            phase = 'real-browser offline and reconnect'
            from playwright.sync_api import sync_playwright
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(channel='msedge', headless=True)
                try:
                    context = browser.new_context(viewport={'width': 390, 'height': 844})
                    page = context.new_page()
                    traffic = []
                    def record_response(response):
                        path = urlsplit(response.url).path
                        if path.startswith('/api/'):
                            traffic.append({'path': path, 'status': response.status})
                    page.on('response', record_response)
                    cdp = context.new_cdp_session(page)
                    cdp.send('Network.enable')
                    def evaluate(expression):
                        result = cdp.send('Runtime.evaluate', {'expression': expression, 'returnByValue': True, 'awaitPromise': True})
                        if result.get('exceptionDetails'):
                            raise RuntimeError('Browser diagnostic expression failed.')
                        return result['result'].get('value')
                    def wait_js(expression, timeout=30):
                        deadline = time.monotonic() + timeout
                        while time.monotonic() < deadline:
                            if evaluate(expression):
                                return
                            time.sleep(0.1)
                        diagnostic = evaluate('({online:navigator.onLine,onlineEvents:globalThis.__liveOnlineEvents,dirty,syncing,syncBlocked,authChecking,localOffline,cloudVersion,retries:syncRetryCount,status:document.getElementById("account-status").textContent,conflict:document.getElementById("conflict-dialog").open,homeHidden:document.getElementById("home").hidden,loginError:document.getElementById("login-error").textContent})')
                        print(json.dumps({'browser_state': diagnostic, 'recent_api': traffic[-10:]}), flush=True)
                        raise TimeoutError('Browser state did not settle.')
                    phase = 'browser HTTPS navigation'
                    page.goto(base, wait_until='domcontentloaded')
                    phase = 'browser login'
                    page.locator('#username').fill(a['username'])
                    page.locator('#password').fill(a['password'])
                    page.locator('#login-submit').click()
                    page.locator('#home').wait_for(state='visible', timeout=45000)
                    phase = 'browser file import'
                    text = 'Chapter 1 Start\n' + ('Synthetic deployment reading paragraph.\n' * 100) + 'Chapter 2 Offline\n' + ('A second generated paragraph for offline reading.\n' * 100)
                    page.locator('#ebook-file').set_input_files({'name': 'deployment-offline.txt', 'mimeType': 'text/plain', 'buffer': text.encode()})
                    page.locator('#ebook-confirm').wait_for(state='visible')
                    title = 'Deployment offline fixture'
                    page.locator('#ebook-title').fill(title)
                    page.locator('#ebook-accept').click()
                    page.locator('#reader').wait_for(state='visible')
                    wait_js('!restoringProgress && !pending')
                    if os.environ.get('LIVE_THEME_CHECKS') == '1':
                        phase = 'browser real palette selection'
                        before = evaluate('({url:current.url,scroll:scrollY})')
                        page.locator('#settings-button').click()
                        for theme in ('paper', 'almond', 'green', 'mist', 'blue', 'night', 'blue'):
                            page.locator('#settings-dialog button[data-theme="' + theme + '"]').click()
                            if evaluate('document.body.dataset.theme') != theme:
                                raise AssertionError('Published palette did not render.')
                        page.locator('#settings-dialog .close-dialog').click()
                        after = evaluate('({url:current.url,scroll:scrollY})')
                        check(before['url'] == after['url'] and abs(before['scroll'] - after['scroll']) < 1, 'published-palettes-preserve-chapter-and-position')
                    page.locator('#back').click()
                    evaluate('pushState()')
                    wait_js('!dirty && !syncing', timeout=45)
                    wait_js('!!navigator.serviceWorker.controller')
                    page.locator('#shelf .book-open').filter(has_text=title).click()
                    wait_js('!restoringProgress && !pending')
                    phase = 'browser offline chapter navigation'
                    cdp.send('Network.emulateNetworkConditions', {'offline': True, 'latency': 0, 'downloadThroughput': -1, 'uploadThroughput': -1})
                    page.locator('#next').click()
                    wait_js('!restoringProgress && !pending')
                    evaluate('scrollTo(0,(document.documentElement.scrollHeight-innerHeight)*0.55)')
                    page.locator('#back').click()
                    phase = 'browser offline reload'
                    page.reload(wait_until='domcontentloaded')
                    page.locator('#home').wait_for(state='visible', timeout=30000)
                    check(page.locator('#offline-banner').is_visible(), 'real-cloud-offline-reload')
                    if os.environ.get('LIVE_THEME_CHECKS') == '1':
                        check(evaluate('document.body.dataset.theme') == 'blue', 'published-palette-persists-in-offline-reload')
                    page.locator('#shelf .book-open').filter(has_text=title).click()
                    wait_js('!restoringProgress && !pending')
                    check(page.locator('#chapter-title').inner_text() == 'Chapter 2 Offline', 'real-offline-last-chapter-restored')
                    phase = 'browser online revalidation and synchronization'
                    evaluate('globalThis.__liveOnlineEvents=0;addEventListener("online",()=>globalThis.__liveOnlineEvents++)')
                    cdp.send('Network.emulateNetworkConditions', {'offline': False, 'latency': 0, 'downloadThroughput': -1, 'uploadThroughput': -1})
                    wait_js('document.getElementById("offline-banner").hidden && !dirty && !syncing', timeout=60)
                    remote = sb('/rest/v1/rpc/reader_context_v2', {}, token=a['token'])[1]
                    restored = next(book for book in remote['payload']['books'] if book['title'] == title)
                    check(restored['lastUrl'].endswith('/1') and 0.45 < restored['progress'] < 0.65, 'real-offline-progress-synced-after-reauth')
                    if os.environ.get('LIVE_THEME_CHECKS') == '1':
                        check(remote['payload']['settings'].get('theme') == 'paper' and remote['payload']['settings'].get('dayPalette') == 'blue', 'published-palette-roundtrips-real-supabase')
                    context.close()
                finally:
                    browser.close()
        phase = 'logout'
        call(a['client'], base + '/api/logout', {})
        call(a['client'], base + '/api/bootstrap', expected=401)
        check(True, 'real-logout-clears-session')
        print(json.dumps({'result': 'passed', 'checks': len(passed), 'credentials_printed': False}), flush=True)
    except Exception as error:
        message = str(error)
        for secret in [public, admin] + [value for user in created for value in (user['password'], user.get('token', ''))]:
            if secret:
                message = message.replace(secret, '[REDACTED]')
        print(json.dumps({'result': 'failed', 'phase': phase, 'error_type': type(error).__name__, 'detail': message[:1000], 'completed_checks': len(passed)}), flush=True)
        return 1
    finally:
        failures = 0
        for user in created:
            try:
                sb('/auth/v1/admin/users/' + user['id'], admin_call=True, method='DELETE')
            except Exception:
                failures += 1
        if created:
            keys = [user['id'] + ':' + route for user in created for route in ('read', 'search', 'check', 'sync')]
            file = Path(os.environ.get('LIVE_FIXTURE_RECORD', str(Path(__file__).with_name('live-fixtures.local')))).with_suffix('.cleanup.sql')
            file.write_text('DELETE FROM private.reader_quotas WHERE key IN (' + ','.join("'" + value + "'" for value in keys) + ');', encoding='utf-8')
            result = subprocess.run(cli + ['db', 'query', '--project-ref', ref, '--linked', '--file', str(file), '--output', 'json'], capture_output=True, text=True, timeout=60)
            failures += int(result.returncode != 0)
            if not failures:
                file.unlink(missing_ok=True)
                record = os.environ.get('LIVE_FIXTURE_RECORD')
                if record:
                    Path(record).unlink(missing_ok=True)
        print(json.dumps({'temporary_users_cleanup_failures': failures, 'existing_accounts_modified': False}), flush=True)
        if failures:
            raise RuntimeError('Disposable fixture cleanup needs attention; IDs retained locally.')
    return 0


if __name__ == '__main__':
    sys.exit(main())

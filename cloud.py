"""Supabase gateway. Tokens remain in HttpOnly same-origin cookies."""
import hashlib
import json
import math
import os
import re
from decimal import Decimal
import observability
from http.cookies import SimpleCookie
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen
import request_budget as budget


class CloudError(Exception):
    def __init__(self, status, message):
        self.status = status
        super().__init__(message)


def enabled():
    return bool(os.environ.get('SUPABASE_URL'))


def required():
    return enabled() or bool(os.environ.get('VERCEL')) or os.environ.get('REQUIRE_AUTH') == '1'


def config():
    base = os.environ.get('SUPABASE_URL', '').rstrip('/')
    key = os.environ.get('SUPABASE_PUBLISHABLE_KEY', '')
    p = urlsplit(base)
    if p.scheme != 'https' or not p.hostname or p.path or p.query or p.fragment or p.username or p.port:
        raise CloudError(503, '账号服务配置无效，请联系管理员。')
    if not key:
        raise CloudError(503, '账号服务尚未配置完成。')
    return base, key


def rpc_v2_enabled():
    """Enable only after the compatible v2 migration has been applied explicitly."""
    return os.environ.get('READER_RPC_V2') == '1'


def remote(path, *, token=None, data=None, method=None, admin=False, deadline=None):
    kind = 'auth' if path.startswith('/auth/') else ('rpc' if path.startswith('/rest/v1/rpc/') else 'cloud')
    with observability.measure(kind):
        return _remote(path, token=token, data=data, method=method, admin=admin, deadline=deadline)


def _remote(path, *, token=None, data=None, method=None, admin=False, deadline=None):
    base, key = config()
    if admin:
        key = os.environ.get('SUPABASE_SECRET_KEY', '')
        if not key:
            raise CloudError(503, '服务端配额密钥尚未配置。')
    headers = {'apikey': key, 'Content-Type': 'application/json'}
    if token:
        headers['Authorization'] = 'Bearer ' + token
    elif admin and not key.startswith('sb_secret_'):
        headers['Authorization'] = 'Bearer ' + key
    payload = json.dumps(data, ensure_ascii=False, allow_nan=False).encode('utf-8') if data is not None else None
    request = Request(base + path, data=payload, headers=headers, method=method)
    connections = budget.Connections()

    def fetch():
        try:
            if deadline is not None:
                budget.remaining(deadline)
            response = (budget.open_url(request, deadline, connections) if deadline is not None
                        else urlopen(request, timeout=12))
            with response:
                raw = response.read(1024 * 1024 + 1)
                if deadline is not None:
                    budget.remaining(deadline)
                if len(raw) > 1024 * 1024:
                    raise CloudError(502, '云端响应过大。')
                result = json.loads(raw) if raw else {}
                if deadline is not None:
                    budget.remaining(deadline)
                return result
        except HTTPError as exc:
            status = exc.code
            # Older GoTrue versions use 400 for invalid_credentials/grant.
            if status == 400 and path.startswith('/auth/v1/token?grant_type='):
                try:
                    error = json.loads(exc.read(8192))
                except (OSError, ValueError):
                    error = {}
                code = error.get('error_code') or error.get('code') or error.get('error') if isinstance(error, dict) else None
                if code in ('invalid_credentials', 'invalid_grant', 'refresh_token_not_found', 'refresh_token_already_used'):
                    status = 401
            exc.close()
            messages = {
                400: '云端请求参数无效。',
                401: '登录已失效或凭据无效，请重新登录。',
                403: '账号未获邀请或已被停用，请联系管理员。',
                409: '其他设备已更新书架或进度，请选择要保留的版本。',
                413: '同步数据过大，请减少书架数量。',
                429: '账号服务请求过于频繁，请稍后再试。',
            }
            raise CloudError(status if status in messages else 503,
                             messages.get(status, '云端服务暂时不可用，请稍后重试。')) from None
        except budget.DeadlineExceeded:
            raise CloudError(504, '请求处理超时，请稍后重试。') from None
        except (URLError, OSError, ValueError):
            if deadline is not None and deadline <= budget.time.monotonic():
                raise CloudError(504, '请求处理超时，请稍后重试。') from None
            raise CloudError(503, '无法连接账号服务，请稍后重试。') from None

    if deadline is None:
        return fetch()
    future = None
    try:
        budget.remaining(deadline)
        future = budget.NETWORK_WORKERS.submit(fetch)
        return future.result(timeout=budget.remaining(deadline))
    except TimeoutError:
        raise CloudError(504, '请求处理超时，请稍后重试。') from None
    except budget.CapacityExceeded as exc:
        raise CloudError(503, str(exc)) from None
    finally:
        connections.cancel()
        if future is not None:
            future.cancel()


def with_budget(function, *args, deadline=None, **kwargs):
    if deadline is not None:
        try:
            budget.remaining(deadline)
        except budget.DeadlineExceeded:
            raise CloudError(504, '请求处理超时，请稍后重试。') from None
        kwargs['deadline'] = deadline
    result = function(*args, **kwargs)
    if deadline is not None:
        try:
            budget.remaining(deadline)
        except budget.DeadlineExceeded:
            raise CloudError(504, '请求处理超时，请稍后重试。') from None
    return result


def cookies(headers):
    try:
        jar = SimpleCookie()
        jar.load(headers.get('Cookie', ''))
        return {name: item.value for name, item in jar.items()}
    except Exception:
        return {}


def session_headers(session=None):
    secure = '; Secure' if os.environ.get('VERCEL') or os.environ.get('COOKIE_SECURE') == '1' else ''
    result = []
    try:
        values = (
            ('pn_access', session['access_token'] if session else '', max(0, min(int(session.get('expires_in', 3600)), 86400)) if session else 0),
            ('pn_refresh', session['refresh_token'] if session else '', 30 * 86400 if session else 0),
        )
    except (KeyError, TypeError, ValueError, OverflowError):
        raise CloudError(502, '账号服务返回无效会话。') from None
    for name, value, age in values:
        if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9._~-]*', value):
            raise CloudError(502, '账号服务返回无效会话。')
        result.append(('Set-Cookie', f'{name}={value}; Path=/api; HttpOnly; SameSite=Strict; Max-Age={age}{secure}'))
    return result


def identity(token, deadline=None):
    user = with_budget(remote, '/auth/v1/user', token=token, deadline=deadline)
    uid = user.get('id', '') if isinstance(user, dict) else ''
    if not isinstance(uid, str) or not re.fullmatch(r'[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}', uid):
        raise CloudError(401, '登录身份无效。')
    email = user.get('email', '')
    username = ''
    if isinstance(email, str) and email.endswith('@' + USERNAME_DOMAIN):
        candidate = email.split('@', 1)[0]
        if re.fullmatch(r'[a-z][a-z0-9_]{2,31}', candidate):
            username = candidate
        elif candidate.startswith('cn-'):
            metadata = user.get('user_metadata')
            name = metadata.get('username') if isinstance(metadata, dict) else None
            try:
                if username_email(name) == email:
                    username = normalize_username(name)
            except CloudError:
                pass
    return {'id': uid, 'username': username}


def member(token, deadline=None):
    user = with_budget(identity, token, deadline=deadline)
    rows = with_budget(remote, '/rest/v1/members?select=user_id&user_id=eq.' + user['id'] + '&active=eq.true',
                       token=token, deadline=deadline)
    if not rows:
        raise CloudError(403, '账号未获邀请或已被停用，请联系管理员。')
    return user


def authenticate(headers, outgoing, deadline=None, *, membership=True):
    validate = member if membership else identity
    jar = cookies(headers)
    token = jar.get('pn_access')
    if token:
        try:
            return with_budget(validate, token, deadline=deadline), token
        except CloudError as exc:
            if exc.status != 401:
                raise
    refresh = jar.get('pn_refresh')
    if not refresh:
        raise CloudError(401, '请先使用受邀账号登录。')
    session = with_budget(remote, '/auth/v1/token?grant_type=refresh_token',
                          data={'refresh_token': refresh}, deadline=deadline)
    token = session_token(session)
    user = with_budget(validate, token, deadline=deadline)
    outgoing.extend(session_headers(session))
    return user, token


def session_token(session):
    if not isinstance(session, dict) or not isinstance(session.get('access_token'), str) or not session['access_token']:
        raise CloudError(502, '账号服务返回无效会话。')
    return session['access_token']


def state_result(result):
    if (not isinstance(result, dict) or type(result.get('version')) is not int or result['version'] < 0
            or 'payload' not in result or result['payload'] is not None and not isinstance(result['payload'], dict)):
        raise CloudError(502, '云端返回无效书架。')
    return {'version': result['version'], 'payload': result['payload']}


def rpc_context(token, deadline=None):
    result = with_budget(remote, '/rest/v1/rpc/reader_context_v2', token=token, data={}, deadline=deadline)
    return state_result(result)


def context(headers, outgoing, deadline=None, *, include_state=True):
    pending = []
    v2 = rpc_v2_enabled()
    user, token = with_budget(authenticate, headers, pending, deadline=deadline, **({'membership': False} if v2 else {}))
    if v2:
        state = with_budget(rpc_context, token, deadline=deadline)
    elif include_state:
        rows = with_budget(remote, '/rest/v1/reader_states?select=version,payload', token=token, deadline=deadline)
        if not isinstance(rows, list):
            raise CloudError(502, '云端返回无效书架。')
        state = state_result(rows[0] if rows else {'version': 0, 'payload': None})
    else:
        state = {}
    outgoing.extend(pending)
    return {'user': user, **state}


def authorize(headers, outgoing, route, deadline=None):
    if not rpc_v2_enabled():
        user, token = with_budget(authenticate, headers, outgoing, deadline=deadline)
        with_budget(consume, user['id'], route, deadline=deadline)
        return user, token
    pending = []
    user, token = with_budget(authenticate, headers, pending, membership=False, deadline=deadline)
    try:
        allowed = with_budget(remote, '/rest/v1/rpc/reader_gate_v2', token=token, data={'p_route': route}, deadline=deadline)
        if allowed is not True:
            raise CloudError(429, '已达到当前请求额度，请稍后再试。')
    except CloudError as exc:
        if exc.status in (400, 429):
            outgoing.extend(pending)
        raise
    outgoing.extend(pending)
    return user, token


def save(headers, outgoing, data, deadline=None):
    if not rpc_v2_enabled():
        _, token = with_budget(authorize, headers, outgoing, 'sync', deadline=deadline)
        return with_budget(sync, token, data, deadline=deadline)
    validate_sync(data)
    pending = []
    _, token = with_budget(authenticate, headers, pending, membership=False, deadline=deadline)
    try:
        result = with_budget(sync, token, data, deadline=deadline)
    except CloudError as exc:
        # A business denial must not lose a rotated refresh token.
        if exc.status in (400, 409, 413, 429):
            outgoing.extend(pending)
        raise
    outgoing.extend(pending)
    return result


USERNAME_DOMAIN = 'users.pure-novel.invalid'


def normalize_username(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z\u3400-\u4dbf\u4e00-\u9fff][A-Za-z0-9_\u3400-\u4dbf\u4e00-\u9fff]{2,31}', value.strip()):
        raise CloudError(400, '用户名需为 3～32 位中文、英文字母、数字或下划线，且以中文或字母开头。')
    return value.strip().lower()


def username_email(value):
    """Internal Auth identifier only; never used to send email."""
    username = normalize_username(value)
    # Keep existing ASCII accounts unchanged; reserve the hyphenated namespace
    # for fixed-length Chinese identifiers within Auth's email length limits.
    local = username if username.isascii() else 'cn-' + hashlib.sha256(username.encode('utf-8')).hexdigest()[:56]
    return local + '@' + USERNAME_DOMAIN


def login(data, outgoing, deadline=None):
    email = username_email(data.get('username'))
    password = data.get('password')
    if not isinstance(password, str) or not 1 <= len(password) <= 256:
        raise CloudError(400, '请输入密码。')
    session = with_budget(remote, '/auth/v1/token?grant_type=password', data={'email': email, 'password': password}, deadline=deadline)
    token = session_token(session)
    if rpc_v2_enabled():
        user = with_budget(identity, token, deadline=deadline)
        with_budget(rpc_context, token, deadline=deadline)
    else:
        user = with_budget(member, token, deadline=deadline)
    outgoing.extend(session_headers(session))
    return {'user': user}


def consume(uid, route, deadline=None):
    allowed = with_budget(remote, '/rest/v1/rpc/consume_reader_quota', admin=True,
                          data={'p_user': uid, 'p_route': route}, deadline=deadline)
    if allowed is not True:
        raise CloudError(429, '已达到当前请求额度，请稍后再试。')


def payload_size(value):
    """UTF-8 bytes of PostgreSQL jsonb::text, including its separator spaces."""
    if isinstance(value, str):
        if '\x00' in value:
            raise ValueError('NUL is not supported by jsonb')
        return len(json.dumps(value, ensure_ascii=False).encode('utf-8'))
    if isinstance(value, dict):
        if any(not isinstance(key, str) for key in value):
            raise ValueError('object keys must be strings')
        return 2 + max(0, len(value) - 1) * 2 + sum(payload_size(key) + 2 + payload_size(item) for key, item in value.items())
    if isinstance(value, list):
        return 2 + max(0, len(value) - 1) * 2 + sum(payload_size(item) for item in value)
    if value is None:
        return 4
    if isinstance(value, bool):
        return 4 if value else 5
    if type(value) is int:
        return len(str(value))
    if type(value) is float and math.isfinite(value):
        number = Decimal(str(value))
        if not number:
            number = number.copy_abs()
        return len(format(number, 'f'))
    raise ValueError('not a JSON value')


def validate_sync(data):
    version, payload = data.get('version'), data.get('payload')
    if type(version) is not int or not 0 <= version <= 9223372036854775807 or not isinstance(payload, dict):
        raise CloudError(400, '同步数据格式无效。')
    if not isinstance(payload.get('books'), list) or len(payload['books']) > 200 or not isinstance(payload.get('settings'), dict):
        raise CloudError(400, '书架最多 200 本，且必须包含有效设置。')
    try:
        size = payload_size(payload)
    except (ValueError, UnicodeError, RecursionError):
        raise CloudError(400, '同步数据格式无效。') from None
    if size > 500000:
        raise CloudError(413, '同步数据过大，请减少书架数量。')
    return version, payload


def sync(token, data, deadline=None):
    version, payload = validate_sync(data)
    path = '/rest/v1/rpc/reader_save_v2' if rpc_v2_enabled() else '/rest/v1/rpc/save_reader_state'
    result = with_budget(remote, path, token=token, data={'p_version': version, 'p_payload': payload}, deadline=deadline)
    if not isinstance(result, dict) or type(result.get('ok')) is not bool or type(result.get('version')) is not int:
        raise CloudError(502, '云端返回无效同步结果。')
    if not result['ok']:
        raise CloudError(409, '其他设备已更新书架或进度，请选择要保留的版本。')
    return result
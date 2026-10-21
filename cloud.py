"""Supabase gateway. Tokens remain in HttpOnly same-origin cookies."""
import hashlib
import json
import os
import re
from http.cookies import SimpleCookie
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen


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


def remote(path, *, token=None, data=None, method=None, admin=False):
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
    payload = json.dumps(data).encode() if data is not None else None
    request = Request(base + path, data=payload, headers=headers, method=method)
    try:
        with urlopen(request, timeout=12) as response:
            raw = response.read(1024 * 1024 + 1)
            if len(raw) > 1024 * 1024:
                raise CloudError(502, '云端响应过大。')
            return json.loads(raw) if raw else {}
    except HTTPError as exc:
        if exc.code in (400, 401, 403):
            raise CloudError(401, '登录已失效、凭据无效或没有访问权限，请重新登录。') from None
        if exc.code == 429:
            raise CloudError(429, '账号服务请求过于频繁，请稍后再试。') from None
        raise CloudError(503, '云端服务暂时不可用，请稍后重试。') from None
    except (URLError, OSError, ValueError):
        raise CloudError(503, '无法连接账号服务，请稍后重试。') from None


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
    for name, value, age in (
        ('pn_access', session['access_token'] if session else '', min(int(session.get('expires_in', 3600)), 86400) if session else 0),
        ('pn_refresh', session['refresh_token'] if session else '', 30 * 86400 if session else 0),
    ):
        if not re.fullmatch(r'[A-Za-z0-9._~-]*', value):
            raise CloudError(502, '账号服务返回无效会话。')
        result.append(('Set-Cookie', f'{name}={value}; Path=/api; HttpOnly; SameSite=Strict; Max-Age={age}{secure}'))
    return result


def member(token):
    user = remote('/auth/v1/user', token=token)
    uid = user.get('id', '')
    if not re.fullmatch(r'[0-9a-fA-F-]{36}', uid):
        raise CloudError(401, '登录身份无效。')
    rows = remote('/rest/v1/members?select=user_id&user_id=eq.' + uid + '&active=eq.true', token=token)
    if not rows:
        raise CloudError(403, '账号未获邀请或已被停用，请联系管理员。')
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


def authenticate(headers, outgoing):
    jar = cookies(headers)
    token = jar.get('pn_access')
    if token:
        try:
            return member(token), token
        except CloudError as exc:
            if exc.status != 401:
                raise
    refresh = jar.get('pn_refresh')
    if not refresh:
        raise CloudError(401, '请先使用受邀账号登录。')
    session = remote('/auth/v1/token?grant_type=refresh_token', data={'refresh_token': refresh})
    token = session['access_token']
    user = member(token)
    outgoing.extend(session_headers(session))
    return user, token


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


def login(data, outgoing):
    email = username_email(data.get('username'))
    password = data.get('password')
    if not isinstance(password, str) or not 1 <= len(password) <= 256:
        raise CloudError(400, '请输入密码。')
    session = remote('/auth/v1/token?grant_type=password', data={'email': email, 'password': password})
    user = member(session['access_token'])
    outgoing.extend(session_headers(session))
    return {'user': user}


def consume(uid, route):
    allowed = remote('/rest/v1/rpc/consume_reader_quota', admin=True, data={'p_user': uid, 'p_route': route})
    if allowed is not True:
        raise CloudError(429, '已达到当前请求额度，请稍后再试。')


def sync(token, data):
    version, payload = data.get('version'), data.get('payload')
    if type(version) is not int or version < 0 or not isinstance(payload, dict):
        raise CloudError(400, '同步数据格式无效。')
    if not isinstance(payload.get('books'), list) or len(payload['books']) > 200 or not isinstance(payload.get('settings'), dict):
        raise CloudError(400, '书架最多 200 本，且必须包含有效设置。')
    if len(json.dumps(payload).encode()) > 500000:
        raise CloudError(413, '同步数据过大，请减少书架数量。')
    result = remote('/rest/v1/rpc/save_reader_state', token=token, data={'p_version': version, 'p_payload': payload})
    if not result.get('ok'):
        raise CloudError(409, '其他设备已更新书架或进度，请选择要保留的版本。')
    return result
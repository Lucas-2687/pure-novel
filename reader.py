"""Pure Novel: dependency-free web reader and bounded public-page extractor."""
from __future__ import annotations

import argparse
import base64
import hashlib
import http.client
import ipaddress
import http.cookiejar
import json
import re
import socket
import ssl
import threading
import time
import zlib
import cloud
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from html.parser import HTMLParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.request import Request
from pathlib import Path
import publications
import openbooks
from urllib.parse import parse_qs, quote, urljoin, urlsplit, urlunsplit

ROOT = Path(__file__).resolve().parent
MAX_BYTES = 3 * 1024 * 1024
MAX_PAGES = 8
CACHE: OrderedDict[str, tuple[float, dict]] = OrderedDict()
CACHE_LOCK = threading.Lock()
FETCH_SLOTS = threading.BoundedSemaphore(4)
SEARCH_ENGINES = {
    'Bing': 'https://www.bing.com/search?q=',
    '360': 'https://www.so.com/s?q=',
    '百度': 'https://www.baidu.com/s?wd=',
    '搜狗': 'https://www.sogou.com/web?query=',
}
SEARCH_CACHE = OrderedDict()


def failure_kind(message: str) -> str:
    if '403' in message or '验证' in message:
        return 'blocked'
    if '匹配' in message:
        return 'mismatch'
    if '正文' in message or '目录' in message or '解析' in message:
        return 'unsupported'
    return 'network'


def search_term(value: str) -> str:
    if not isinstance(value, str):
        raise ReaderError('请输入小说书名。')
    value = value.strip().strip('《》').strip()
    if not 2 <= len(value) <= 60 or not normalized_title(value) or re.search(r'[\x00-\x1f]', value):
        raise ReaderError('书名长度应为 2～60 个字符。')
    return value


def normalized_title(text: str) -> str:
    return re.sub(r'[\W_]+', '', text).casefold()


def non_reading_url(url: str) -> bool:
    p = urlsplit(url)
    host = p.hostname or ''
    return (host in ('baike.sogou.com', 'wenwen.sogou.com', 'zh.wikipedia.org', 'zhihu.com', 'www.zhihu.com')
            or host.startswith(('baike.', 'wenda.', 'zhidao.', 'tieba.'))
            or bool(re.search(r'/(?:ask|keyword)(?:/|$)', p.path)))


def reject_challenge(html: str):
    if re.search(r'cf-chl-|challenge-platform|g-recaptcha|h-captcha|cf-turnstile|<title[^>]*>[^<]*(?:Just a moment|安全验证|人机验证|访问验证|验证码)', html, re.I):
        raise ReaderError('源站要求人机验证，纯阅暂不支持；已跳过，不会自动绕过验证。')


def search_results(html: str, base: str, query: str, engine: str) -> list[dict]:
    results, seen = [], set()
    expected = normalized_title(query)
    for heading in Document(html).root.walk():
        if heading.tag not in ('h2', 'h3'):
            continue
        for anchor in heading.walk():
            if anchor.tag != 'a':
                continue
            title = tidy(anchor.text())
            if expected not in normalized_title(title):
                continue
            href = anchor.attrs.get('data-mdurl') or anchor.attrs.get('href', '')
            parent = heading
            for _ in range(4):
                if not parent:
                    break
                direct = parent.attrs.get('mu') or parent.attrs.get('data-url')
                if direct and direct.startswith(('https://', 'http://')):
                    href = direct
                    break
                parent = parent.parent
            target = safe_link(base, href)
            if target and urlsplit(target).hostname in ('www.bing.com', 'cn.bing.com'):
                encoded = parse_qs(urlsplit(target).query).get('u', [''])[0]
                if encoded.startswith('a1'):
                    try:
                        payload = encoded[2:]
                        target = safe_link(base, base64.urlsafe_b64decode(payload + '=' * (-len(payload) % 4)).decode())
                    except (ValueError, UnicodeError):
                        target = None
            if not target or target in seen:
                continue
            host = urlsplit(target).hostname
            if (host in ('www.sogou.com', 'www.so.com', 'www.baidu.com')
                    and urlsplit(target).path == '/link' and urlsplit(target).query):
                if target not in seen:
                    seen.add(target)
                    results.append({'title': title[:200], 'url': target, 'host': host,
                                    'engines': [engine], 'redirect': True})
                if len(results) == 8:
                    return results
                continue
            if (non_reading_url(target) or host in ('www.bing.com', 'cn.bing.com', 'www.so.com', 'ai.so.com', 'wenda.so.com', 'baike.so.com', 'tieba.baidu.com',
                         'www.baidu.com', 'www.sogou.com', 'zhidao.baidu.com', 'baike.baidu.com')
                    or re.search(r'/(?:ask|keyword)(?:/|$)', urlsplit(target).path)):
                continue
            seen.add(target)
            results.append({'title': title[:200], 'url': target, 'host': host, 'engines': [engine]})
            if len(results) == 8:
                return results
    return results


def find_books(query: str, round: int = 0) -> dict:
    query = search_term(query)
    if type(round) is not int or not 0 <= round <= 2:
        raise ReaderError('搜索轮次必须为 0～2。')
    key = (query, round)
    with CACHE_LOCK:
        hit = SEARCH_CACHE.get(key)
        if hit and time.monotonic() - hit[0] < 120:
            return {**hit[1], 'cached': True}
    suffix = (' 小说 目录', ' 小说 最新章节', ' 全文阅读 章节目录')[round]

    def run(item):
        name, endpoint = item
        try:
            html, final = fetch_page(endpoint + quote('"' + query + '"' + suffix))
            if re.search(r'<title[^>]*>[^<]*(?:验证码|安全验证|访问验证|访问受限|人机验证)', html, re.I):
                raise ReaderError('搜索入口要求验证，未尝试绕过。')
            entries = search_results(html, final, query, name)
            unresolved = [e for e in entries if e.get('redirect')][:4]
            def resolve(entry):
                try:
                    _, target = fetch_page(entry['url'])
                    host = urlsplit(target).hostname
                    if host in ('www.sogou.com', 'www.so.com', 'www.baidu.com') or non_reading_url(target):
                        return None, '中转链接未到达可用的阅读网站。'
                    return {**entry, 'url': target, 'host': host, 'redirect': False}, ''
                except (ReaderError, OSError, http.client.HTTPException) as exc:
                    return None, str(exc)[:120]
            redirect_errors = []
            if unresolved:
                with ThreadPoolExecutor(max_workers=4) as resolver:
                    outcomes = list(resolver.map(resolve, unresolved))
                    resolved = [e for e, error in outcomes if e]
                    redirect_errors = list(dict.fromkeys(error for e, error in outcomes if error))
                entries = [e for e in entries if not e.get('redirect')] + resolved
            if not entries and redirect_errors:
                return [], {'engine': name, 'state': 'redirect_failed', 'count': 0,
                            'message': '已发现匹配标题，但中转解析失败：' + '；'.join(redirect_errors)[:160]}
            return entries, {'engine': name, 'state': 'ok' if entries else 'empty_or_unparsed',
                             'count': len(entries), 'message': '' if entries else '未提取到匹配结果：可能无匹配、跳转链接未支持或页面结构变化。'}
        except (ReaderError, OSError, http.client.HTTPException) as exc:
            return [], {'engine': name, 'state': failure_kind(str(exc)), 'count': 0, 'message': str(exc)[:200]}

    merged, warnings, groups, statuses = {}, [], [], []
    with ThreadPoolExecutor(max_workers=4) as pool:
        for entries, warning in pool.map(run, SEARCH_ENGINES.items()):
            statuses.append(warning)
            if warning['state'] != 'ok':
                warnings.append(warning)
            groups.append(entries)
    # Interleave engines so the first page is not monopolized by one index.
    for i in range(max((len(group) for group in groups), default=0)):
        for group in groups:
            if i < len(group):
                entry = group[i]
                if entry['url'] in merged:
                    merged[entry['url']]['engines'].extend(entry['engines'])
                else:
                    merged[entry['url']] = entry
    result = {'query': query, 'round': round, 'hasMore': round < 2,
              'results': list(merged.values())[:20], 'warnings': warnings, 'engines': statuses,
              'sites': len({entry['host'].removeprefix('www.').removeprefix('m.') for entry in merged.values()}),
              'note': '搜索结果不代表可读或获得授权；样本检查不保证全书完整。'}
    with CACHE_LOCK:
        SEARCH_CACHE[key] = (time.monotonic(), result)
        while len(SEARCH_CACHE) > 64:
            SEARCH_CACHE.popitem(last=False)
    return result


def check_source(query: str, url: str) -> dict:
    query = search_term(query)
    url = reading_url(url)
    if non_reading_url(url):
        raise ReaderError('百科、问答等非阅读页面，已排除。')
    html, final = fetch_page(url)
    reject_challenge(html)
    try:
        page = parse_page(html, final)
    except ReaderError as original:
        # Follow only explicit reading entries on a matching book introduction.
        if '没有识别到正文或目录' not in str(original):
            raise
        nodes = list(Document(html).root.walk())
        heading = ' '.join(n.text() for n in nodes if n.tag in ('h1', 'title'))
        if normalized_title(query) not in normalized_title(heading):
            raise ReaderError('页面未匹配目标书籍，未继续查找阅读入口。') from None
        targets = []
        for n in nodes:
            if n.tag != 'a' or not re.fullmatch(r'(?:查看|全部|进入|小说|章节|立即|开始|点击|免费|在线|正文|返回|最新)*\s*(?:目录|章节列表|阅读)(?:全部|章节)?', tidy(n.text())):
                continue
            link = safe_link(final, n.attrs.get('href', ''))
            if link and same_site(final, link) and link != final and link not in targets:
                targets.append(link)
        page = None
        for link in targets[:2]:
            try:
                child, resolved = fetch_page(link)
                if not same_site(final, resolved):
                    continue
                reject_challenge(child)
                candidate = parse_page(child, resolved)
                if normalized_title(query) in normalized_title(candidate['bookTitle']):
                    page, final = candidate, resolved
                    break
            except ReaderError as exc:
                if '验证' in str(exc) or 'HTTP 403' in str(exc):
                    raise
        if page is None:
            raise ReaderError('已匹配书籍介绍页，但未找到可解析的同站目录或阅读入口。') from None
    expected = normalized_title(query)
    if expected not in normalized_title(page['bookTitle']):
        raise ReaderError('页面书名与搜索书名不匹配，未作为可读来源。')
    if page['kind'] == 'catalog':
        if not page['chapters']:
            raise ReaderError('没有找到可验证的章节。')
        sample_entry = next((c for c in page['chapters'] if not re.search(r'新书|感言|请假|公告|求票|更新说明', c['title'])), None)
        if not sample_entry:
            raise ReaderError('目录仅含公告或新书推荐，未作为正文样本。')
        sample_url = sample_entry['url']
        html, resolved = fetch_page(sample_url)
        if not same_site(final, resolved):
            raise ReaderError('样本章节跳转至其他站点，已停止验证。')
        reject_challenge(html)
        sample = parse_page(html, resolved)
        if sample['kind'] != 'chapter':
            raise ReaderError('样本未返回可读正文。')
        if normalized_title(chapter_key(sample_entry['title'])) != normalized_title(chapter_key(sample['title'])):
            raise ReaderError('目录与正文样本章节标题不匹配，未作为可读来源。')
        if sample['bookTitle'] != '未命名小说' and expected not in normalized_title(sample['bookTitle']):
            raise ReaderError('样本章节书名不匹配。')
    else:
        sample, resolved = page, final
    # Validate complete sample including pagination, not just its first page.
    if sample.get('nextPage'):
        sample = load_reading(resolved)
        if sample['warnings']:
            raise ReaderError('样本章节分页未完整合并，未作为可读来源。')
    return {'url': final, 'bookTitle': page['bookTitle'], 'sampleTitle': sample['title'],
            'author': page.get('author', ''),
            'sampleUrl': resolved, 'characters': sum(map(len, sample['paragraphs'])),
            'chapters': len(page['chapters']), 'kind': page['kind'],
            'note': (page.get('accessNote') or '目录已识别；一个完整章节样本通过，不保证全书完整。' if page['kind'] == 'catalog'
                     else '仅当前章节样本通过，未验证目录及全书完整性。')}


class ReaderError(Exception):
    pass


def validate_url(raw: str) -> str:
    if not isinstance(raw, str) or len(raw) > 2048 or re.search(r'[\x00-\x20\\]', raw):
        raise ReaderError('网址无效，请粘贴完整的 http:// 或 https:// 链接。')
    try:
        p = urlsplit(raw)
        if p.scheme not in ('http', 'https') or not p.hostname or p.username or p.password:
            raise ValueError()
        if p.port not in (None, 80 if p.scheme == 'http' else 443):
            raise ValueError()
        host = p.hostname.encode('idna').decode('ascii').lower().rstrip('.')
        if not host or host == 'localhost' or host.endswith(('.local', '.localhost')) or '%' in host:
            raise ValueError()
        netloc = f'[{host}]' if ':' in host else host
        return urlunsplit((p.scheme, netloc, p.path or '/', p.query, ''))
    except (ValueError, UnicodeError):
        raise ReaderError('只支持不含账号信息、使用标准端口的 HTTP/HTTPS 公网网址。') from None


def public_addresses(host: str, port: int) -> list[str]:
    try:
        addresses = list(dict.fromkeys(x[4][0] for x in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)))
    except OSError:
        raise ReaderError('域名解析失败，请检查网址或网络。') from None
    if not addresses:
        raise ReaderError('域名没有可用地址。')
    for address in addresses:
        ip = ipaddress.ip_address(address)
        mapped = getattr(ip, 'ipv4_mapped', None)
        if not ip.is_global or (mapped and not mapped.is_global):
            raise ReaderError('出于安全考虑，不允许访问本机、内网或保留地址。')
    return addresses


class PinnedConnection(http.client.HTTPConnection):
    def __init__(self, host: str, ip: str, secure: bool):
        super().__init__(host, 443 if secure else 80, timeout=12)
        self.ip, self.secure = ip, secure

    def connect(self):
        self.sock = socket.create_connection((self.ip, self.port), self.timeout)
        if self.secure:
            self.sock = ssl.create_default_context().wrap_socket(self.sock, server_hostname=self.host)


def fetch_page(raw: str) -> tuple[str, str]:
    url = validate_url(raw)
    deadline = time.monotonic() + 24
    cookies = http.cookiejar.CookieJar()
    visited = set()
    for _ in range(5):
        if time.monotonic() >= deadline:
            raise ReaderError('网页请求超时，请稍后重试或更换来源。')
        p = urlsplit(url)
        addresses = public_addresses(p.hostname, 443 if p.scheme == 'https' else 80)
        response = None
        conn = None
        for ip in addresses[:3]:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            conn = PinnedConnection(p.hostname, ip, p.scheme == 'https')
            conn.timeout = min(6, remaining)
            try:
                request = Request(url)
                cookies.add_cookie_header(request)
                # Include cookie state: a redirect to the same URL can legitimately
                # establish a session once, but an unchanged cycle must stop early.
                marker = (url, request.get_header('Cookie', ''))
                headers = {
                    'User-Agent': 'PureNovelReader/0.1 (personal reading; no bulk download)',
                    'Accept': 'text/html,application/xhtml+xml', 'Accept-Encoding': 'identity',
                }
                if request.has_header('Cookie'):
                    headers['Cookie'] = request.get_header('Cookie')
                conn.request('GET', p.path + ('?' + p.query if p.query else ''), headers=headers)
                response = conn.getresponse()
                cookies.extract_cookies(response, request)
                break
            except (OSError, http.client.HTTPException):
                conn.close()
        if response is None:
            raise ReaderError('无法连接源站：可能是网络、TLS 或源站服务问题。请尝试在浏览器打开原网址。')
        try:
            if response.status in (301, 302, 303, 307, 308):
                if marker in visited:
                    raise ReaderError('搜索或源站跳转循环，入口当前不可用；不是书籍不存在。')
                visited.add(marker)
                location = response.getheader('Location')
                if not location:
                    raise ReaderError('源站返回了无效的跳转。')
                new = validate_url(urljoin(url, location))
                if p.scheme == 'https' and urlsplit(new).scheme != 'https':
                    raise ReaderError('已阻止从 HTTPS 降级到 HTTP 的跳转。')
                url = new
                continue
            if response.status != 200:
                if response.getheader('Cf-Mitigated', '').lower() == 'challenge':
                    raise ReaderError('源站要求人机验证（HTTP %s），纯阅暂不支持；未尝试绕过。' % response.status)
                raise ReaderError(f'源站返回 HTTP {response.status}，无法读取；不会绕过登录或访问限制。')
            mime = response.getheader('Content-Type', '')
            if not any(t in mime.lower() for t in ('text/html', 'application/xhtml+xml')):
                raise ReaderError('该链接不是 HTML 网页。')
            compression = response.getheader('Content-Encoding', 'identity').lower()
            if compression not in ('identity', 'gzip', 'deflate'):
                raise ReaderError('源站返回了暂不支持的压缩格式。')
            data = bytearray()
            while True:
                if time.monotonic() > deadline:
                    raise ReaderError('网页读取超时，请稍后再试。')
                chunk = response.read1(min(65536, MAX_BYTES + 1 - len(data)))
                if not chunk:
                    break
                data.extend(chunk)
                if len(data) > MAX_BYTES:
                    raise ReaderError('网页过大，已停止读取。')
            if compression != 'identity':
                data = bytearray(decompress_bounded(bytes(data), compression))
            encoding = re.search(r'charset\s*=\s*["\']?([\w-]+)', mime, re.I)
            if not encoding:
                encoding = re.search(r'charset\s*=\s*["\']?([\w-]+)', bytes(data[:4096]).decode('ascii', 'ignore'), re.I)
            codec = encoding.group(1) if encoding else 'utf-8'
            if codec.lower() in ('gb2312', 'gbk'):
                codec = 'gb18030'
            try:
                return bytes(data).decode(codec, 'replace'), url
            except LookupError:
                raise ReaderError('源站使用了无法识别的文字编码。') from None
        finally:
            conn.close()
    raise ReaderError('网页重定向次数过多。')


def decompress_bounded(data: bytes, compression: str) -> bytes:
    try:
        decoder = zlib.decompressobj(16 + zlib.MAX_WBITS if compression == 'gzip' else zlib.MAX_WBITS)
        result = decoder.decompress(data, MAX_BYTES + 1)
        if len(result) > MAX_BYTES or decoder.unconsumed_tail:
            raise ReaderError('解压后的网页过大，已停止读取。')
        if not decoder.eof:
            raise ReaderError('源站返回了不完整的压缩网页。')
        return result
    except zlib.error:
        raise ReaderError('源站返回了无效的压缩网页。') from None


class Node:
    def __init__(self, tag='', attrs=None, parent=None):
        self.tag, self.attrs, self.parent = tag, dict(attrs or []), parent
        self.children: list[Node | str] = []

    def walk(self):
        yield self
        for child in self.children:
            if isinstance(child, Node):
                yield from child.walk()

    def text(self, clean=False):
        if self.tag in ('script', 'style', 'noscript', 'iframe', 'svg', 'form', 'button'):
            return ''
        if clean and self.tag in ('a', 'nav', 'aside', 'header', 'footer'):
            return ''
        if clean and re.search(r'(?:^|[\s_-])(ad|ads|advert|recommend|banner)(?:$|[\s_-])',
                               self.attrs.get('class', '') + ' ' + self.attrs.get('id', ''), re.I):
            return ''
        if self.tag == 'br':
            return '\n'
        result = ''.join(c.text(clean) if isinstance(c, Node) else c for c in self.children)
        return result + ('\n' if self.tag in ('p', 'div', 'section', 'article', 'li', 'h1', 'h2') else '')


class Document(HTMLParser):
    VOID = {'br', 'img', 'meta', 'link', 'hr', 'input', 'source', 'wbr', 'area', 'base', 'embed', 'param', 'col', 'track'}

    def __init__(self, html: str):
        super().__init__(convert_charrefs=True)
        self.root = Node()
        self.current = self.root
        self.count = 0
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        self.count += 1
        if self.count > 70000:
            raise ReaderError('网页结构过于复杂，已停止解析。')
        node = Node(tag, attrs, self.current)
        self.current.children.append(node)
        if tag not in self.VOID:
            depth, p = 0, self.current
            while p:
                depth, p = depth + 1, p.parent
            if depth > 150:
                raise ReaderError('网页嵌套过深。')
            self.current = node

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in self.VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        node = self.current
        while node.parent:
            if node.tag == tag:
                self.current = node.parent
                return
            node = node.parent

    def handle_data(self, data):
        self.current.children.append(data)


def tidy(text: str) -> str:
    return re.sub(r'\s+', ' ', text).strip()


def safe_link(base: str, href: str) -> str | None:
    if not href or href.startswith(('#', 'javascript:', 'data:')):
        return None
    try:
        return validate_url(urljoin(base, href))
    except ReaderError:
        return None


def same_site(a: str, b: str) -> bool:
    return urlsplit(a).netloc == urlsplit(b).netloc


def chapter_key(title: str) -> str:
    m = re.search(r'第\s*[零〇一二三四五六七八九十百千万两\d]+\s*[章回节集].*', title)
    text = m.group(0) if m else title
    return re.sub(r'[（(]\s*\d+\s*[/／]\s*\d+\s*[）)]|\s+', '', text).split('_')[0].split('|')[0]


def reading_url(url: str) -> str:
    """Expand only the supported official mobile book catalog, not arbitrary URLs."""
    p = urlsplit(url)
    if p.hostname == 'wap.jjwxc.net':
        if re.fullmatch(r'/vip/\d+/\d+/?', p.path):
            raise ReaderError('该章节为晋江 VIP 章节，请前往原站登录并按要求订阅。')
        if re.fullmatch(r'/book2/\d+/?', p.path):
            return urlunsplit((p.scheme, p.netloc, p.path.rstrip('/'), 'more=0&whole=1', ''))
    return url


def parse_jjwxc(nodes: list[Node], url: str) -> dict | None:
    p = urlsplit(url)
    if p.hostname != 'wap.jjwxc.net':
        return None
    match = re.fullmatch(r'/book2/(\d+)(?:/(\d+))?/?', p.path)
    if not match:
        return None
    book_id, number = match.groups()
    page_title = next((tidy(n.text()) for n in nodes if n.tag == 'title'), '')
    identity = re.search(r'《(.+?)》([^_^]+)', page_title)
    if not identity:
        raise ReaderError('晋江作品信息未识别，页面结构可能已经改变。')
    book_title, author = identity.groups()
    catalog = f'https://wap.jjwxc.net/book2/{book_id}?more=0&whole=1'
    result = {'kind': 'chapter' if number else 'catalog', 'url': url, 'title': book_title,
              'bookTitle': book_title, 'author': author.strip(), 'paragraphs': [], 'chapters': [],
              'previous': None, 'next': None, 'nextPage': None, 'catalog': catalog, 'catalogNext': None}
    if not number:
        seen = set()
        for n in nodes:
            if n.tag != 'a':
                continue
            link = safe_link(url, n.attrs.get('href', ''))
            if not link or not same_site(url, link):
                continue
            entry = re.fullmatch(rf'/(book2|vip)/{book_id}/(\d+)', urlsplit(link).path)
            if not entry or entry[2] in seen:
                continue
            seen.add(entry[2])
            # Raw NBSP separates chapter title from the teaser on official pages.
            raw = n.text().split('\xa0')[0]
            label = re.sub(r'^\s*\d+\s*[.、]\s*', '', tidy(raw))
            result['chapters'].append({'title': f'第{entry[2]}章 {label}', 'url': link,
                                       'locked': entry[1] == 'vip', 'number': int(entry[2])})
        result['chapters'].sort(key=lambda c: c['number'])
        if not result['chapters']:
            raise ReaderError('晋江目录为空或结构已改变。')
        locked = [c for c in result['chapters'] if c['locked']]
        ordinary = len(result['chapters']) - len(locked)
        result['accessNote'] = f'目录 {len(result["chapters"])} 章，其中 {ordinary} 个普通章节链接、{len(locked)} 个 VIP 链接；普通章节未逐章验证。'
        return result
    body = next((n for n in nodes if n.tag == 'ul' and 'content_ul' in n.attrs.get('class', '').split()), None)
    if not body:
        raise ReaderError('晋江公开正文未识别，可能需要登录、订阅或页面结构已改变。')
    paragraphs = [tidy(line) for line in body.text(clean=True).splitlines() if tidy(line)]
    if sum(map(len, paragraphs)) < 180:
        raise ReaderError('晋江章节正文为空或过短，未作为完整章节。')
    heading = next((tidy(n.text()) for n in nodes if n.tag == 'h2'), '')
    heading = re.sub(r'^\d+\s*[、.]\s*', '', heading).split(' ')[0]
    if not heading:
        raise ReaderError('晋江章节标题未识别。')
    result['title'] = f'第{number}章 {heading}'
    result['paragraphs'] = paragraphs
    for n in nodes:
        if n.tag != 'a':
            continue
        label = tidy(n.text())
        if label not in ('上一章', '下一章'):
            continue
        link = safe_link(url, n.attrs.get('href', ''))
        if link and same_site(url, link) and re.fullmatch(rf'/(book2|vip)/{book_id}/\d+', urlsplit(link).path):
            if '/vip/' in urlsplit(link).path and label == '下一章':
                result['accessNote'] = '下一章为 VIP 章节，请前往原站按要求订阅。'
                result['restrictedNext'] = link
            elif '/vip/' not in urlsplit(link).path:
                result['previous' if label == '上一章' else 'next'] = link
    return result


def parse_page(html: str, url: str) -> dict:
    reject_challenge(html)
    nodes = list(Document(html).root.walk())
    specialized = parse_jjwxc(nodes, url)
    if specialized is not None:
        return specialized
    metas = {n.attrs.get('property', n.attrs.get('name', '')): n.attrs.get('content', '') for n in nodes if n.tag == 'meta'}
    title = next((tidy(n.text()) for n in nodes if n.tag == 'h1'), '')
    page_title = next((tidy(n.text()) for n in nodes if n.tag == 'title'), '')
    title = title or page_title.split('_')[0].split('|')[0]
    book = metas.get('og:novel:book_name', '')
    author = metas.get('og:novel:author', '')
    links = []
    for n in nodes:
        if n.tag == 'a':
            link = safe_link(url, n.attrs.get('href', ''))
            if link and same_site(url, link):
                links.append((tidy(n.text()), link))
    nav = {'previous': None, 'next': None, 'nextPage': None, 'catalog': None, 'catalogNext': None}
    for text, link in links:
        if link == url:
            continue
        if re.fullmatch(r'[←<\s]*上[一]?章[→>\s]*', text):
            nav['previous'] = link
        elif re.fullmatch(r'[←<\s]*下[一]?章[→>\s]*', text):
            nav['next'] = link
        elif re.fullmatch(r'[←<\s]*下[一]?页[→>\s]*', text):
            nav['nextPage'] = link
        elif text in ('目录', '章节列表', '全部章节', '返回目录'):
            nav['catalog'] = link

    # Prefer explicit body containers; never use the longest text of the entire page.
    ids = {'content', 'chaptercontent', 'chapter-content', 'booktext', 'txt', 'nr1'}
    candidates = [n for n in nodes if n.attrs.get('id', '').lower() in ids]
    if not candidates:
        candidates = [n for n in nodes if n.tag == 'article' or set(n.attrs.get('class', '').lower().split()) &
                      {'read-content', 'chapter-content', 'book-content', 'content_read', 'readcontent'}]
    body = max(candidates, key=lambda n: len(n.text(clean=True)), default=None)
    paragraphs = []
    if body:
        for line in body.text(clean=True).splitlines():
            line = tidy(line.replace('\u200b', ''))
            if not line or re.match(r'^(如果此书侵犯|喜欢.{0,50}请大家收藏|本章未完|请收藏本站|本章结束|最新网址|手机用户请|《.*》无错的章节将持续)', line):
                continue
            paragraphs.append(line)
    text = ''.join(paragraphs)
    if text and sum(0xE000 <= ord(c) <= 0xF8FF for c in text) / len(text) > .02:
        raise ReaderError('正文含大量特殊编码字符，暂不支持该站的文字渲染方式。')
    if body and len(text) < 180:
        raise ReaderError('该章节正文为空或过短，可能未更新、需要动态加载或受访问限制。')
    if len(text) > 100 and any(s in text for s in ('请登录后阅读', '购买本章', '订阅后阅读', '验证您是真人')):
        raise ReaderError('源站要求登录、购买或验证，阅读器不会绕过这些限制。')

    chapters = []
    catalog_links = links
    # Prefer a full catalog list. Keep the last occurrence of duplicate links:
    # many sites put a newest-first block before the actual ascending list.
    lists = [n for n in nodes if n.tag in ('dl', 'ul')]
    if lists and not body:
        longest = max(lists, key=lambda n: sum(x.tag == 'a' for x in n.walk()))
        anchors = [n for n in longest.walk() if n.tag == 'a']
        if len(anchors) > 20:
            catalog_links = [(tidy(n.text()), safe_link(url, n.attrs.get('href', ''))) for n in anchors]
            catalog_links = [(label, link) for label, link in catalog_links if link and same_site(url, link)]
    last_occurrence = {link: i for i, (_, link) in enumerate(catalog_links)}
    for i, (label, link) in enumerate(catalog_links):
        is_chapter = re.match(r'^(第[零〇一二三四五六七八九十百千万两\d]+[章回节集]|序章|楔子|后记|尾声|番外)', label)
        catalog_entry = (catalog_links is not links and urlsplit(link).path.startswith(urlsplit(url).path)
                         and urlsplit(link).path.endswith('.html') and '下载' not in label)
        if (is_chapter or catalog_entry) and last_occurrence[link] == i and 0 < len(label) < 160:
            chapters.append({'title': label, 'url': link})
    is_chapter = bool(body)
    if not is_chapter and not chapters:
        raise ReaderError('没有识别到正文或目录。请导入具体书籍目录页或章节页；该站也可能需要专门适配。')
    if not is_chapter:
        nav['catalogNext'] = nav['nextPage']
        nav['nextPage'] = None
    if not book:
        if urlsplit(url).hostname == 'book.qq.com':
            book = next((tidy(n.text()) for n in nodes if n.tag == 'a'
                         and 'book-title' in n.attrs.get('class', '').split()
                         and '/book-detail/' in n.attrs.get('href', '')), '')
    if not book:
        if not is_chapter:
            book = re.split(r'全文阅读|最新章节|小说全文|目录|_', title)[0].strip()
        else:
            parts = re.split(r'[_|]', page_title)
            book = parts[1].strip() if len(parts) > 1 else '未命名小说'
    numbers = [re.match(r'^第(\d+)章', c['title']) for c in chapters]
    if len(numbers) > 1 and all(numbers):
        values = [int(m.group(1)) for m in numbers]
        if all(a > b for a, b in zip(values, values[1:])):
            chapters.reverse()
    return {'kind': 'chapter' if is_chapter else 'catalog', 'url': url, 'title': title,
            'bookTitle': book or title, 'author': author, 'paragraphs': paragraphs,
            'chapters': chapters, **nav}


def load_reading(raw: str) -> dict:
    url = reading_url(validate_url(raw))
    with CACHE_LOCK:
        cached = CACHE.get(url)
        if cached and time.monotonic() - cached[0] < 300:
            CACHE.move_to_end(url)
            return cached[1]
    html, final = fetch_page(url)
    result = parse_page(html, final)
    result['warnings'] = []
    result['pages'] = 1
    if result['kind'] == 'chapter':
        visited = {final}
        hashes = {hashlib.sha256('\n'.join(result['paragraphs']).encode()).hexdigest()}
        while result['nextPage']:
            target = result['nextPage']
            if target in visited or len(visited) >= MAX_PAGES:
                result['warnings'].append('章节分页循环或超过 8 页，已停止合并；可打开原网页检查。')
                break
            if not same_site(final, target):
                raise ReaderError('章节分页指向其他站点，已停止读取。')
            try:
                html, resolved = fetch_page(target)
                if not same_site(final, resolved) or resolved in visited:
                    raise ReaderError('分页发生异常跳转。')
                page = parse_page(html, resolved)
                if page['kind'] != 'chapter' or chapter_key(page['title']) != chapter_key(result['title']):
                    raise ReaderError('分页章节标题不一致。')
                digest = hashlib.sha256('\n'.join(page['paragraphs']).encode()).hexdigest()
                if digest in hashes:
                    raise ReaderError('分页正文重复。')
                hashes.add(digest)
                visited.add(resolved)
                result['paragraphs'].extend(page['paragraphs'])
                result['nextPage'] = page['nextPage']
                result['next'] = page['next']
                result['pages'] += 1
            except (ReaderError, OSError, http.client.HTTPException):
                result['warnings'].append('后续分页未能完整合并，请打开原网页核对；当前内容可能不完整。')
                break
    if not result['warnings']:
        with CACHE_LOCK:
            CACHE[url] = (time.monotonic(), result)
            while len(CACHE) > 64:
                CACHE.popitem(last=False)
    return result


class Handler(BaseHTTPRequestHandler):
    server_version = 'PureNovel/0.1'

    def log_message(self, fmt, *args):
        # Avoid logging user reading URLs and query strings.
        pass

    def send_data(self, status, data, content_type):
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(data)))
        for name, value in getattr(self, 'outgoing', []):
            self.send_header(name, value)
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('Cache-Control', 'no-store' if self.path.startswith('/api/') else 'no-cache')
        self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'")
        self.end_headers()
        self.wfile.write(data)

    def json_response(self, status, obj):
        self.send_data(status, json.dumps(obj, ensure_ascii=False).encode(), 'application/json; charset=utf-8')

    def do_GET(self):
        self.outgoing = []
        if self.path == '/api/health':
            return self.json_response(200, {'ok': True, 'authRequired': cloud.required()})
        if self.path in ('/api/session', '/api/state'):
            try:
                if not cloud.required():
                    return self.json_response(200, {'local': True})
                user, token = cloud.authenticate(self.headers, self.outgoing)
                if self.path == '/api/session':
                    return self.json_response(200, {'user': user})
                rows = cloud.remote('/rest/v1/reader_states?select=version,payload', token=token)
                return self.json_response(200, rows[0] if rows else {'version': 0, 'payload': None})
            except cloud.CloudError as exc:
                return self.json_response(exc.status, {'error': str(exc)})
        files = {'/': ('index.html', 'text/html; charset=utf-8'), '/app.js': ('app.js', 'text/javascript; charset=utf-8'),
                 '/ebooks.js': ('ebooks.js', 'text/javascript; charset=utf-8'),
                 '/style.css': ('style.css', 'text/css; charset=utf-8'), '/sw.js': ('sw.js', 'text/javascript; charset=utf-8'),
                 '/manifest.webmanifest': ('manifest.webmanifest', 'application/manifest+json'), '/icon.svg': ('icon.svg', 'image/svg+xml')}
        item = files.get(urlsplit(self.path).path)
        if not item:
            return self.json_response(404, {'error': '页面不存在。'})
        self.send_data(200, (ROOT / 'public' / item[0]).read_bytes(), item[1])

    def do_POST(self):
        self.outgoing = []
        if self.path not in ('/api/read', '/api/search', '/api/check', '/api/login', '/api/logout', '/api/state', '/api/publications', '/api/openbooks', '/api/openbooks/acquire'):
            return self.json_response(404, {'error': '接口不存在。'})
        origin = self.headers.get('Origin')
        if origin and urlsplit(origin).netloc != self.headers.get('Host'):
            return self.json_response(403, {'error': '不允许跨站请求。'})
        if self.headers.get('Content-Type', '').split(';')[0] != 'application/json':
            return self.json_response(415, {'error': '只接受 JSON 请求。'})
        try:
            length = int(self.headers.get('Content-Length', '0'))
            if not 0 < length <= (510000 if self.path == '/api/state' else 8192):
                raise ValueError()
            data = json.loads(self.rfile.read(length))
            if not isinstance(data, dict):
                raise ValueError()
            required = ('query',) if self.path in ('/api/search', '/api/publications') else (('query', 'url') if self.path == '/api/check' else (('url',) if self.path == '/api/read' else ()))
            if any(not isinstance(data.get(key), str) for key in required):
                raise ValueError()
        except (ValueError, UnicodeError):
            return self.json_response(400, {'error': '请求格式无效。'})
        try:
            if self.path == '/api/logout':
                token = cloud.cookies(self.headers).get('pn_access')
                if token and cloud.enabled():
                    try:
                        cloud.remote('/auth/v1/logout?scope=local', token=token, method='POST')
                    except cloud.CloudError:
                        pass
                self.outgoing.extend(cloud.session_headers())
                return self.json_response(200, {'ok': True})
            if self.path == '/api/login':
                return self.json_response(200, cloud.login(data, self.outgoing))
            if cloud.required():
                user, token = cloud.authenticate(self.headers, self.outgoing)
                cloud.consume(user['id'], 'sync' if self.path == '/api/state' else ('search' if self.path == '/api/publications' else self.path.rsplit('/', 1)[-1]))
                if self.path == '/api/state':
                    return self.json_response(200, cloud.sync(token, data))
            elif self.path == '/api/state':
                return self.json_response(400, {'error': '本地模式不使用云端同步。'})
        except cloud.CloudError as exc:
            return self.json_response(exc.status, {'error': str(exc)})
        if not FETCH_SLOTS.acquire(blocking=False):
            return self.json_response(429, {'error': '正在处理其他阅读请求，请稍后再试。'})
        try:
            if self.path in ('/api/openbooks', '/api/openbooks/acquire'):
                try:
                    if self.path == '/api/openbooks':
                        result = openbooks.search(data.get('query'))
                    else:
                        if data.get('authorized') is not True:
                            raise ValueError('请先确认所选公版原文在所在地允许获取与阅读。')
                        result = openbooks.acquire(data.get('key'), public_addresses, PinnedConnection)
                except ValueError as exc:
                    raise ReaderError(str(exc)) from None
            elif self.path == '/api/publications':
                try:
                    result = publications.search(data['query'], fetch_page, Document, tidy)
                except ValueError as exc:
                    raise ReaderError(str(exc)) from None
            elif self.path == '/api/search':
                result = find_books(data['query'], data.get('round', 0))
            elif self.path == '/api/check':
                result = check_source(data['query'], data['url'])
            else:
                result = load_reading(data['url'])
            self.json_response(200, result)
        except ReaderError as exc:
            self.json_response(422, {'error': str(exc), 'category': failure_kind(str(exc))})
        except (OSError, http.client.HTTPException):
            self.json_response(502, {'error': '读取源站超时或连接中断，请稍后再试。'})
        except Exception:
            self.json_response(500, {'error': '解析失败，该网页结构暂不支持。'})
        finally:
            FETCH_SLOTS.release()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Pure Novel personal reader')
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=8000)
    args = parser.parse_args()
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f'Pure Novel: http://{args.host}:{args.port} (Ctrl+C to stop)', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
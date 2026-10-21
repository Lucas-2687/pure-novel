"""Small reviewed public-domain catalogue, never an arbitrary-URL download proxy."""
import hashlib
import re
import time
from urllib.parse import urlsplit

# Original historical texts, not modern translations. Keep Gutenberg notices intact.
CATALOG = (
    {'key': 'pg-23962', 'title': '西遊記', 'author': '吳承恩',
     'aliases': '西游记 吴承恩 Journey to the West', 'number': 23962,
     'version': 'Gutenberg #23962 · 中文原文'},
    {'key': 'pg-23950', 'title': '三國演義', 'author': '羅貫中',
     'aliases': '三国演义 罗贯中 Romance of the Three Kingdoms', 'number': 23950,
     'version': 'Gutenberg #23950 · 中文原文'},
)
MIRROR = 'https://mirror.cs.odu.edu/gutenberg/'
MAX_BYTES = 4 * 1024 * 1024


def search(query):
    if not isinstance(query, str) or not 2 <= len(query.strip()) <= 60:
        raise ValueError('请输入 2～60 字的书名或作者。')
    words = query.strip().strip('《》').casefold().split()
    books = []
    for item in CATALOG:
        if all(w in ' '.join((item['title'], item['author'], item['aliases'])).casefold() for w in words):
            books.append({k: item[k] for k in ('key', 'title', 'author', 'version')})
    return {'books': books, 'notice': '小型公版书目，不是全网书库；来源标注美国公版，使用前请确认所在地权限。'}


def file_url(item):
    number = str(item['number'])
    return MIRROR + '/'.join(number[:-1]) + '/' + number + '/' + number + '-0.txt'


def fetch_text(url, public_addresses, connection):
    """Pin public IPs and refuse redirects. Only server-selected mirror paths."""
    if url not in {file_url(item) for item in CATALOG}:
        raise ValueError('不支持此文件来源。')
    parsed = urlsplit(url)
    addresses = public_addresses(parsed.hostname, 443)
    for address in addresses[:3]:
        conn = connection(parsed.hostname, address, True)
        try:
            conn.request('GET', parsed.path, headers={
                'User-Agent': 'PureNovelReader/0.1 (individual public-domain file)',
                'Accept-Encoding': 'identity', 'Accept': 'text/plain'})
            response = conn.getresponse()
            if response.status != 200:
                raise ValueError('公版来源返回 HTTP %s；未绕过或跟随其他来源。' % response.status)
            if 'text/plain' not in response.getheader('Content-Type', '').lower():
                raise ValueError('来源没有返回 TXT 文件。')
            if response.getheader('Content-Encoding', 'identity') != 'identity':
                raise ValueError('来源文件压缩方式不受支持。')
            expected = response.getheader('Content-Length')
            if expected and (not expected.isdigit() or int(expected) > MAX_BYTES):
                raise ValueError('来源文件超出大小限制。')
            raw = bytearray()
            deadline = time.monotonic() + 210
            while True:
                if time.monotonic() > deadline:
                    raise ValueError('公版文件获取超时，请重试。')
                chunk = response.read1(min(65536, MAX_BYTES + 1 - len(raw)))
                if not chunk:
                    break
                raw.extend(chunk)
                if len(raw) > MAX_BYTES:
                    raise ValueError('来源文件超出大小限制。')
            if expected and int(expected) != len(raw):
                raise ValueError('来源文件不完整，未导入。')
            return bytes(raw)
        except OSError:
            continue
        finally:
            conn.close()
    raise ValueError('无法安全连接公版文件镜像，请稍后重试。')


def prepare(item, raw):
    if not raw or len(raw) > MAX_BYTES:
        raise ValueError('文件为空或超出大小限制。')
    try:
        text = raw.decode('utf-8-sig')
    except UnicodeError:
        raise ValueError('来源 TXT 不是有效的 UTF-8。') from None
    if '\x00' in text or re.search(r'<\s*(?:!doctype|html|head|body|script|form)\b', text, re.I):
        raise ValueError('来源返回了网页或二进制内容，未导入。')
    if not re.search(r'\[EBook\s*#%s\]|eBook(?:\s+number)?\s*[:#]?\s*%s\b' %
                     (item['number'], item['number']), text, re.I):
        raise ValueError('文件编号与所选版本不一致，未导入。')
    if not re.search(r'\*\*\*\s*END OF (?:THE |THIS )?PROJECT GUTENBERG', text, re.I):
        raise ValueError('没有找到正文结束标记，文件可能不完整。')
    sections = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        heading = re.match(r'^第[零〇○Ｏ一二三四五六七八九十百千万两\d]+[章回节卷].{0,75}$', line)
        if not sections or heading:
            sections.append({'title': line if heading else '正文与来源声明', 'paragraphs': []})
        sections[-1]['paragraphs'].append(line)
    if not sections or not re.search(r'[\u4e00-\u9fff]', text):
        raise ValueError('未取得中文正文。')
    return {'id': hashlib.sha256(raw).hexdigest(), 'title': item['title'], 'author': item['author'],
            'sections': sections, 'source': file_url(item), 'version': item['version'],
            'rights': '来源标注美国公版；原文件的 Gutenberg 声明和许可保留在正文中。'}


def acquire(key, public_addresses, connection):
    item = next((item for item in CATALOG if item['key'] == key), None)
    if item is None:
        raise ValueError('此版本尚未接入自动获取；不能通过网址或书名猜测文件。')
    return prepare(item, fetch_text(file_url(item), public_addresses, connection))
"""Opt-in, bounded public-source survey; never part of offline unittest discovery.

Run from the repository root: python -B tests/source-survey.py
Only fixed public endpoints, no credentials, no file downloads. A successful
metadata response is deliberately NOT reported as a successful acquisition.
"""
import concurrent.futures
import datetime
import json
from pathlib import Path
import sys
import time
from urllib.parse import quote, urljoin, urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import reader


PROBES = (
    ('gutendex', '中文开放目录', 'https://gutendex.com/books/?languages=zh&copyright=false'),
    ('wikisource', '西游记', 'https://zh.wikisource.org/w/api.php?action=query&format=json&list=search&srsearch=' + quote('西遊記') + '&srlimit=3'),
    ('wikisource', '红楼梦', 'https://zh.wikisource.org/w/api.php?action=query&format=json&list=search&srsearch=' + quote('紅樓夢') + '&srlimit=3'),
    ('wikisource', '鲁迅', 'https://zh.wikisource.org/w/api.php?action=query&format=json&list=search&srsearch=' + quote('魯迅') + '&srlimit=3'),
    ('wikisource', '红楼梦目录解析', 'https://zh.wikisource.org/w/api.php?action=parse&format=json&prop=text%7Clinks&page=' + quote('紅樓夢')),
    ('wikisource', '红楼梦第一回正文样本', 'https://zh.wikisource.org/w/api.php?action=parse&format=json&prop=text&page=' + quote('紅樓夢/第001回')),
    ('standardebooks', '公开获取目录', 'https://standardebooks.org/feeds/opds/all'),
    ('openlibrary', '三体（书目，不代表可读）', 'https://openlibrary.org/search.json?title=' + quote('三体') + '&limit=3'),
    ('openlibrary', 'Pride and Prejudice（书目）', 'https://openlibrary.org/search.json?title=Pride%20and%20Prejudice&limit=3'),
    ('z-library', '三体', 'https://z-lib.tv/zh/s?q=' + quote('三体')),
    ('z-library', '活着', 'https://z-lib.tv/zh/s?q=' + quote('活着')),
    ('z-library', 'Pride and Prejudice', 'https://z-lib.tv/zh/s?q=Pride%20and%20Prejudice'),
    ('gutenberg', '官方机器访问政策', 'https://www.gutenberg.org/policy/robot_access.html'),
    ('gutenberg', '允许的中文TXT收集入口', 'https://www.gutenberg.org/robot/harvest?filetypes[]=txt&langs[]=zh'),
)


class SurveyConnection(reader.PinnedConnection):
    def __init__(self, *args):
        super().__init__(*args)
        self.timeout = 6


def fetch_structured(url):
    """Fixed JSON/Atom probes only. Pin public IPs; never follow redirects."""
    if url not in {entry[2] for entry in PROBES}:
        raise ValueError('Only reviewed survey URLs are allowed')
    parsed = urlsplit(reader.validate_url(url))
    addresses = reader.public_addresses(parsed.hostname, 443)
    for address in addresses[:2]:
        conn = SurveyConnection(parsed.hostname, address, True)
        try:
            conn.request('GET', parsed.path + ('?' + parsed.query if parsed.query else ''), headers={
                'User-Agent': 'PureNovelReader/0.1 (bounded public metadata survey)',
                'Accept': 'application/json,application/atom+xml,application/xml',
                'Accept-Encoding': 'identity',
            })
            response = conn.getresponse()
            if response.status != 200:
                raise reader.ReaderError('HTTP %s; no redirect or access-control bypass' % response.status)
            mime = response.getheader('Content-Type', '').lower()
            if not any(value in mime for value in ('application/json', 'application/atom+xml', 'application/xml', 'text/xml')):
                raise reader.ReaderError('Unexpected structured-response MIME type')
            if response.getheader('Content-Encoding', 'identity').lower() != 'identity':
                raise reader.ReaderError('Unexpected compressed response')
            length = response.getheader('Content-Length')
            limit = 2 * 1024 * 1024
            if length and (not length.isdigit() or int(length) > limit):
                raise reader.ReaderError('Structured response too large')
            raw = bytearray()
            deadline = time.monotonic() + 15
            while True:
                if time.monotonic() > deadline:
                    raise reader.ReaderError('Structured response read deadline exceeded')
                chunk = response.read1(min(65536, limit + 1 - len(raw)))
                if not chunk:
                    break
                raw.extend(chunk)
                if len(raw) > limit:
                    raise reader.ReaderError('Structured response too large')
            if length and len(raw) != int(length):
                raise reader.ReaderError('Incomplete structured response')
            return bytes(raw).decode('utf-8-sig'), url
        except OSError:
            continue
        finally:
            conn.close()
    raise reader.ReaderError('Structured endpoint connection failed; not evidence of global unavailability')


def summarize(source, body, final):
    """Describe evidence, never infer acquisition capability from metadata."""
    result = {'acquisitionVerified': False}
    if source in ('gutendex', 'openlibrary', 'wikisource'):
        data = json.loads(body)
        if source == 'gutendex':
            books = data.get('results', [])
            result.update(count=data.get('count'), hasMore=bool(data.get('next')),
                          titles=[b.get('title') for b in books[:8]],
                          fileCandidates=sum(any(k.startswith(('text/plain', 'application/epub'))
                                                 for k in b.get('formats', {})) for b in books))
        elif source == 'openlibrary':
            result.update(count=data.get('numFound'), titles=[b.get('title') for b in data.get('docs', [])[:3]])
        else:
            result.update(titles=[b.get('title') for b in data.get('query', {}).get('search', [])],
                          apiError=bool(data.get('error')))
            parsed = data.get('parse', {})
            if parsed:
                text = reader.Document(parsed.get('text', {}).get('*', '')).root.text()
                result.update(pageTitle=parsed.get('title'), textCharacters=len(text),
                              chapterLinkCandidates=sum('/第' in n.get('*', '') for n in parsed.get('links', [])),
                              chineseTextPresent=any('\u4e00' <= c <= '\u9fff' for c in text),
                              scope='single_page_sample_not_complete_book')
        result.setdefault('scope', 'metadata_only')
    else:
        doc = reader.Document(body)
        anchors = [n for n in doc.root.walk() if n.tag == 'a']
        if source == 'z-library':
            def public_path(link):
                # Never retain query strings, tokens, or external link destinations.
                parsed = urlsplit(urljoin(final, link))
                return parsed.path if parsed.hostname == 'z-lib.tv' else ''
            paths = [public_path(n.attrs.get('href', '')) for n in anchors]
            result.update(titles=[reader.tidy(n.text())[:160] for n in doc.root.walk()
                                  if n.tag == 'h3' and n.attrs.get('itemprop') == 'name'][:10],
                          detailLinkCount=sum(p.startswith('/book/') or p.startswith('/zh/book/') for p in paths),
                          downloadLinkCount=sum('download' in p.lower() for p in paths),
                          loginPresent=any('login' in p.lower() for p in paths), scope='metadata_only')
        elif source == 'standardebooks':
            # XML may contain whitespace between namespace and href attributes.
            import xml.etree.ElementTree as ET
            root = ET.fromstring(body)
            links = [n for n in root.iter() if n.tag.rsplit('}', 1)[-1] == 'link']
            result.update(acquisitionLinkCount=sum('opds-spec.org/acquisition' in n.get('rel', '') for n in links),
                          scope='catalogue_only')
        else:
            result.update(scope='policy_or_harvest_only', links=len(anchors))
    return result


def probe(spec):
    source, sample, url = spec
    started = time.monotonic()
    record = {'source': source, 'sample': sample, 'url': url, 'acquisitionVerified': False}
    try:
        fetch = fetch_structured if source in ('gutendex', 'openlibrary', 'wikisource', 'standardebooks') else reader.fetch_page
        body, final = fetch(url)
        record.update(status='response_parsed', evidence=summarize(source, body, final))
    except reader.ReaderError as exc:
        record.update(status='probe_rejected_or_network_failure', error=str(exc))
    except Exception as exc:
        # No raw body, private URL, cookies or exception traceback in the report.
        record.update(status='unexpected_response', errorType=type(exc).__name__)
    record['seconds'] = round(time.monotonic() - started, 2)
    return record


def main():
    # Standalone process only. Avoid modifying live reader service configuration.
    reader.PinnedConnection = SurveyConnection
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        records = list(pool.map(probe, PROBES))
    report = {
        'timeUTC': datetime.datetime.now(datetime.timezone.utc).isoformat(),
        'environment': 'Windows local Python; direct pinned public-IP requests',
        'scope': '%s fixed public probes; not exhaustive, no login or ebook acquisition' % len(PROBES),
        'limitations': [
            'Structured probes accept JSON/XML only and refuse redirects; HTML probes use reader.fetch_page.',
            'First-page samples only; no complete catalogue claim.',
            'No rights verification, completeness check or iPhone test performed by this script.',
        ],
        'results': records,
    }
    target = ROOT / 'source-survey.json'
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    for item in records:
        print(item['source'], item['sample'], item['status'], item['seconds'],
              json.dumps(item.get('evidence', item.get('error', item.get('errorType'))), ensure_ascii=True))
    print('Report:', target)


if __name__ == '__main__':
    main()
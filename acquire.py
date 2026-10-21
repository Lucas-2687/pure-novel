"""Personal visible-browser download experiment; no download proxy or credentials API."""
import argparse
import asyncio
import hashlib
import ipaddress
import os
from pathlib import Path
import re
import socket
import subprocess
import tempfile
from urllib.parse import urlsplit
import uuid
import zipfile

MAX_BYTES = 24 * 1024 * 1024
TOTAL_BYTES = 64 * 1024 * 1024


class AcquireError(Exception):
    pass


def source_url(value):
    """Validate the initial public page, not an arbitrary server-side URL fetch."""
    try:
        parts = urlsplit(value)
        if (parts.scheme not in ('https', 'http') or not parts.hostname or
                parts.username is not None or parts.password is not None or
                parts.port not in (None, 80, 443) or parts.query or parts.fragment or
                re.search(r'[\x00-\x20\\]', value)):
            raise ValueError()
        addresses = socket.getaddrinfo(parts.hostname, parts.port or (443 if parts.scheme == 'https' else 80))
        if not addresses or any(not ipaddress.ip_address(a[4][0]).is_global for a in addresses):
            raise ValueError()
    except (ValueError, OSError):
        raise AcquireError('请使用不含凭据、查询参数或片段的公网详情页 URL；不接受下载令牌或本地地址。') from None
    return value


def reader_url(value):
    if value not in ('http://127.0.0.1:8000/', 'http://localhost:8000/'):
        raise AcquireError('实验助手只连接本机 8000 端口的纯阅，不向远程网页交付文件。')
    return value


def protect_directory(path):
    path.mkdir(parents=True, exist_ok=True)
    if os.name == 'nt':
        # Use the current SID, not a shell-expanded user name; never log command output.
        who = subprocess.run(['whoami', '/user', '/fo', 'csv', '/nh'], capture_output=True, check=True)
        match = re.search(rb'S-1-\d+(?:-\d+)+', who.stdout)
        if not match:
            raise AcquireError('无法确定当前 Windows 用户，未启动会话。')
        sid = match.group().decode('ascii')
        result = subprocess.run(['icacls', str(path), '/inheritance:r', '/grant:r',
                                 f'*{sid}:(OI)(CI)F', '*S-1-5-18:(OI)(CI)F'], capture_output=True)
        if result.returncode:
            raise AcquireError('无法保护本机会话目录，未启动会话。')
    else:
        path.chmod(0o700)


def validate_file(path):
    size = path.stat().st_size
    if not 0 < size <= MAX_BYTES:
        raise AcquireError('文件为空或超过 24 MiB。')
    suffix = path.suffix.lower()
    if suffix == '.txt':
        raw = path.read_bytes()
        try:
            if raw.startswith((b'\xff\xfe', b'\xfe\xff')):
                text = raw.decode('utf-16')
            else:
                try:
                    text = raw.decode('utf-8-sig')
                except UnicodeError:
                    text = raw.decode('gb18030')
        except UnicodeError:
            raise AcquireError('TXT 编码无效。') from None
        if (not text.strip() or '\x00' in text or
                re.search(r'<\s*(?:!doctype|html|head|body|form|script)\b', text, re.I)):
            raise AcquireError('文件疑似 HTML 登录／错误页或二进制内容，不作为 TXT 导入。')
    elif suffix == '.epub':
        try:
            with zipfile.ZipFile(path) as book:
                entries = book.infolist()
                names = [e.filename for e in entries]
                if (len(entries) > 3000 or len(names) != len(set(names)) or
                        sum(e.file_size for e in entries) > TOTAL_BYTES or
                        any(e.flag_bits & 1 or e.file_size > MAX_BYTES or
                            e.filename.startswith('/') or '\\' in e.filename or
                            '..' in e.filename.split('/') or
                            e.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED)
                            for e in entries)):
                    raise AcquireError('EPUB 加密、过大或路径／压缩结构不安全。')
                if ('META-INF/container.xml' not in names or 'mimetype' not in names or
                        book.getinfo('mimetype').file_size != 20 or
                        book.read('mimetype') != b'application/epub+zip'):
                    raise AcquireError('文件不是完整的 EPUB。')
                if book.testzip() is not None:
                    raise AcquireError('EPUB CRC 校验失败。')
                if 'META-INF/encryption.xml' in names:
                    raise AcquireError('实验助手不接受声明加密内容的 EPUB。')
        except (zipfile.BadZipFile, RuntimeError, NotImplementedError, EOFError, KeyError, OSError):
            raise AcquireError('EPUB 损坏或压缩格式不支持。') from None
    else:
        raise AcquireError('只接受实际下载得到的 EPUB／TXT。')
    return hashlib.sha256(path.read_bytes()).hexdigest()


async def capture_download(page, directory, timeout=600):
    """Watch only this source context, including newly opened source tabs."""
    queue = asyncio.Queue()
    pages = set()

    def attach(tab):
        if tab not in pages:
            pages.add(tab)
            tab.on('download', enqueue)

    def enqueue(download):
        queue.put_nowait(download)

    context = page.context
    context.on('page', attach)
    for tab in context.pages:
        attach(tab)
    save = None
    download = None
    try:
        download = await asyncio.wait_for(queue.get(), timeout)
        # The source-provided name is never used as a filesystem path.
        suffix = Path(download.suggested_filename).suffix.lower()
        if suffix not in ('.epub', '.txt'):
            await download.cancel()
            raise AcquireError('来源返回的文件名不是 EPUB／TXT，已停止。')
        target = directory / (uuid.uuid4().hex + suffix)
        save = asyncio.create_task(download.save_as(target))
        deadline = asyncio.get_running_loop().time() + timeout
        while not save.done():
            if asyncio.get_running_loop().time() > deadline:
                raise AcquireError('下载超时，已停止。')
            # Includes Chromium's partial artifacts, not just the final saved copy.
            if sum(p.stat().st_size for p in directory.rglob('*') if p.is_file()) > MAX_BYTES * 2:
                raise AcquireError('下载临时文件超过安全上限，已停止。')
            await asyncio.sleep(0.1)
        await save
        digest = await asyncio.to_thread(validate_file, target)
        return target, digest
    except asyncio.TimeoutError:
        raise AcquireError('等待下载超时；未取得文件，不会自动更换来源。') from None
    finally:
        context.remove_listener('page', attach)
        for tab in pages:
            tab.remove_listener('download', enqueue)
        if save is not None and not save.done():
            if download is not None:
                await download.cancel()
            save.cancel()
            await asyncio.gather(save, return_exceptions=True)


async def import_into_reader(page, path, digest, title, author, url, timeout=600):
    await page.goto(reader_url(url))
    if page.url != url:
        raise AcquireError('本机纯阅页面发生跳转，未交付文件。')
    await page.wait_for_function('() => document.documentElement.dataset.ready === "true"')
    local = await page.evaluate('!cloudMode && document.getElementById("login-screen").hidden')
    if not local:
        raise AcquireError('此实验仅支持本地免登录模式；未向云端账号交付文件。')
    await page.evaluate('''({title, author, format}) => selectEdition({title,
        authors: author ? [author] : [], format, language: ''}, title)''',
                        {'title': title, 'author': author, 'format': path.suffix[1:].upper()})
    await page.locator('#ebook-file').set_input_files(str(path))
    await page.wait_for_function('''() => document.getElementById('ebook-confirm').open ||
        (!pending && !!document.getElementById('ebook-status').textContent)''')
    if not await page.locator('#ebook-confirm').evaluate('(node) => node.open'):
        raise AcquireError('文件已取得，但纯阅解析失败；请查看纯阅中的具体错误。')
    if not await page.evaluate('(id) => preparedEbook?.id === id', digest):
        raise AcquireError('导入文件摘要不一致，已停止。')
    print('文件已取得并解析。请在纯阅窗口核对版本，点击“确认导入并阅读”；取消不会报成功。', flush=True)
    await page.wait_for_function('() => !document.getElementById("ebook-confirm").open', timeout=timeout * 1000)
    await page.wait_for_function('() => !pending')
    opened = await page.evaluate('''id => current?.kind === 'chapter' &&
        current.catalog === 'ebook:' + id && !document.getElementById('reader').hidden''', digest)
    if not opened:
        raise AcquireError('文件已取得，但导入被取消或正文未打开。')


async def run(args):
    source_url(args.detail_url)
    reader_url(args.reader)
    if not args.authorized:
        raise AcquireError('仅在确认文件授权与来源允许获取后，使用 --authorized。')
    root = Path(os.environ.get('LOCALAPPDATA', str(Path.home()))) / 'PureNovelAssistant'
    protect_directory(root)
    from playwright.async_api import async_playwright
    async with async_playwright() as playwright:
        with tempfile.TemporaryDirectory(prefix='transfer-', dir=root) as work:
            directory = Path(work)
            source = await playwright.chromium.launch_persistent_context(
                str(root / 'source-profile'), channel='msedge', headless=False,
                accept_downloads=True, downloads_path=str(directory))
            try:
                tab = source.pages[0] if source.pages else await source.new_page()
                capture = asyncio.create_task(capture_download(tab, directory, args.timeout))
                await asyncio.sleep(0)
                try:
                    await tab.goto(args.detail_url)
                    print('请在来源窗口核对版本，必要时自行登录／完成验证，再点击正常 EPUB／TXT 下载入口。', flush=True)
                    path, digest = await capture
                finally:
                    if not capture.done():
                        capture.cancel()
                    await asyncio.gather(capture, return_exceptions=True)
                print('文件校验通过；现在自动送入本机纯阅，不需要重新选文件。', flush=True)
                reader = await playwright.chromium.launch_persistent_context(
                    str(root / 'reader-profile'), channel='msedge', headless=False,
                    accept_downloads=False)
                try:
                    page = reader.pages[0] if reader.pages else await reader.new_page()
                    await import_into_reader(page, path, digest, args.title, args.author, args.reader, args.timeout)
                    print('正文已打开。文件仅保存于助手专用纯阅浏览器，本轮不代表来源全站或手机验收。', flush=True)
                    await asyncio.to_thread(input, '阅读完成后按回车关闭窗口（阅读数据保留，传输临时文件删除）。')
                finally:
                    await reader.close()
            finally:
                await source.close()


def main():
    parser = argparse.ArgumentParser(description='纯阅个人本机获取助手（实验性，无云端）')
    parser.add_argument('--detail-url', required=True, help='无私有参数的公开具体版本详情页')
    parser.add_argument('--title', required=True, help='需人工核对的准确书名／版本')
    parser.add_argument('--author', default='')
    parser.add_argument('--reader', default='http://127.0.0.1:8000/')
    parser.add_argument('--timeout', type=int, default=600)
    parser.add_argument('--authorized', action='store_true', help='确认文件使用授权与来源允许获取')
    args = parser.parse_args()
    if not args.title.strip() or not 1 <= args.timeout <= 1800:
        parser.error('书名不能为空，超时需为 1～1800 秒。')
    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        print('已停止；没有将未完成的任务标为成功。')
        return 1
    except Exception as error:
        # Browser exceptions may contain private URLs; do not print them or a traceback.
        print(str(error) if isinstance(error, AcquireError) else
              '助手未完成：浏览器／服务连接失败或操作超时。检查可见窗口；未输出私有请求详情。')
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
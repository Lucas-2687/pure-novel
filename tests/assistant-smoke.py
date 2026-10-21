"""Controlled localhost download E2E; NOT an external source authorization test."""
import asyncio
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import sys
import tempfile
import threading

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import acquire
from playwright.async_api import async_playwright

TEXT = '第一章 开始\n这是项目自行生成的测试正文。\n第二章 继续\n这是第二章正文。'


class Fixture(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        if self.path == '/':
            payload = b'<a href="/book">Download TXT</a><a href="/bad">Bad TXT</a>'
            kind = 'text/html'
        else:
            payload = TEXT.encode() if self.path == '/book' else b'<html><body>Login required</body></html>'
            kind = 'application/octet-stream'
        self.send_response(200)
        self.send_header('Content-Type', kind)
        if self.path != '/':
            self.send_header('Content-Disposition', 'attachment; filename="fixture.txt"')
        self.end_headers()
        self.wfile.write(payload)


async def main():
    server = ThreadingHTTPServer(('127.0.0.1', 0), Fixture)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        async with async_playwright() as pw:
            with tempfile.TemporaryDirectory() as work:
                browser = await pw.chromium.launch(channel='msedge', headless=True)
                try:
                    source = await browser.new_context(accept_downloads=True)
                    target = await browser.new_context(accept_downloads=False)
                    page = await source.new_page()
                    await page.goto(f'http://127.0.0.1:{server.server_port}/')
                    download = asyncio.create_task(acquire.capture_download(page, Path(work), 15))
                    await asyncio.sleep(0)
                    await page.locator('a[href="/book"]').click()
                    path, digest = await download
                    reader = await target.new_page()
                    importing = asyncio.create_task(acquire.import_into_reader(
                        reader, path, digest, 'fixture', '', 'http://127.0.0.1:8000/', 15))
                    await reader.wait_for_url('http://127.0.0.1:8000/')
                    await reader.locator('#ebook-confirm[open]').wait_for()
                    await reader.locator('#ebook-accept').click()
                    await importing
                    assert await reader.evaluate('current.title') == '第一章 开始'
                    await reader.locator('#next').click()
                    await reader.wait_for_function('() => current?.title === "第二章 继续"')
                    await reader.wait_for_timeout(1000)
                    await reader.reload()
                    await reader.wait_for_function('() => document.documentElement.dataset.ready === "true"')
                    await reader.locator('.book-card button').first.click()
                    await reader.wait_for_function('() => current?.title === "第二章 继续"')
                    assert await reader.evaluate('async () => (await ebookStore("readonly", s => s.get(current.catalog.slice(6)))).sections.length') == 2
                    isolated = await browser.new_context()
                    other = await isolated.new_page()
                    await other.goto('http://127.0.0.1:8000/')
                    await other.wait_for_function('() => document.documentElement.dataset.ready === "true"')
                    assert await other.evaluate('state.books.length') == 0
                    await isolated.close()
                    print('PASS controlled: real browser download -> auto file delivery -> confirm -> chapters -> reload/resume; separate browser context has no shelf')
                    cancelled = asyncio.create_task(acquire.import_into_reader(
                        reader, path, digest, 'fixture', '', 'http://127.0.0.1:8000/', 15))
                    await reader.locator('#ebook-confirm[open]').wait_for()
                    await reader.locator('#ebook-cancel').click()
                    try:
                        await cancelled
                        raise AssertionError('Cancelled import reported success')
                    except acquire.AcquireError:
                        print('PASS controlled: user cancellation does not report readable success')
                    try:
                        await acquire.capture_download(page, Path(work), 0.1)
                        raise AssertionError('Missing download reported success')
                    except acquire.AcquireError:
                        print('PASS controlled: missing download timeout stops without retrying sources')
                    bad = asyncio.create_task(acquire.capture_download(page, Path(work), 15))
                    await asyncio.sleep(0)
                    await page.locator('a[href="/bad"]').click()
                    try:
                        await bad
                        raise AssertionError('HTML accepted')
                    except acquire.AcquireError:
                        print('PASS controlled: downloaded HTML disguised as TXT rejected')
                finally:
                    await browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


asyncio.run(main())
"""Opt-in real network/browser test. No mock file or source responses."""
import asyncio
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from playwright.async_api import async_playwright


async def main():
    base = os.environ.get('READER_TEST_URL', 'http://127.0.0.1:8000/')
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(channel='msedge', headless=True)
        try:
            context = await browser.new_context(viewport={'width': 390, 'height': 844}, is_mobile=True)
            page = await context.new_page()
            page.on('dialog', lambda dialog: dialog.accept())
            await page.goto(base)
            await page.wait_for_function('() => document.documentElement.dataset.ready === "true"')
            await page.locator('#url').fill('西游记')
            await page.locator('#import-button').click()
            await page.locator('#openbooks-results button').wait_for()
            await page.locator('#openbooks-results button').click()
            try:
                await page.wait_for_function('() => current?.kind === "chapter" && !pending', timeout=270000)
            except Exception:
                print('FAIL live UI status:', await page.locator('#openbooks-status').inner_text())
                raise
            assert await page.evaluate('current.catalog.startsWith("ebook:")')
            count = await page.evaluate('book.chapters.length')
            assert count == 101, count
            unmatched = await page.evaluate('''async () => {
              const data = await ebookStore('readonly', s => s.get(current.catalog.slice(6)));
              return data.sections.flatMap(s => s.paragraphs.filter(p => /^第.{1,18}回/.test(p) && p !== s.title)).slice(0,30);
            }''')
            print('Chapter-like lines not split:', unmatched)
            assert not unmatched, unmatched
            await page.locator('#next').click()
            await page.wait_for_function('() => !pending && current.url.endsWith("/1")')
            first = await page.evaluate('current.title')
            assert '第一回' in first, first
            assert await page.locator('#content').inner_text()
            await page.locator('#next').click()
            await page.wait_for_function('() => !pending && current.url.endsWith("/2")')
            second = await page.evaluate('current.title')
            assert '第二回' in second, second
            await page.wait_for_timeout(1200)
            await page.reload()
            await page.wait_for_function('() => document.documentElement.dataset.ready === "true"')
            await page.locator('#shelf .book-open').click()
            await page.wait_for_function('() => current?.url.endsWith("/2") && !pending')
            assert await page.evaluate('current.title') == second
            assert await page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            print('PASS REAL NETWORK: title search -> mirror TXT -> validation -> IndexedDB -> chapter 1/2 -> refresh/resume;', count,
                  'sections; secureContext=', await page.evaluate('isSecureContext'), '; URL=', base)
            await context.close()
        finally:
            await browser.close()


asyncio.run(main())
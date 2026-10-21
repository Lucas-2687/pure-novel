import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

import acquire


class AcquireTests(unittest.TestCase):
    def check_bytes(self, data, suffix):
        with tempfile.TemporaryDirectory() as work:
            path = Path(work) / ('book' + suffix)
            path.write_bytes(data)
            return acquire.validate_file(path)

    def epub(self, extra=None):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as book:
            book.writestr('mimetype', 'application/epub+zip')
            book.writestr('META-INF/container.xml', '<container/>')
            if extra:
                book.writestr(*extra)
        return buffer.getvalue()

    def test_txt_encodings(self):
        for encoding in ('utf-8', 'utf-16', 'gb18030'):
            self.assertEqual(len(self.check_bytes('第一章 开始\n正文。'.encode(encoding), '.txt')), 64)

    def test_html_rejected_even_utf16(self):
        for encoding in ('utf-8', 'utf-16'):
            with self.assertRaises(acquire.AcquireError):
                self.check_bytes('<html><body>登录</body></html>'.encode(encoding), '.txt')

    def test_empty_binary_unsupported(self):
        for data, suffix in ((b'', '.txt'), (b'abc\x00', '.txt'), (b'abc', '.pdf'), (b'bad zip', '.epub')):
            with self.assertRaises(acquire.AcquireError):
                self.check_bytes(data, suffix)

    def test_size_limit(self):
        with patch('acquire.MAX_BYTES', 3), self.assertRaises(acquire.AcquireError):
            self.check_bytes(b'1234', '.txt')

    def test_epub_container_crc(self):
        self.assertEqual(len(self.check_bytes(self.epub(), '.epub')), 64)
        data = bytearray(self.epub())
        index = data.find(b'PK\x01\x02')
        data[index + 16] ^= 1
        with self.assertRaises(acquire.AcquireError):
            self.check_bytes(data, '.epub')

    def test_epub_paths_encryption_and_expansion(self):
        for extra in (('../escape', 'bad'), ('META-INF/encryption.xml', '<encryption/>')):
            with self.assertRaises(acquire.AcquireError):
                self.check_bytes(self.epub(extra), '.epub')
        with patch('acquire.TOTAL_BYTES', 3), self.assertRaises(acquire.AcquireError):
            self.check_bytes(self.epub(), '.epub')

    def test_source_url_public_only_no_secrets(self):
        public = [(2, 1, 6, '', ('93.184.216.34', 443))]
        with patch('acquire.socket.getaddrinfo', return_value=public):
            self.assertEqual(acquire.source_url('https://example.org/books/1'), 'https://example.org/books/1')
            for url in ('file:///book', 'https://user:pass@example.org/book',
                        'https://example.org/book?token=private', 'https://example.org/book#secret',
                        'https://example.org:9000/book'):
                with self.assertRaises(acquire.AcquireError):
                    acquire.source_url(url)
        with patch('acquire.socket.getaddrinfo', return_value=[(2, 1, 6, '', ('127.0.0.1', 443))]):
            with self.assertRaises(acquire.AcquireError):
                acquire.source_url('https://example.org/books/1')

    def test_reader_only_fixed_loopback(self):
        self.assertEqual(acquire.reader_url('http://127.0.0.1:8000/'), 'http://127.0.0.1:8000/')
        for url in ('https://example.org/', 'http://127.0.0.1:8000/evil', 'http://evil.localhost:8000/'):
            with self.assertRaises(acquire.AcquireError):
                acquire.reader_url(url)
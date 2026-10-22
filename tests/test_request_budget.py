"""Connection-stage cancellation regressions using only loopback sockets."""
import socket
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch
from urllib.error import URLError
from urllib.request import Request

import cloud
import request_budget as budget


class ConnectionStageTests(unittest.TestCase):
    def listener(self):
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.addCleanup(listener.close)
        listener.bind(('127.0.0.1', 0))
        listener.listen(2)
        listener.settimeout(0.1)
        return listener

    def assert_no_connection(self, listener):
        try:
            connection, _ = listener.accept()
        except socket.timeout:
            return
        with connection:
            connection.settimeout(0.1)
            try:
                data = connection.recv(4096)
            except socket.timeout:
                data = b''
        self.fail(f'Cancelled DNS lookup still opened TCP and sent {len(data)} bytes')

    def test_dns_released_after_504_never_connects(self):
        original_dns = socket.getaddrinfo
        for scheme in ('http', 'https'):
            with self.subTest(scheme=scheme):
                listener = self.listener()
                entered, release = threading.Event(), threading.Event()
                workers = budget.BoundedWorkers(1, 'test-delayed-dns')

                def dns(host, port, *args, **kwargs):
                    self.assertEqual(host, 'cloud.invalid')
                    entered.set()
                    if not release.wait(2):
                        raise AssertionError('DNS test was not released')
                    return original_dns('127.0.0.1', listener.getsockname()[1], *args, **kwargs)

                with ThreadPoolExecutor(max_workers=1) as callers, patch('cloud.config', return_value=(scheme + '://cloud.invalid', 'test-key')), patch('urllib.request.getproxies', return_value={}), patch.object(budget, 'NETWORK_WORKERS', workers), patch('socket.getaddrinfo', side_effect=dns):
                    call = callers.submit(cloud.remote, '/test', deadline=time.monotonic() + 0.15)
                    try:
                        self.assertTrue(entered.wait(1))
                        with self.assertRaises(cloud.CloudError) as error:
                            call.result(1)
                        self.assertEqual(error.exception.status, 504)
                        self.assertFalse(workers.slots.acquire(blocking=False))
                    finally:
                        release.set()
                        self.assertTrue(workers.slots.acquire(timeout=1))
                        workers.slots.release()
                    self.assert_no_connection(listener)

    def test_cancel_during_dns_prevents_connect_with_time_remaining(self):
        listener = self.listener()
        original_dns = socket.getaddrinfo
        entered, release = threading.Event(), threading.Event()
        connections = budget.Connections()
        deadline = time.monotonic() + 0.6

        def dns(host, port, *args, **kwargs):
            self.assertEqual(host, 'cloud.invalid')
            entered.set()
            if not release.wait(2):
                raise AssertionError('DNS test was not released')
            return original_dns('127.0.0.1', listener.getsockname()[1], *args, **kwargs)

        with ThreadPoolExecutor(max_workers=1) as callers, patch('urllib.request.getproxies', return_value={}), patch('socket.getaddrinfo', side_effect=dns):
            future = callers.submit(budget.open_url, Request('https://cloud.invalid/test'), deadline, connections)
            try:
                self.assertTrue(entered.wait(1))
                self.assertGreater(deadline - time.monotonic(), 0.2)
                connections.cancel()
            finally:
                release.set()
            with self.assertRaises((budget.DeadlineExceeded, URLError)):
                future.result(1)
            self.assert_no_connection(listener)

    def test_active_tls_handshake_starts_before_deadline_and_is_cancellable(self):
        listener = self.listener()
        listener.settimeout(1)
        original_dns = socket.getaddrinfo
        workers = budget.BoundedWorkers(1, 'test-tls-handshake')

        def dns(host, port, *args, **kwargs):
            self.assertEqual(host, 'cloud.invalid')
            return original_dns('127.0.0.1', listener.getsockname()[1], *args, **kwargs)

        with ThreadPoolExecutor(max_workers=1) as callers, patch('cloud.config', return_value=('https://cloud.invalid', 'test-key')), patch('urllib.request.getproxies', return_value={}), patch.object(budget, 'NETWORK_WORKERS', workers), patch('socket.getaddrinfo', side_effect=dns):
            future = callers.submit(cloud.remote, '/test', deadline=time.monotonic() + 0.4)
            connection, _ = listener.accept()
            with connection:
                connection.settimeout(1)
                data = connection.recv(4096)
                self.assertEqual(data[:2], b'\x16\x03')
                self.assertGreater(len(data), 100)
                with self.assertRaises(cloud.CloudError) as error:
                    future.result(1)
                self.assertEqual(error.exception.status, 504)
                connection.settimeout(0.2)
                self.assertEqual(connection.recv(4096), b'')
            self.assertTrue(workers.slots.acquire(timeout=1))
            workers.slots.release()

    def test_socket_is_cancellable_before_connect_returns_and_tls_never_starts(self):
        listener = self.listener()
        original_socket = socket.socket
        original_dns = socket.getaddrinfo
        connected, release = threading.Event(), threading.Event()
        workers = budget.BoundedWorkers(1, 'test-delayed-connect')

        class PausedSocket(original_socket):
            def connect(self, address):
                super().connect(address)
                connected.set()
                if not release.wait(2):
                    raise AssertionError('TCP test was not released')

        def dns(host, port, *args, **kwargs):
            self.assertEqual(host, 'cloud.invalid')
            return original_dns('127.0.0.1', listener.getsockname()[1], *args, **kwargs)

        with ThreadPoolExecutor(max_workers=1) as callers, patch('cloud.config', return_value=('https://cloud.invalid', 'test-key')), patch('urllib.request.getproxies', return_value={}), patch.object(budget, 'NETWORK_WORKERS', workers), patch('socket.getaddrinfo', side_effect=dns), patch('socket.socket', PausedSocket):
            future = callers.submit(cloud.remote, '/test', deadline=time.monotonic() + 0.15)
            connection = None
            try:
                self.assertTrue(connected.wait(1))
                with self.assertRaises(cloud.CloudError) as error:
                    future.result(1)
                self.assertEqual(error.exception.status, 504)
                connection, _ = listener.accept()
                connection.settimeout(0.2)
                self.assertEqual(connection.recv(4096), b'')
                self.assertFalse(workers.slots.acquire(blocking=False))
            finally:
                release.set()
                if connection is not None:
                    connection.close()
                self.assertTrue(workers.slots.acquire(timeout=1))
                workers.slots.release()


if __name__ == '__main__':
    unittest.main()

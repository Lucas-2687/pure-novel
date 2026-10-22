"""Bounded daemon work: timed-out callers never release a still-running slot."""
from concurrent.futures import Future
import errno
import http.client
import socket
import sys
import threading
import time
from urllib.request import HTTPHandler, HTTPSHandler, build_opener


class DeadlineExceeded(TimeoutError):
    pass


class CapacityExceeded(Exception):
    pass


def remaining(deadline, error=DeadlineExceeded):
    seconds = deadline - time.monotonic()
    if seconds <= 0:
        raise error('请求处理超时，请稍后重试。')
    return seconds


class BoundedWorkers:
    """No queue; capacity belongs to the worker until its callable actually exits."""

    def __init__(self, capacity, name):
        self.slots = threading.BoundedSemaphore(capacity)
        self.name = name

    def submit(self, function, *args, future=None):
        if not self.slots.acquire(blocking=False):
            raise CapacityExceeded('正在处理其他请求，请稍后重试。')
        future = future if future is not None else Future()

        def run():
            try:
                if future.set_running_or_notify_cancel():
                    try:
                        result = function(*args)
                    except BaseException as exc:
                        future.set_exception(exc)
                    else:
                        future.set_result(result)
            finally:
                self.slots.release()

        try:
            threading.Thread(target=run, name=self.name, daemon=True).start()
        except BaseException:
            self.slots.release()
            raise
        return future


class Connections:
    """Interrupt socket I/O without acquiring an HTTPResponse buffered-read lock."""

    def __init__(self):
        self.lock = threading.Lock()
        self.connections = {}
        self.cancelled = False

    def _check(self, deadline=None):
        if self.cancelled:
            raise DeadlineExceeded('请求处理超时，请稍后重试。')
        return remaining(deadline) if deadline is not None else None

    def check(self, deadline):
        with self.lock:
            return self._check(deadline)

    def add(self, connection):
        with self.lock:
            self._check()
            self.connections[connection] = set()

    def capture(self, connection):
        with self.lock:
            self._check()
            if connection.sock is not None:
                self.connections[connection].add(connection.sock)

    def prepare_socket(self, connection, deadline, factory):
        # Only create/wrap sockets here, never connect or perform a TLS handshake.
        # Wrapping detaches the old socket, so replacement must be atomic with cancel.
        with self.lock:
            self._check(deadline)
            connection.sock = factory()
            self.connections[connection].add(connection.sock)

    def discard(self, connection):
        with self.lock:
            self.connections.pop(connection, None)

    def cancel(self):
        with self.lock:
            self.cancelled = True
            for connection, sockets in self.connections.items():
                sockets = sockets | ({connection.sock} if connection.sock is not None else set())
                for sock in sockets:
                    try:
                        sock.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass
                    try:
                        sock.close()
                    except OSError:
                        pass


def open_url(request, deadline, connections):
    class TimedConnection:
        def __init__(self, *args, **kwargs):
            kwargs['timeout'] = min(12, connections.check(deadline))
            super().__init__(*args, **kwargs)
            connections.add(self)

        def _connect_tcp(self):
            connections.check(deadline)
            sys.audit('http.client.connect', self, self.host, self.port)
            addresses = socket.getaddrinfo(self.host, self.port, 0, socket.SOCK_STREAM)
            connections.check(deadline)
            error = OSError('域名没有可用地址。')
            for family, kind, protocol, _, address in addresses:
                connections.check(deadline)
                try:
                    connections.prepare_socket(
                        self, deadline, lambda: socket.socket(family, kind, protocol))
                    self.sock.settimeout(min(12, connections.check(deadline)))
                    if self.source_address:
                        self.sock.bind(self.source_address)
                    try:
                        self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                    except OSError as exc:
                        if exc.errno != errno.ENOPROTOOPT:
                            raise
                    connections.check(deadline)
                    self.sock.connect(address)
                    connections.check(deadline)
                    return
                except OSError as exc:
                    self.close()
                    connections.check(deadline)
                    error = exc
            raise error

        def connect(self):
            try:
                self._connect_tcp()
                if self._tunnel_host:
                    self.sock.settimeout(min(12, connections.check(deadline)))
                    self._tunnel()
                    connections.check(deadline)
                if isinstance(self, http.client.HTTPSConnection):
                    connections.prepare_socket(
                        self, deadline, lambda: self._context.wrap_socket(
                            self.sock, server_hostname=self._tunnel_host or self.host,
                            do_handshake_on_connect=False))
                    self.sock.settimeout(min(12, connections.check(deadline)))
                    self.sock.do_handshake()
                    connections.check(deadline)
                self.sock.settimeout(min(12, connections.check(deadline)))
            except BaseException:
                self.close()
                raise

        def send(self, data):
            connections.check(deadline)
            if self.sock is None:
                self.connect()
            self.sock.settimeout(min(12, connections.check(deadline)))
            super().send(data)

        def getresponse(self):
            connections.capture(self)
            connections.check(deadline)
            return super().getresponse()

    class HTTPConnection(TimedConnection, http.client.HTTPConnection):
        pass

    class HTTPSConnection(TimedConnection, http.client.HTTPSConnection):
        pass

    class TimedHTTPHandler(HTTPHandler):
        def http_open(self, req):
            return self.do_open(HTTPConnection, req)

    class TimedHTTPSHandler(HTTPSHandler):
        def https_open(self, req):
            return self.do_open(HTTPSConnection, req)

    return build_opener(TimedHTTPHandler(), TimedHTTPSHandler()).open(
        request, timeout=min(12, connections.check(deadline)))


NETWORK_WORKERS = BoundedWorkers(16, 'reader-network')

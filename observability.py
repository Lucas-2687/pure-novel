"""READER_LOG_REQUESTS=1 emits request-only JSON to stderr; default is silent.

Fields: request_id (server random), route (known path or category), method,
status, total_ms, cloud_count/cloud_ms, auth_ms, rpc_count/rpc_ms and
fetch_count/fetch_ms. Auth/RPC times are subsets of cloud time; concurrent fetch
sums can exceed wall time, and work finishing after a response is best-effort.
Headers always expose X-Request-ID and Server-Timing. No user data is recorded.
"""
from contextlib import contextmanager
from contextvars import ContextVar
import json
import os
import secrets
import sys
import threading
import time


CURRENT = ContextVar('reader_request_trace', default=None)


class Trace:
    def __init__(self, route, method):
        self.request_id = secrets.token_hex(16)
        self.route = route
        self.method = method
        self.started = time.perf_counter()
        self.lock = threading.Lock()
        self.metrics = {name: 0 for name in (
            'cloud_count', 'cloud_ms', 'auth_ms', 'rpc_count', 'rpc_ms', 'fetch_count', 'fetch_ms')}

    def record(self, kind, milliseconds):
        with self.lock:
            if kind == 'fetch':
                self.metrics['fetch_count'] += 1
                self.metrics['fetch_ms'] += milliseconds
            else:
                self.metrics['cloud_count'] += 1
                self.metrics['cloud_ms'] += milliseconds
                if kind == 'auth':
                    self.metrics['auth_ms'] += milliseconds
                elif kind == 'rpc':
                    self.metrics['rpc_count'] += 1
                    self.metrics['rpc_ms'] += milliseconds

    def snapshot(self):
        with self.lock:
            return {key: round(value, 2) for key, value in self.metrics.items()}

    def total_ms(self):
        return round((time.perf_counter() - self.started) * 1000, 2)

    def headers(self):
        values = self.snapshot()
        timing = ', '.join(
            f'{name};dur={values[name + "_ms"]:.2f}' for name in ('cloud', 'auth', 'rpc', 'fetch'))
        return [('X-Request-ID', self.request_id),
                ('Server-Timing', f'total;dur={self.total_ms():.2f}, {timing}, cloud_calls;desc="{values["cloud_count"]}"')]

    def finish(self, status):
        if os.environ.get('READER_LOG_REQUESTS') == '1':
            record = {'request_id': self.request_id, 'route': self.route, 'method': self.method,
                      'status': status, 'total_ms': self.total_ms(), **self.snapshot()}
            print(json.dumps(record, separators=(',', ':')), file=sys.stderr, flush=True)


@contextmanager
def measure(kind):
    trace = CURRENT.get()
    started = time.perf_counter()
    try:
        yield
    finally:
        if trace is not None:
            trace.record(kind, (time.perf_counter() - started) * 1000)

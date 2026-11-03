"""Bounded provider HTTP with shared, dated cache. Never used by the API."""
import hashlib
import json
import math
import os
import threading
import time
from collections import defaultdict
from contextlib import contextmanager
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from queue import Empty, LifoQueue
from urllib.parse import urlsplit

import httpx

from tracker.providers.http import USER_AGENT, read_response


class IO:
    """Share request results and host pacing within one collection batch.

    Successful responses may survive in the disk cache; failures and Retry-After
    cooldowns live only in this instance. Injected clients remain caller-owned
    and must disable redirects and configure finite transport timeouts. Close
    this owner only after its worker requests have finished.
    """
    def __init__(self, cache=None, *, client=None, ttl=21600, workers=4):
        self.cache = Path(cache) if cache else None
        if type(workers) is not int or workers < 1:
            raise ValueError('workers must be a positive integer')
        self.client = client
        self.proxy = os.environ.get('TRACKER_MONITOR_PROXY') or None
        self.clients = LifoQueue(workers)
        for _ in range(workers):
            self.clients.put(None)
        self.ttl = ttl
        self.today = datetime.now(UTC).date()
        self.memory, self.locks = {}, defaultdict(threading.Lock)
        self.guard = threading.Lock()
        self.host_locks = defaultdict(threading.Lock)
        self.next_request, self.cooldowns = {}, {}

    def close(self):
        while not self.clients.empty():
            if client := self.clients.get_nowait():
                client.close()

    @contextmanager
    def connection(self):
        if self.client is not None:
            yield self.client
            return
        try:
            client = self.clients.get(timeout=15)
        except Empty:
            raise httpx.PoolTimeout('provider connections are busy') from None
        try:
            if client is None:
                client = httpx.Client(
                    timeout=httpx.Timeout(connect=15, read=15, write=15, pool=15),
                    limits=httpx.Limits(max_connections=1, max_keepalive_connections=1),
                    follow_redirects=False, proxy=self.proxy)
            try:
                yield client
            except httpx.TransportError:
                # A failed proxy TLS tunnel can remain active in HTTPX's pool.
                # Exclusive leases let us discard it without closing another request.
                try:
                    client.close()
                finally:
                    client = None
                raise
        finally:
            self.clients.put(client)

    def for_hosts(self, hosts, *, max_age=None):
        return ProviderIO(self, frozenset(hosts), max_age)

    def wait_for_host(self, host, interval):
        if not isinstance(interval, (int, float)) or not math.isfinite(interval) or not 0 <= interval <= 60:
            raise ValueError('invalid provider request interval')
        with self.guard:
            lock = self.host_locks[host]
        with lock:
            if self.cooldowns.get(host, 0) > time.monotonic():
                raise ValueError('provider requested a cooldown')
            delay = self.next_request.get(host, 0) - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            if interval:
                self.next_request[host] = time.monotonic() + interval

    def defer_host(self, host, retry_after):
        try:
            delay = float(retry_after)
        except (TypeError, ValueError):
            try:
                delay = (parsedate_to_datetime(retry_after) - datetime.now(UTC)).total_seconds()
            except (TypeError, ValueError, OverflowError):
                delay = 60
        if not math.isfinite(delay):
            delay = 60
        with self.guard:
            deadline = time.monotonic() + max(60, delay)
            self.cooldowns[host] = max(self.cooldowns.get(host, 0), deadline)

    def json(self, method, url, body=None, *, max_age=None, min_interval=0):
        return self._read(method, url, body, max_age=max_age, min_interval=min_interval)

    def text(self, url, *, max_age=None, min_interval=0):
        return self._read('GET', url, None, max_age=max_age, min_interval=min_interval, text=True)

    def _read(self, method, url, body, *, max_age, min_interval, text=False, observed=None):
        identity = [method, url, body] + (['text'] if text else [])
        key = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
        with self.guard:
            lock = self.locks[key]
        with lock:
            now = time.time()
            cached = self.memory.get(key)
            path = self.cache / (key + '.json') if self.cache else None
            if cached is None and path:
                try:
                    if path.is_file() and path.stat().st_size <= 32 * 1024 * 1024:
                        stored = json.loads(path.read_text())
                        # Disk is a disposable success cache, not persistent failure state.
                        if (isinstance(stored, dict) and set(stored) == {'time', 'data'}
                                and type(stored['time']) in (int, float)
                                and math.isfinite(stored['time']) and stored['time'] >= 0):
                            cached = stored
                except (OSError, ValueError, OverflowError):
                    # Ignore an unreadable cache entry and fetch from the provider.
                    pass
            ttl = self.ttl if max_age is None else min(self.ttl, max_age)
            if cached and 0 <= now - cached.get('time', 0) < ttl:
                self.memory[key] = cached
                if cached.get('error'):
                    raise ValueError('provider request failed earlier in this run')
                if observed is not None:
                    observed.append(cached['time'])
                return cached['data']
            try:
                host = urlsplit(url).hostname
                self.wait_for_host(host, min_interval)
                deadline = time.monotonic() + 30
                with self.connection() as client, client.stream(
                        method, url, json=body if method == 'POST' else None,
                        headers={'User-Agent': USER_AGENT}) as response:
                    if response.status_code in (429, 503):
                        self.defer_host(host, response.headers.get('Retry-After'))
                    response.raise_for_status()
                    body_bytes = read_response(response, max_bytes=512 * 1024 if text else 16 * 1024 * 1024, deadline=deadline)
                data = body_bytes.decode('utf-8') if text else json.loads(body_bytes)
                cached = {'time': now, 'data': data}
                if path:
                    try:
                        path.parent.mkdir(parents=True, exist_ok=True)
                        temp = path.with_suffix('.tmp')
                        temp.write_text(json.dumps(cached, separators=(',', ':')))
                        temp.replace(path)
                    except OSError:
                        # Cache persistence is optional; it cannot turn a valid
                        # provider response into a failed observation.
                        pass
                self.memory[key] = cached
                if observed is not None:
                    observed.append(cached['time'])
                return data
            except Exception:
                # A broken global KEV endpoint must not be retried for every package.
                self.memory[key] = {'time': now, 'error': True}
                raise


class ProviderIO:
    """Restrict trusted adapters to their declared HTTPS hostnames.

    This checks URL scope, not DNS destinations or arbitrary adapter code;
    adapters are in-process code, not sandboxed plugins.
    """
    def __init__(self, owner, hosts, max_age=None):
        self.owner, self.hosts = owner, hosts
        self.max_age = max_age
        self.today = owner.today
        self.observed = []

    @property
    def observed_at(self):
        """Oldest successful response used by this check, including cache hits."""
        if self.observed:
            return datetime.fromtimestamp(min(self.observed), UTC).isoformat(timespec='seconds')
        return None

    def json(self, method, url, body=None, *, min_interval=0):
        self.validate(method, url)
        return self.owner._read(method, url, body, max_age=self.max_age,
                                min_interval=min_interval, observed=self.observed)

    def text(self, url, *, min_interval=0):
        self.validate('GET', url)
        return self.owner._read('GET', url, None, max_age=self.max_age,
                                min_interval=min_interval, text=True, observed=self.observed)

    def git_history(self, repository):
        from tracker.providers.git_history import open_history

        self.validate('GET', repository)
        return open_history(self.owner.cache, repository, self.owner.proxy)

    def validate(self, method, url):
        parsed = urlsplit(url)
        if (method not in ('GET', 'POST') or parsed.scheme != 'https'
                or parsed.hostname not in self.hosts or parsed.port not in (None, 443)
                or parsed.username or parsed.password or parsed.fragment):
            raise ValueError('provider URL outside declared HTTPS hosts')

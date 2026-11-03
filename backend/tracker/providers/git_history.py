"""Collector-owned Git objects and restartable indexes for reviewed repositories."""
import hashlib
import json
import os
import re
import sqlite3
import subprocess
from collections.abc import Iterator
from contextlib import closing, contextmanager, nullcontext
from dataclasses import asdict, dataclass
from itertools import batched
from pathlib import Path
from tempfile import TemporaryDirectory

from tracker import state

MAX_RANGE = 20000
_REPOSITORY = re.compile(r'https://github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)')


class HistoryError(RuntimeError):
    """An operator-facing diagnostic whose message contains no remote output."""


def public_repository(value):
    match = _REPOSITORY.fullmatch(value) if isinstance(value, str) else None
    if not match or any(part in ('.', '..') or part.endswith('.git') for part in match.groups()):
        raise ValueError('expected a public GitHub repository root')
    return value


@dataclass(frozen=True)
class Commit:
    sha: str
    title: str
    message: str
    paths: list[str]


class History:
    def __init__(self, directory: Path, index: sqlite3.Connection, proxy=None):
        self.repo = directory / 'objects.git'
        self.index = index
        # Ignore machine URL rewrites, credential helpers, hooks and diff drivers.
        self.git = ['git', '--no-replace-objects', '-c', 'core.hooksPath=/dev/null',
                    '-c', 'credential.helper=', '-c', 'http.followRedirects=false',
                    '-c', 'gc.auto=0', '-c', 'maintenance.auto=false']
        if proxy:
            self.git += ['-c', 'http.proxy=' + proxy]
        self.git += ['--git-dir', str(self.repo)]
        self.env = {key: value for key, value in os.environ.items() if not key.startswith('GIT_')}
        self.env.update(GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL=os.devnull, GIT_TERMINAL_PROMPT='0',
                        GIT_ALLOW_PROTOCOL='https', GIT_LFS_SKIP_SMUDGE='1', LC_ALL='C')
        self.run('init', '--bare', '--quiet')
        index.executescript("""
            CREATE TABLE IF NOT EXISTS metadata (sha TEXT PRIMARY KEY, record TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS patches (sha TEXT PRIMARY KEY, patch TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS discovery (query TEXT PRIMARY KEY, head TEXT NOT NULL, result TEXT NOT NULL);
        """)

    def run(self, *args, allow=()):
        result = subprocess.run([*self.git, *args], capture_output=True, env=self.env, timeout=240)
        if result.returncode not in (0, *allow):
            # A transport error may contain credentials or operator proxy details.
            raise HistoryError(f'Git {args[0]} failed with exit code {result.returncode}.')
        return result

    def fetch(self, repository):
        public_repository(repository)
        namespace = 'refs/lookout/' + hashlib.sha256(repository.encode()).hexdigest()
        self.run('fetch', '--quiet', '--atomic', '--prune', '--no-tags', '--no-recurse-submodules',
                 repository, f'+refs/heads/*:{namespace}/heads/*', f'+refs/tags/*:{namespace}/tags/*')
        if self.run('rev-parse', '--is-shallow-repository').stdout.strip() == b'true':
            raise HistoryError('The cached repository is shallow. Rebuild it with complete history.')
        return namespace

    def resolve(self, namespace, ref):
        # Callers supply a validated hash or a qualified heads/tags ref.
        name = ref.replace('refs/', namespace + '/', 1) if ref.startswith('refs/') else ref
        result = self.run('rev-parse', '--verify', '--quiet', '--end-of-options', name + '^{commit}', allow=(1,))
        return result.stdout.decode().strip() if result.returncode == 0 else None

    def ancestor(self, first, second):
        return self.run('merge-base', '--is-ancestor', first, second, allow=(1,)).returncode == 0

    def interval(self, base, tip):
        shas = self.run('rev-list', '--topo-order', f'--max-count={MAX_RANGE + 1}',
                        tip, '^' + base, '--').stdout.decode().splitlines()
        if len(shas) > MAX_RANGE:
            raise HistoryError('History exceeds the scan budget. Review a more recent baseline.')
        return shas

    def checkpoint(self, query):
        row = self.index.execute('SELECT head,result FROM discovery WHERE query=?', (query,)).fetchone()
        return (row[0], json.loads(row[1])) if row else (None, [])

    def save_checkpoint(self, query, head, matches):
        with self.index:
            self.index.execute('INSERT OR REPLACE INTO discovery VALUES(?,?,?)', (query, head, json.dumps(matches)))

    def records(self, shas) -> Iterator[Commit]:
        # Checkpoint complete batches, including commits with no rule hit.
        # Enumerating the current DAG drops commits abandoned by a force-push.
        for chunk in batched(shas, 32):
            with self.index:
                for sha in chunk:
                    cached = self.index.execute('SELECT record FROM metadata WHERE sha=?', (sha,)).fetchone()
                    if cached:
                        yield Commit(**json.loads(cached[0]))
                        continue
                    raw = self.run('show', '--format=%s%x00%B%x00', '--diff-merges=first-parent',
                                   '--root', '--no-renames', '--name-only', '-z', sha, '--').stdout
                    title, message, paths = raw.decode('utf-8', 'replace').split('\0', 2)
                    paths = paths.removeprefix('\0').removeprefix('\n').removesuffix('\0')
                    record = Commit(sha, title, message, paths.split('\0') if paths else [])
                    self.index.execute('INSERT INTO metadata VALUES(?,?)', (sha, json.dumps(asdict(record))))
                    yield record

    def patch_ids(self, shas):
        """Stable nonempty single-parent/root patches. Stream diffs between Git processes."""
        patches = {}
        for chunk in batched(dict.fromkeys(shas), 64):
            marks = ','.join('?' for _ in chunk)
            cached = dict(self.index.execute(f'SELECT sha,patch FROM patches WHERE sha IN ({marks})', chunk))
            missing = [sha for sha in chunk if sha not in cached]
            if missing:
                values = dict.fromkeys(missing, '')
                args = ['log', '--no-walk=unsorted', '--no-merges', '--root', '--format=medium',
                        '--no-renames', '--no-ext-diff', '--no-textconv', '--full-index', '--binary', '-p', *missing, '--']
                with subprocess.Popen([*self.git, *args], stdout=subprocess.PIPE,
                                      stderr=subprocess.DEVNULL, env=self.env) as diff:
                    try:
                        result = subprocess.run([*self.git, 'patch-id', '--stable'], stdin=diff.stdout,
                                                capture_output=True, env=self.env, check=True, timeout=120)
                        if diff.wait(timeout=5):
                            raise HistoryError('Git could not read the commit patches.')
                    finally:
                        if diff.poll() is None:
                            diff.kill()
                for line in result.stdout.decode().splitlines():
                    patch, sha = line.split()
                    values[sha] = patch
                with self.index:
                    self.index.executemany('INSERT INTO patches VALUES(?,?)', values.items())
                cached.update(values)
            patches.update(cached)
        return patches


@contextmanager
def open_history(cache, repository, proxy=None) -> Iterator[History]:
    key = hashlib.sha256(public_repository(repository).encode()).hexdigest()
    with TemporaryDirectory(prefix='lookout-commits-') if cache is None else nullcontext(cache) as root:
        directory = Path(root) / 'git-history' / key
        directory.mkdir(parents=True, exist_ok=True)
        with state.writer_lock(directory / 'index', timeout=60), closing(sqlite3.connect(directory / 'index-v1.sqlite3')) as db:
            yield History(directory, db, proxy)

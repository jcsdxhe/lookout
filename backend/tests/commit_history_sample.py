"""Run from backend with python -m tests.commit_history_sample for an offline Git example."""
import json
import os
import subprocess
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory

from tracker.monitors.commits import monitor
from tracker.providers.git_history import open_history

URL = 'https://github.com/example/commit-fixture'
SUBJECT = {'name': 'fixture', 'version': '1.0', 'revision': 'sample', 'target_version': '2.0'}


class Sample:
    def __init__(self, directory):
        self.repo = directory / 'fixture'
        self.repo.mkdir()
        self.cache = directory / 'cache'
        self.git('init', '-q', '-b', 'main')
        self.base = self.commit('Baseline', 'README', 'sample')
        self.direct = self.commit('Add RISC-V support', 'cpu.c', 'cpu')
        self.equal = self.commit('Use vector instructions', 'arch/riscv/vector.c', 'vector')
        self.adapted = self.commit('RV64 ABI adjustment', 'abi.c', 'abi')
        self.absent = self.commit('RISC-V scheduling', 'schedule.c', 'schedule')
        self.git('tag', '-a', 'v2.0', '-m', 'Candidate release')
        self.git('switch', '-q', '-c', 'stable', self.direct)
        self.commit('Stable branch setup', 'README', 'stable')
        self.git('cherry-pick', self.equal)
        self.cherry = self.git('rev-parse', 'HEAD')
        self.backport = self.commit('Adapt ABI on stable\n\nBackport-of: ' + self.adapted, 'abi.c', 'adapted')
        self.git('revert', '--no-edit', self.cherry)
        self.revert = self.git('rev-parse', 'HEAD')
        self.git('tag', 'v1.0')
        self.git('switch', '-q', 'main')
        self.config = {'repository': URL, 'branch': 'main', 'baseline': self.base, 'tag_pattern': 'v{version}',
                       'keywords': ['RISC-V'], 'regex': [r'\brv64\b'], 'paths': ['arch/riscv/*']}

    def git(self, *args):
        env = {**os.environ, 'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': os.devnull,
               'GIT_AUTHOR_DATE': '2025-01-01T00:00:00Z', 'GIT_COMMITTER_DATE': '2025-01-01T00:00:00Z'}
        return subprocess.run(['git', '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.org',
                               '-c', 'core.autocrlf=false', '-C', str(self.repo), *args],
                              env=env, capture_output=True, check=True, text=True).stdout.strip()

    def commit(self, message, path, content):
        file = self.repo / path
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(content + '\n')
        self.git('add', '--', path)
        self.git('commit', '-qm', message)
        return self.git('rev-parse', 'HEAD')

    @contextmanager
    def git_history(self, repository):
        assert repository == URL
        with open_history(self.cache, repository) as history:
            # Only the sample rewrites the reviewed URL to its generated repository.
            history.git[1:1] = ['-c', f'url.{self.repo.as_uri()}.insteadOf={URL}']
            history.env['GIT_ALLOW_PROTOCOL'] = 'file'
            yield history

    def for_hosts(self, hosts, **kwargs):
        assert hosts == {'github.com'}
        return self

    def settings(self):
        return monitor.inputs({**SUBJECT, 'source_tag': {'repository': URL, 'tag': 'v1.0'}}, self.config)


if __name__ == '__main__':
    with TemporaryDirectory() as tmp:
        sample = Sample(Path(tmp))
        print(json.dumps(monitor.check(SUBJECT, sample.settings(), sample), indent=2))

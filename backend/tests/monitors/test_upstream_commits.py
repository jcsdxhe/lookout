"""Exercise the real Git graph, restartable collector and read-only package surfaces."""
import json
import os
import re
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

import pytest

from tests.commit_history_sample import SUBJECT, URL, Sample
from tests.conftest import ProjectedClient
from tests.helpers.query import query_url
from tracker import state
from tracker.api import create_app
from tracker.monitors import runner
from tracker.monitors import model as monitor_model
from tracker.monitors.commits import monitor
from tracker.monitors.commits import rules
from tracker.providers.git_history import History


def facts(result, sha, scope='current'):
    item = next(f for f in result['findings'] if f['id'] == f'{scope}:{sha}')
    return {f['code']: f['value'] for f in item['facts']}


def test_inclusion_patch_references_and_target_only_reverts(tmp_path):
    graph = Sample(tmp_path)
    result = monitor.check(SUBJECT, graph.settings(), graph)
    assert result['status'] == 'ok' and len(result['findings']) == 8
    assert facts(result, graph.direct)['inclusion'] == 'original'
    equal = facts(result, graph.equal)
    assert equal['inclusion'] == 'equivalent' and equal['witness'] == graph.cherry
    assert equal['revert'] == graph.revert and equal['patch_id']
    assert equal['match_reasons'] == ['Path arch/riscv/* · arch/riscv/vector.c']
    adapted = facts(result, graph.adapted)
    assert adapted['inclusion'] == 'backport' and adapted['witness'] == graph.backport
    assert facts(result, graph.absent)['inclusion'] == 'no_evidence'
    candidate = facts(result, graph.equal, 'upgrade')
    assert candidate['inclusion'] == 'original' and 'revert' not in candidate
    assert candidate['comparison'] == 'equivalent → original'
    assert candidate['target_commit'] == graph.absent
    assert all(f['target_version'] == '2.0' for f in result['findings'] if f['scope'] == 'upgrade')


def test_checkpoints_resume_across_restart_rewrites_and_rule_changes(tmp_path, monkeypatch):
    graph = Sample(tmp_path)
    initial = monitor.check(SUBJECT, graph.settings(), graph)
    calls = []
    original = rules.match

    def counted(commit, settings):
        calls.append(commit.sha)
        return original(commit, settings)

    monkeypatch.setattr(rules, 'match', counted)
    assert monitor.check(SUBJECT, graph.settings(), graph) == initial and not calls
    extra = graph.commit('RISC-V fix', 'another.c', 'one')
    changed = monitor.check(SUBJECT, graph.settings(), graph)
    assert calls == [extra] and len(changed['findings']) == 10
    calls.clear()
    graph.git('tag', '-f', 'v2.0', extra)
    moved = monitor.check(SUBJECT, graph.settings(), graph)
    assert not calls and facts(moved, extra, 'upgrade')['inclusion'] == 'original'
    graph.git('reset', '--hard', graph.absent)
    rewritten = monitor.check(SUBJECT, graph.settings(), graph)
    assert extra not in [f['id'].split(':')[1] for f in rewritten['findings']]
    assert len(calls) == 4
    graph.config.update(keywords=['scheduling'], regex=[], paths=[])
    narrowed = monitor.check(SUBJECT, graph.settings(), graph)
    assert [f['id'].split(':')[1] for f in narrowed['findings']] == [graph.absent] * 2


def test_mapping_conflicts_and_missing_targets_are_explicit(tmp_path):
    graph = Sample(tmp_path)
    reviewed = {**graph.config, 'releases': {'1.0': 'refs/tags/v1.0'}}
    assert monitor.inputs(SUBJECT, reviewed)['current']['basis'] == 'Reviewed release mapping'
    conflict = {**SUBJECT, 'source_tag': {'repository': 'https://github.com/other/other', 'tag': 'v1.0'}}
    settings = monitor.inputs(conflict, reviewed)
    result = monitor.check(SUBJECT, settings, graph)
    assert facts(result, graph.direct)['inclusion'] == 'unknown'
    assert 'differs' in facts(result, graph.direct)['inclusion_reason']
    graph.git('tag', '-d', 'v2.0')
    absent = monitor.check(SUBJECT, graph.settings(), graph)
    assert facts(absent, graph.direct, 'upgrade')['target_commit'] is None
    assert facts(absent, graph.direct)['target_commit'] == graph.revert
    missing_base = monitor.check(SUBJECT, {**graph.settings(), 'baseline': 'f' * 40}, graph)
    assert missing_base['status'] == 'error' and 'baseline' in missing_base['note']
    for invalid in ({'repository': 'file:///tmp'}, {'branch': '-x'}, {'regex': ['[']},
                    {'tag_pattern': '{version}^'}, {'paths': [], 'regex': [], 'keywords': []}):
        with pytest.raises((ValueError, re.error)):
            monitor.inputs(SUBJECT, {**reviewed, **invalid})


def test_downstream_tag_and_sha_are_bound_to_the_source_repository(tmp_path, monkeypatch):
    graph = Sample(tmp_path)
    downstream = tmp_path / 'downstream.git'
    graph.git('init', '--bare', str(downstream))
    graph.git('push', str(downstream), graph.equal + ':refs/tags/v1.0', graph.revert + ':refs/tags/v2.0')
    url = 'https://github.com/example/downstream'
    history_for = graph.git_history

    @contextmanager
    def with_downstream(repository):
        with history_for(repository) as history:
            history.git[1:1] = ['-c', f'url.{downstream.as_uri()}.insteadOf={url}']
            yield history

    monkeypatch.setattr(graph, 'git_history', with_downstream)
    config = {**graph.config, 'source_repository': url, 'releases': {'1.0': 'refs/tags/v1.0'}}
    result = monitor.check(SUBJECT, monitor.inputs(SUBJECT, config), graph)
    assert facts(result, graph.equal)['target_commit'] == graph.equal
    assert facts(result, graph.equal, 'upgrade')['target_commit'] == graph.absent
    for sha, expected in ((graph.cherry, graph.cherry), (graph.absent, None)):
        source = {**SUBJECT, 'source_commit': {'repository': url, 'commit': sha}}
        result = monitor.check(source, monitor.inputs(source, config), graph)
        assert facts(result, graph.equal)['target_commit'] == expected
        if expected is None:
            assert facts(result, graph.equal)['inclusion'] == 'unknown'


def test_candidate_filter_detects_different_reverts_with_same_inclusion(tmp_path):
    graph = Sample(tmp_path)
    for branch, tag in [('stable', 'v1.0'), ('main', 'v2.0')]:
        graph.git('switch', '-q', branch)
        graph.git('revert', '--no-edit', graph.direct)
        graph.git('tag', '-f', tag)
    result = monitor.check(SUBJECT, graph.settings(), graph)
    current = facts(result, graph.direct)
    candidate = facts(result, graph.direct, 'upgrade')
    assert current['inclusion'] == candidate['inclusion'] == 'original'
    assert current['revert'] != candidate['revert']
    finding = next(f for f in result['findings'] if f['id'] == 'upgrade:' + graph.direct)
    assert 'InclusionChanged' in finding['tags']


def test_interrupted_scan_reuses_completed_batches(tmp_path, monkeypatch):
    graph = Sample(tmp_path)
    for sequence in range(38):
        graph.commit('RISC-V change', 'counter', str(sequence))
    original = History.run
    batches = []
    failing = True

    def interrupt(history, *args, **kwargs):
        if '--name-only' in args:
            batches.append(args[args.index('-z') + 1:-1])
            if failing and len(batches) == 2:
                raise OSError('interrupted')
        return original(history, *args, **kwargs)

    subject = {**SUBJECT, 'target_version': None}
    monkeypatch.setattr(History, 'run', interrupt)
    with pytest.raises(OSError, match='interrupted'):
        monitor.check(subject, graph.settings(), graph)
    completed = set(batches[0])
    assert len(completed) == 32
    failing = False
    batches.clear()
    recovered = monitor.check(subject, graph.settings(), graph)
    assert recovered['status'] == 'ok' and len({f['id'] for f in recovered['findings']}) == 42
    assert not completed.intersection(sha for batch in batches for sha in batch)


def test_batched_records_preserve_order_empty_commits_and_unusual_paths(tmp_path):
    graph = Sample(tmp_path)
    graph.git('commit', '--allow-empty', '--allow-empty-message', '-m', '')
    empty = graph.git('rev-parse', 'HEAD')
    graph.git('switch', '-qc', 'topic')
    path = '\nvector\tline\n测试.c'
    topic = graph.commit('One subject\n\nAdditional detail', path, 'change')
    graph.git('switch', '-q', 'main')
    graph.git('merge', '-q', '--no-ff', 'topic', '-m', 'Merge topic')
    merge = graph.git('rev-parse', 'HEAD')
    requested = [empty, topic, graph.base, merge, topic]
    with graph.git_history(URL) as history:
        history.fetch(URL)
        list(history.records([graph.base]))  # Mix a previously indexed SHA into the batch.
        batch = list(history.records(requested))
        assert [c.sha for c in batch] == requested
        assert (batch[0].title, batch[0].message, batch[0].paths) == ('', '', [])
        assert batch[1].title == 'One subject' and batch[1].message == 'One subject\n\nAdditional detail\n'
        assert batch[1].paths == batch[3].paths == [path] and batch[2].paths == ['README']
        assert list(history.records(requested)) == batch


def test_rechecks_refs_before_reusing_release_assessments(tmp_path, monkeypatch):
    graph = Sample(tmp_path)
    initial = monitor.check(SUBJECT, graph.settings(), graph)
    scans = []
    interval = History.interval

    def track_scan(history, base, tip):
        scans.append(tip)
        return interval(history, base, tip)

    monkeypatch.setattr(History, 'interval', track_scan)
    assert monitor.check(SUBJECT, graph.settings(), graph) == initial and not scans
    graph.git('switch', '-q', 'stable')
    discussed = graph.commit(f'Revert "Backport of {graph.absent}"\n\nThis was not cherry-picked from commit {graph.absent}.',
                             'notes', 'deferred')
    graph.git('tag', '-f', 'v1.0')
    updated = monitor.check(SUBJECT, graph.settings(), graph)
    assert scans == [discussed] and facts(updated, graph.absent)['inclusion'] == 'no_evidence'


def test_new_short_sha_collision_invalidates_a_cached_backport(tmp_path):
    graph = Sample(tmp_path)
    # Two real Git commits with the same seven-digit prefix under the fixture's fixed author and clock.
    tree = graph.git('hash-object', '-w', '-t', 'tree', os.devnull)
    first, second = [graph.git('commit-tree', tree, '-m', f'RISC-V archive {n}') for n in (8222, 32864)]
    ref = first[:7]
    assert ref == second[:7] and first != second
    graph.git('merge', '--allow-unrelated-histories', '--no-ff', first, '-m', 'Merge architecture work')
    graph.git('switch', '-q', 'stable')
    witness = graph.commit(f'Adapt scheduling\n\n(cherry picked from commit {ref})', 'schedule.c', 'adapted')
    graph.git('tag', '-f', 'v1.0')
    updated = monitor.check(SUBJECT, graph.settings(), graph)
    assert facts(updated, first)['witness'] == witness and facts(updated, first)['inclusion'] == 'backport'
    assert monitor.check(SUBJECT, graph.settings(), graph) == updated
    graph.git('branch', 'unrelated', second)
    uncertain = facts(monitor.check(SUBJECT, graph.settings(), graph), first)
    assert uncertain['inclusion'] == 'unknown' and uncertain['witness'] == witness
    assert 'uniquely' in uncertain['inclusion_reason']
    proof = graph.commit(f'This patch was backported from commit {first}\n\nBackport-of: {first[:12]}',
                         'notes', 'record full SHA')
    graph.git('tag', '-f', 'v1.0')
    repaired = monitor.check(SUBJECT, graph.settings(), graph)
    assert facts(repaired, first)['inclusion'] == 'backport'
    finding = next(f for f in repaired['findings'] if f['id'] == 'current:' + first)
    assert [f['value'] for f in finding['facts'] if f['code'] == 'witness'] == [proof]


def test_runner_storage_filters_retention_and_retry(config, snapshot, tmp_path, monkeypatch):
    graph = Sample(tmp_path)
    config.update(monitors={'enabled': ['commits']}, config_digest='fixture', nv_digest='fixture')
    config['packages']['binutils'] = {'monitors': {'commits': graph.config}}
    monkeypatch.setattr(runner.cfg, 'require_unchanged', lambda *args: None)
    monkeypatch.setattr(state, 'compare', lambda current, target, *args: 'outdated' if target else 'unknown')
    snapshot['specs']['binutils'] = state.success({}, {
        'head': 'a' * 40, 'version': '1.0',
        'metadata': {'version': '1.0', 'sources': [{'number': 0, 'url': URL + '/archive/refs/tags/v1.0.tar.gz'}]},
        'native_query': {'spec_sha256': 'c' * 64, 'context': {'resolver': 10}},
    }, state.utcnow())
    snapshot['tracks']['binutils']['version'] = '2.0'
    db = tmp_path / 'state.sqlite3'
    state.commit(db, snapshot)
    collected = runner.collect(config, 'fixture', db, io=graph)
    saved = collected['monitors']['binutils']['commits']
    assert saved['status'] == 'ok' and saved['findings']
    assert runner.collect(config, 'fixture', db, io=graph)['generation'] == collected['generation']
    api = ProjectedClient(create_app(db))
    plan = runner.plan(config, collected, 'binutils', 'commits')
    old = (datetime.now(UTC) - timedelta(hours=2)).isoformat()
    for release in saved['scope_checks'].values():
        release['checked_at'] = old
    saved['checked_at'] = old
    def disconnected(*args):
        raise OSError('offline')

    with monkeypatch.context() as offline:
        offline.setattr(History, 'fetch', disconnected)
        listing = api.get(query_url('/api/v2/packages', {'maintenance': 'EquivalentPatch'}, monitor='commits')).json()
        assert listing['total'] == 1 and listing['items'][0]['name'] == 'binutils'
        data = api.get('/api/v2/packages/binutils?include=commits&commit_state=equivalent').json()['monitors']['commits']['data']
        assert data['finding_count'] == 2 and {f['scope'] for f in data['findings']} == {'current', 'upgrade'}
        assert api.get('/api/v2/packages/binutils?commit_state=typo').status_code == 422
        version_ref = next(f for finding in data['findings'] if finding['scope'] == 'current'
                           for f in finding['facts'] if f['code'] == 'target_ref')
        assert version_ref['url'] == URL + '/archive/refs/tags/v1.0.tar.gz'
        page = api.get('/api/ui/packages/binutils?commit_state=equivalent').json()
        section = next(s for s in page['sections'] if s['id'] == 'commits')
        assert len(section['entries']) == 1 and graph.cherry[:12] in json.dumps(section['entries'])
        failed = runner.execute('commits', plan, graph, saved)
        assert failed['status'] == 'error' and failed['checked_at'] == old
        assert failed['findings'] == saved['findings']
    retry = runner.execute('commits', plan, graph, failed)
    assert retry['status'] == 'ok' and retry['failures'] == 0 and retry['checked_at'] != old

    release_commit = History.release_commit
    def fail_current(history, namespace, ref):
        if ref == 'refs/tags/v1.0':
            raise OSError('current target could not be read')
        return release_commit(history, namespace, ref)

    monkeypatch.setattr(History, 'release_commit', fail_current)
    partial = runner.execute('commits', plan, graph, saved)
    assert partial['status'] == 'partial'
    collected['monitors']['binutils']['commits'] = partial
    projected = monitor_model.project(collected, 'binutils', datetime.now(UTC))['findings']
    assert all(f['checked_at'] == old and f['stale'] for f in projected if f['scope'] == 'current')
    assert all(f['checked_at'] == partial['checked_at'] and not f['stale'] for f in projected if f['scope'] == 'upgrade')

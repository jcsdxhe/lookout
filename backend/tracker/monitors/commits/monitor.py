"""Collect dated inclusion evidence for the packaged source and selected upgrade."""
import sqlite3
import subprocess

from tracker.monitors.commits.analysis import MAX_MATCHES, assess, discover
from tracker.monitors.commits.rules import candidate_identity, inputs
from tracker.monitors.issues import INCLUSION_LABELS
from tracker.monitors.model import evidence, finding
from tracker.monitors.model import version_query as query_subject
from tracker.providers.git_history import HistoryError

__all__ = ['HOSTS', 'SCOPE', 'TITLE', 'VERSION', 'check', 'inputs', 'query_subject']

TITLE = 'Upstream commits'
VERSION = 2
HOSTS = {'github.com'}
SCOPE = 'current_and_upgrade'

def commit_url(repository, sha):
    return f'{repository}/commit/{sha}'


def observation(match, scope, version, target, tip, result, settings, head, current):
    upstream = settings['repository']
    base = settings['baseline']
    url = commit_url(upstream, match['sha'])
    repository = target.get('repository', upstream)
    target_url = commit_url(repository, tip) if tip else repository
    facts = [evidence('Matched rules', match['reasons'], 'Git commit', url, code='match_reasons'),
             evidence('Upstream commit', match['sha'], 'Git commit', url, code='upstream_commit'),
             evidence('Monitored branch', settings['branch'], 'Git head', commit_url(upstream, head), code='query'),
             evidence('Search baseline (exclusive)', base, 'Git history', commit_url(upstream, base), code='query'),
             evidence('Version', version, target.get('basis', 'Lookout'), target_url, code='release_version'),
             evidence('Inclusion', result.status, 'Git history', target_url, code='inclusion'),
             evidence('Basis', result.reason, 'Git history', target_url, code='inclusion_reason')]
    if ref := target.get('ref'):
        facts.append(evidence('Version ref', ref, target['basis'], target.get('archive', target_url), code='target_ref'))
    facts.append(evidence('Target commit', tip, 'Git history', target_url, code='target_commit',
                          status='observed' if tip else 'unavailable'))
    if result.patch:
        facts.append(evidence('Stable patch ID', result.patch, 'git patch-id', target_url, code='patch_id'))
    for title, code, shas in [('Supporting commit', 'witness', result.witnesses), ('Revert clue', 'revert', result.reverts)]:
        for sha in shas[:8]:
            facts.append(evidence(title, sha, 'Target history', commit_url(repository, sha), code=code))
        if len(shas) > 8:
            facts.append(evidence(title + ' count', len(shas), 'Target history', target_url, code=code + '_count'))
    tags = [INCLUSION_LABELS[result.status]]
    if result.reverts:
        tags.append('RevertClue')
    if current is not None:
        comparison = f'{current.status} → {result.status}'
        facts.append(evidence('Current → candidate', comparison, 'Lookout', target_url, code='comparison'))
        if (current.status != result.status or current.reverts != result.reverts
                or set(current.witnesses) != set(result.witnesses)):
            tags.append('InclusionChanged')
    return finding(f"{scope}:{match['sha']}", 'UpstreamCommit', match['title'], facts, url, scope=scope,
                   tags=tags, target_version=version if scope == 'upgrade' else None)


def collect_releases(subject, settings, io):
    repo = settings['repository']
    targets = [('current', subject['version'], settings['current'])]
    if candidate := subject.get('target_version'):
        targets.append(('upgrade', candidate, candidate_identity(settings, candidate)))
    checks, findings, current = {}, [], {}
    with io.git_history(repo) as history:
        namespaces = {repo: history.fetch(repo)}
        head = history.resolve(namespaces[repo], 'refs/heads/' + settings['branch'])
        if not head:
            raise HistoryError('The monitored branch was not found.')
        if not history.resolve(namespaces[repo], settings['baseline']):
            raise HistoryError('The reviewed baseline was not found in the fetched repository.')
        matches = discover(history, settings, head)
        selected = matches[:MAX_MATCHES]
        notes = [f"{settings['branch']} at {head}. Showing {len(selected)} of {len(matches)} matched commits "
                 f"after {settings['baseline']}."]
        for scope, version, target in targets:
            target_repo = target.get('repository', repo)
            try:
                if target_repo not in namespaces:
                    namespaces[target_repo] = history.fetch(target_repo)
                tip, results = assess(history, settings, namespaces[target_repo], target, selected)
            except (OSError, RuntimeError, ValueError, sqlite3.Error, subprocess.SubprocessError) as error:
                reason = str(error) if isinstance(error, HistoryError) else 'Target history could not be refreshed.'
                checks[scope] = {'status': 'error', 'note': reason}
                notes.append(f'{scope} {version}: {reason}')
                continue
            explanation = target.get('reason') or ('Mapped object unavailable.' if tip is None else tip)
            notes.append(f'{scope} {version}: {explanation}')
            checks[scope] = {'status': 'ok', 'note': explanation}
            findings.extend(observation(m, scope, version, target, tip, results[m['sha']], settings, head,
                                        current.get(m['sha'])) for m in selected)
            if scope == 'current':
                current = results
    successes = sum(c['status'] == 'ok' for c in checks.values())
    status = 'ok' if successes == len(checks) else 'partial' if successes else 'error'
    return {'status': status, 'findings': findings, 'note': ' '.join(notes), 'scope_checks': checks}


def check(subject, settings, io):
    try:
        return collect_releases(subject, settings, io)
    except HistoryError as error:
        scopes = ['current', 'upgrade'] if subject.get('target_version') else ['current']
        return {'status': 'error', 'findings': [], 'note': str(error),
                'scope_checks': {s: {'status': 'error', 'note': str(error)} for s in scopes}}

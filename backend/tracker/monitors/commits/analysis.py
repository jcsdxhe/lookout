"""Commit graph, patch and message evidence within one target's own history."""
import re
from collections import defaultdict
from dataclasses import asdict, dataclass, field

from tracker.monitors.commits import rules
from tracker.monitors.model import fingerprint
from tracker.providers.git_history import HistoryError

_SHA = r'([0-9a-f]{7,64})\b'
BACKPORT = re.compile(r'^[ \t]*\(?(?:this(?: (?:patch|change|commit))? (?:was |is (?:a )?))?'
                      r'(?:cherry[- ]picked from commit[ \t]+|backport(?:ed)?[- ](?:of|from):?[ \t]+(?:commit[ \t]+)?)'
                      + _SHA, re.I | re.M)
REVERT = re.compile(r'^\s*This reverts commit\s+' + _SHA, re.I | re.M)
MAX_MATCHES = 500  # Two release scopes share the 1,000-finding ingestion limit.
DISCOVERY_VERSION = 1
VERSION = 3


def discover(history, settings, head):
    base = settings['baseline']
    if not history.ancestor(base, head):
        raise HistoryError('The reviewed baseline is outside the monitored branch.')
    key = fingerprint([DISCOVERY_VERSION, settings['branch'], base, settings['rules']])
    previous, matches = history.checkpoint(key)
    if previous == head:
        return matches
    if not previous or not history.ancestor(previous, head):
        previous, matches = base, []
    additions = []
    for commit in history.records(history.interval(previous, head)):
        if reasons := rules.match(commit, settings['rules']):
            additions.append({'sha': commit.sha, 'title': commit.title[:512] or commit.sha, 'reasons': reasons})
    matches = additions + matches
    history.save_checkpoint(key, head, matches)
    return matches


@dataclass(frozen=True)
class Assessment:
    status: str
    reason: str
    witnesses: list[str] = field(default_factory=list)
    reverts: list[str] = field(default_factory=list)
    patch: str = ''


def assess(history, settings, namespace, scope, target, matches):
    tip = history.release_commit(namespace, target['ref']) if 'ref' in target else None
    if tip is None:
        reason = target.get('reason', 'The mapped Git object or release tag was not found in the fetched history.')
        return None, {m['sha']: Assessment('unknown', reason) for m in matches}
    if not matches:
        return tip, {}

    base = settings['baseline']
    key = fingerprint(['release-assessment', namespace, base, scope])
    signature = fingerprint([VERSION, tip, sorted(m['sha'] for m in matches)])
    previous, cached = history.checkpoint(key)
    abbreviations = {}

    def resolve(ref):
        ref = ref.lower()
        if len(ref) == len(tip):
            return ref
        if ref not in abbreviations:
            abbreviations[ref] = history.resolve(namespace, ref)
        return abbreviations[ref]

    # Fetching another object can make an abbreviated SHA ambiguous even when
    # the release tag and every selected upstream commit remain unchanged.
    if previous == signature and all(resolve(ref) == sha for ref, sha in cached['abbreviations'].items()):
        return tip, {sha: Assessment(**values) for sha, values in cached['results'].items()}
    results = _assess_range(history, base, tip, matches, resolve)
    history.save_checkpoint(key, signature, {'abbreviations': abbreviations,
                                             'results': {sha: asdict(result) for sha, result in results.items()}})
    return tip, results


def _assess_range(history, base, tip, matches, resolve):
    target_shas = history.interval(base, tip)
    reachable = set(target_shas)
    backports, reverts, ambiguous_backports = defaultdict(list), defaultdict(list), defaultdict(list)
    for commit in history.records(target_shas):
        for expression, references in ((BACKPORT, backports), (REVERT, reverts)):
            for ref in set(expression.findall(commit.message)):
                if resolved := resolve(ref):
                    references[resolved].append(commit.sha)
                elif expression is BACKPORT:
                    ambiguous_backports[ref.lower()].append(commit.sha)
    short_lengths = sorted({len(ref) for ref in ambiguous_backports})
    sources = {m['sha'] for m in matches}
    # Direct ancestry needs no diff. A revert may still name a separate cherry-pick.
    compare_with = target_shas if sources - reachable else reachable.intersection(reverts)
    patches = history.patch_ids([*sorted(sources), *compare_with]) if compare_with or sources - reachable else {}
    equivalents = defaultdict(list)
    for sha in compare_with:
        if patch := patches.get(sha):
            equivalents[patch].append(sha)
    complete = history.ancestor(base, tip)
    assessments = {}
    for match in matches:
        sha = match['sha']
        patch = patches.get(sha, '')
        if sha in reachable:
            status, reason, evidence = 'original', 'The original commit is reachable from the target.', [sha]
        elif patch and equivalents[patch]:
            status, reason, evidence = 'equivalent', 'The target contains the same stable Git patch ID.', equivalents[patch]
        elif backports[sha]:
            status, reason, evidence = 'backport', 'A target commit names the original. Review the adapted diff.', backports[sha]
        elif possible := [witness for length in short_lengths for witness in ambiguous_backports.get(sha[:length], ())]:
            status, reason = 'unknown', 'A backport reference does not uniquely identify the original commit.'
            evidence = possible
        elif not complete:
            status, reason, evidence = 'unknown', 'The target does not descend from the reviewed search baseline.', []
        elif not patch:
            status, reason, evidence = 'unknown', 'The original has no comparable single-parent or root patch.', []
        else:
            status, reason, evidence = 'no_evidence', 'No matching commit, stable patch or backport reference in the target range.', []
        related = {sha, *equivalents[patch], *backports[sha]}
        clues = {r for applied in related for r in reverts[applied]}
        # A target commit may cite the same change using both full and abbreviated SHAs.
        witnesses = list(dict.fromkeys(evidence))
        assessments[sha] = Assessment(status, reason, witnesses, sorted(clues), patch if status == 'equivalent' else '')
    return assessments

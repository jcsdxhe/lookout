"""Select saved commit findings while keeping both sides of a version comparison."""
from typing import Literal

from tracker.monitors.issues import INCLUSION_LABELS
from tracker.monitors.model import summarize

CommitFilter = Literal['original', 'equivalent', 'backport', 'no_evidence', 'unknown', 'changed', 'reverted']
FILTER_TAGS = {**INCLUSION_LABELS, 'changed': 'InclusionChanged', 'reverted': 'RevertClue'}


def select(result, choice):
    if not choice:
        return result
    data = result['data']
    urls = {f['evidence_url'] for f in data['findings'] if FILTER_TAGS[choice] in f['tags']
            and (choice not in INCLUSION_LABELS or f['scope'] == 'current')}
    findings = [f for f in data['findings'] if f['evidence_url'] in urls]
    return {**result, 'data': {**data, 'findings': findings, 'finding_count': len(findings), 'labels': summarize(findings)}}

"""Public issue names shared by projections, filters and presentation."""
from enum import StrEnum


class Issue(StrEnum):
    OUTDATED = 'Outdated'
    UNTRACKED = 'Untracked'
    CHECK_FAILED = 'CheckFailed'
    LICENSE_DIFF = 'LicenseDiff'
    DEP_MISMATCH = 'DepMismatch'
    ADVISORY = 'Advisory'
    YANKED = 'Yanked'


VERSION_ISSUES = {'updates': Issue.OUTDATED, 'untracked': Issue.UNTRACKED}

INCLUSION_LABELS = {'original': 'OriginalCommit', 'equivalent': 'EquivalentPatch', 'backport': 'BackportReference',
                    'no_evidence': 'NoInclusionEvidence', 'unknown': 'InclusionUnknown'}

_STORED_LABELS = {
    ('license', 'License'): Issue.LICENSE_DIFF,
    ('security', 'Security'): Issue.ADVISORY,
    ('requires', 'Requires'): 'Dependencies',
    ('requires', 'RuntimeDeps'): 'Dependencies',
    ('yanked', 'Release files'): Issue.YANKED,
}


def observation_label(monitor, label):
    # Stored observations outlive display terminology. Normalize only the known
    # producer's old category; never rewrite evidence or another adapter's label.
    return _STORED_LABELS.get((monitor, label), label)

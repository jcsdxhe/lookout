"""Pure monitor facts/projections. No provider imports, network or persistence."""

import hashlib
import json
from typing import Annotated, Literal, Self
from urllib.parse import urlsplit

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    ValidationInfo,
    field_validator,
    model_validator,
)

from tracker import state
from tracker.monitors.issues import observation_label
from tracker.monitors.observations import visible
from tracker.monitors.requires.model import RequirementDeclaration
from tracker.monitors.version import compare as version_status

CORE_IDS = frozenset(('source', 'version', 'build'))

# Coverage gaps and failed checks are not a partition of all check states:
# pending, stale, partial and inapplicable observations retain their own meaning.
CHECK_GROUPS = {
    'uncovered': ('not_configured', 'unsupported'),
    'failed': ('error',),
}


def check_failed(check):
    return check['status'] == 'error' or bool(check.get('failures'))


def subject(snapshot, name):
    return version_status.evaluate(snapshot, name).subject


def version_query(subject, inputs):
    """Dependencies of external version-based evidence, not local patch applicability."""
    return {key: subject.get(key) for key in ('version', 'target_version') if key in subject}


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def evidence(key, value, source, url, *, status="observed", code=None):
    result = dict(key=key, value=value, source=source, url=url, status=status)
    if code is not None:
        result['code'] = code
    return result


def finding(identity, label, title, facts, evidence_url, *, scope="current", tags=(), target_version=None,
            requirement=None):
    result = dict(
        id=identity,
        label=label,
        title=title,
        facts=facts,
        evidence_url=evidence_url,
        scope=scope,
        tags=list(tags),
        target_version=target_version,
    )
    if requirement is not None:
        result['requirement'] = requirement
    validate_findings([result])
    return result


def validate_url(value):
    if not isinstance(value, str) or len(value) > 8192:
        raise ValueError("invalid evidence URL")
    url = urlsplit(value)
    if url.scheme != "https" or not url.hostname or url.username or url.password:
        raise ValueError("monitor evidence must be a public HTTPS link")
    return value


Text = Annotated[str, Field(min_length=1, max_length=8192)]
Identifier = Annotated[str, Field(pattern=r"^[A-Za-z][A-Za-z0-9]{0,39}$")]
EvidenceURL = Annotated[str, AfterValidator(validate_url)]


class Evidence(BaseModel):
    """Attributed provider fact, shared by ingestion and read-only responses."""

    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)

    key: Annotated[str, Field(min_length=1, max_length=256)]
    code: Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")] | None = None
    value: str | bool | int | float | list[str] | None
    source: Annotated[str, Field(min_length=1, max_length=256)]
    url: EvidenceURL
    status: Literal["observed", "unavailable", "not_applicable", "not_evaluated"]

    @field_validator("code")
    @classmethod
    def supplied_code(cls, value, info: ValidationInfo):
        # Ingestion rejects an explicitly empty identifier; optional response
        # fields may serialize to null without rewriting the stored evidence.
        if value is None and info.context and info.context.get("stored_fact"):
            raise ValueError("an evidence code must be a nonempty identifier when supplied")
        return value

    @model_validator(mode="after")
    def checked_value(self) -> Self:
        if self.status == "observed" and self.value is None:
            raise ValueError("observed evidence requires a value")
        if self.status != "observed" and self.value is not None:
            raise ValueError("unobserved evidence cannot have a value")
        if len(json.dumps(self.value, allow_nan=False)) > 16384:
            raise ValueError("evidence value exceeds budget")
        return self


class RawFinding(BaseModel):
    """Persisted monitor contract; API projections may add fields, not relax it."""

    model_config = ConfigDict(extra="forbid", strict=True)

    id: Text
    label: Identifier
    title: Text
    facts: Annotated[list[Evidence], Field(max_length=256)]
    evidence_url: EvidenceURL
    scope: Literal["current", "upgrade"]
    tags: Annotated[list[Identifier], Field(max_length=8)]
    target_version: str | None
    requirement: RequirementDeclaration | None = None

    @model_validator(mode="after")
    def upgrade_target(self) -> Self:
        if self.scope == "upgrade" and not state.usable_version(self.target_version):
            raise ValueError("upgrade finding requires its target version")
        if self.requirement is not None:
            validate_url(self.requirement.constraint.url)
        return self


_FINDINGS = TypeAdapter(Annotated[list[RawFinding], Field(max_length=1000)])


def validate_findings(findings):
    # Validate without rewriting provider dictionaries or adding optional defaults.
    parsed = _FINDINGS.validate_python(findings, strict=True, context={"stored_fact": True})
    if any(not isinstance(item, dict) or any(not isinstance(fact, dict) for fact in item["facts"])
           for item in findings):
        raise ValueError("stored findings and evidence must be dictionaries")
    if len({item.id for item in parsed}) != len(parsed):
        raise ValueError("duplicate monitor finding id")


def project(snapshot, name, now, *, version=None):
    observations = snapshot.get("monitors", {}).get(name, {})
    version = version or version_status.evaluate(snapshot, name, now)
    current = version.subject
    latest = version.upstream.get('version')
    source_unavailable = version.source_unavailable
    findings, checks = [], []
    ttl = snapshot.get("monitor_stale_after_seconds", 86400)
    for provider, observation in sorted(observations.items()):
        observation = visible(observation)
        combined = observation.get('scope') == 'current_and_upgrade'
        expected = {**current, "target_version": latest} if observation.get("scope") == "upgrade" else current
        observed_subject = observation.get('subject') or {}
        same = ({k: v for k, v in observed_subject.items() if k != 'target_version'} == current
                if combined else observed_subject == expected)
        old = source_unavailable or observation.get('_retained', False) or state.stale(observation, now, ttl)
        status = observation.get("status", "pending") if same else "input_changed"
        gated = same and observation.get('input_status') == 'unsupported'
        if gated:
            status = 'input_unavailable'
        if same and old and status == "ok":
            status = "expired"
        compatible = True
        try:
            validate_findings(observation.get("findings", []))
        except (ValueError, TypeError):
            compatible = False
            status = "schema_changed" if same else status
        failures = []
        scopes = observation.get('scope_checks', {})
        for scope, check in scopes.items():
            if check['status'] == 'error':
                failures.append(scope + ': ' + (check.get('note') or 'Provider check failed'))
        if observation.get('error'):
            failures.append(observation['error'])
        elif observation.get('status') == 'partial' and not scopes:
            failures.append(observation.get('note') or 'Provider response incomplete')
        if not compatible:
            failures.append('Saved evidence does not match the monitor schema')
        checks.append(
            {
                "monitor": provider,
                "changed_at": observation.get("changed_at"),
                "evidence_revision": observation.get("evidence_revision"),
                "status": status,
                "checked_at": observation.get("checked_at"),
                "attempted_at": observation.get("attempted_at"),
                "note": observation.get("input_note") if gated else observation.get("note"),
                "error": observation.get("error"),
                "failures": list(dict.fromkeys(failures)),
            }
        )
        if same and compatible:
            for f in observation.get("findings", []):
                if f["scope"] == "upgrade" and (version.last_known_relation != 'outdated'
                                                or f.get("target_version") != latest):
                    continue
                scope_check = observation.get('scope_checks', {}).get(f['scope']) if combined else None
                unavailable = (source_unavailable or gated or observation.get('_retained', False) or state.stale(scope_check, now, ttl)
                               or scope_check['status'] != 'ok') if scope_check else old or status not in ('ok', 'partial')
                findings.append(
                    {
                        **f,
                        "label": observation_label(provider, f['label']),
                        "tags": list(dict.fromkeys(observation_label(provider, tag) for tag in f["tags"])),
                        "id": provider + ":" + f["id"],
                        "monitor": provider,
                        "checked_at": (scope_check if scope_check is not None else observation).get('checked_at'),
                        "stale": unavailable
                        or f['scope'] == 'upgrade' and not version.upgrading,
                    }
                )
    return {"findings": findings, "checks": checks, "summary": summarize(findings)}


def summarize(findings):
    groups = {}
    for finding in findings:
        labels = dict.fromkeys([finding['label'], *finding['tags']])
        for label in labels:
            group = groups.setdefault(label, {'label': label, 'count': 0, 'stale': False})
            group['count'] += 1
            group['stale'] |= finding['stale']
    return list(groups.values())

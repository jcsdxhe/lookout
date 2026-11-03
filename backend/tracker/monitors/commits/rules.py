"""Package rules, source attribution and changed-path matching."""
import fnmatch
import re
from urllib.parse import urlsplit

from tracker.monitors.source.release import commit_hash
from tracker.providers.git_history import public_repository


def ref_name(value):
    if (not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9._/+-]{0,199}', value)
            or '..' in value or value.endswith('.') or any(not part or part.startswith('.') or part.endswith('.lock')
                                                          for part in value.split('/'))):
        raise ValueError('invalid Git ref name')
    return value


def release_ref(value):
    if commit_hash(value):
        return value
    if isinstance(value, str) and value.startswith('refs/tags/'):
        return 'refs/tags/' + ref_name(value.removeprefix('refs/tags/'))
    raise ValueError('a release mapping requires an exact tag ref or full commit')


def current_identity(package, upstream, source_repo, releases):
    for key, field in [('source_commit', 'commit'), ('source_tag', 'tag')]:
        pinned = package.get(key)
        if pinned:
            repository = pinned['repository']
            if repository.casefold() not in (upstream.casefold(), source_repo.casefold()):
                return {'reason': 'The Source0 repository differs from the reviewed repositories.'}
            ref = pinned[field] if field == 'commit' else 'refs/tags/' + pinned[field]
            target = {'repository': repository, 'ref': release_ref(ref), 'basis': 'RPM Source0'}
            archive = next((s['url'] for s in package.get('source_archives') or [] if s['number'] == 0), None)
            if archive:
                url = urlsplit(archive)
                if (url.scheme == 'https' and url.hostname in ('github.com', 'codeload.github.com')
                        and not url.username and not url.password and not url.query and len(archive) <= 8192):
                    target['archive'] = url._replace(fragment='').geturl()
            return target
    if ref := releases.get(package.get('version')):
        return {'repository': source_repo, 'ref': ref, 'basis': 'Reviewed release mapping'}
    return {'reason': 'No corroborated Source0 tag, commit or reviewed mapping for this version.'}


def inputs(package, configured):
    if configured is None:
        return None
    allowed = {'repository', 'source_repository', 'baseline', 'branch', 'tag_pattern', 'releases',
               'keywords', 'regex', 'paths'}
    if not isinstance(configured, dict) or set(configured) - allowed:
        raise ValueError('invalid upstream commit configuration')
    upstream = public_repository(configured['repository'])
    source_repo = public_repository(configured.get('source_repository', upstream))
    rules = {}
    for key in ('keywords', 'regex', 'paths'):
        values = configured.get(key, [])
        if (not isinstance(values, list) or len(values) > 16
                or any(not isinstance(v, str) or not v.strip() or len(v) > 160 for v in values)):
            raise ValueError('expected a short list of ' + key)
        rules[key] = sorted(set(values))
    if not any(rules.values()):
        raise ValueError('at least one commit discovery rule is required')
    for expression in rules['regex']:
        re.compile(expression, re.IGNORECASE)
    if not commit_hash(configured.get('baseline')):
        raise ValueError('expected an immutable baseline commit')
    pattern = configured.get('tag_pattern')
    if pattern is not None:
        if not isinstance(pattern, str) or pattern.count('{version}') != 1:
            raise ValueError('a release tag pattern requires one {version}')
        ref_name(pattern.replace('{version}', '1'))
    releases = {version: release_ref(value) for version, value in configured.get('releases', {}).items()}
    return {'repository': upstream, 'branch': ref_name(configured['branch']), 'baseline': configured['baseline'],
            'rules': rules, 'current': current_identity(package, upstream, source_repo, releases), 'tag_pattern': pattern}


def candidate_identity(settings, version):
    pattern = settings['tag_pattern']
    if pattern is not None:
        try:
            ref = 'refs/tags/' + ref_name(pattern.replace('{version}', version))
            return {'repository': settings['repository'], 'ref': ref, 'basis': 'Reviewed candidate tag pattern'}
        except ValueError:
            pass
    return {'reason': 'The selected upgrade has no reviewed release tag mapping.'}


def match(commit, rules):
    message = commit.message.casefold()
    reasons = [f'Keyword "{word}"' for word in rules['keywords'] if word.casefold() in message]
    reasons += [f'Regex /{pattern}/' for pattern in rules['regex'] if re.search(pattern, commit.message, re.I)]
    for pattern in rules['paths']:
        hits = [path for path in commit.paths if fnmatch.fnmatchcase(path, pattern)]
        if hits:
            example = min(hits)
            reasons.append(f'Path {pattern} · {example[:200]}' + (f' (+{len(hits) - 1} more)' if len(hits) > 1 else ''))
    return reasons

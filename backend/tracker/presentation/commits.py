"""Show a matched upstream change once, with its current and candidate witnesses."""
from urllib.parse import quote

from tracker.monitors.issues import INCLUSION_LABELS
from tracker.presentation.labels import caption
from tracker.presentation.model import Entry, Section
from tracker.presentation.values import check_value, field, retained_marker, stamp, text
from tracker.readmodel.commits import FILTER_TAGS, select


def section(name, result, choice):
    check = result['check']
    if check['status'] in ('not_configured', 'not_applicable'):
        return []
    detail = '/packages/' + quote(name, safe='')
    filters = [text(caption(label), href=None if key == choice else detail + (f'?commit_state={key}' if key else '') + '#commits',
                    kind='tag') for key, label in [(None, 'All'), *FILTER_TAGS.items()]]
    fields = [field('Filter commits', *filters), field('Observation', check_value(check), stamp(check.get('checked_at')))]
    entries = {}
    for item in select(result, choice)['data']['findings']:
        url = item['evidence_url']
        facts = {f['code']: f for f in item['facts'] if f.get('code')}
        if url not in entries:
            entry = Entry(heading=[text(item['title'], href=url)],
                          fields=[field('Matched rules', *(text(reason) for reason in facts['match_reasons']['value']))])
            entries[url] = entry
        entry = entries[url]
        target = facts['target_commit']
        version = facts['release_version']['value']
        scope = 'Current' if item['scope'] == 'current' else 'Candidate'
        label = INCLUSION_LABELS.get(facts['inclusion']['value'], 'InclusionUnknown')
        values = [text(version, kind='code'), text(caption(label), kind='tag')]
        if item['stale']:
            values.append(retained_marker())
            if checked := item.get('checked_at'):
                values.append(stamp(checked))
        if ref := facts.get('target_ref'):
            values.append(text(ref['value'], href=ref['url'], title=ref['source']))
        if target['value']:
            values.append(text(target['value'][:12], kind='code', href=target['url'], title=target['value']))
        entry.fields.append(field(scope, *values))
        notes = [text(facts['inclusion_reason']['value'])]
        for fact in item['facts']:
            if fact.get('code') in ('witness', 'revert'):
                notes.append(text(f"{fact['key']} {fact['value'][:12]}", href=fact['url']))
        entry.fields.append(field(f'{scope} evidence', *notes))
        if 'InclusionChanged' in item['tags']:
            entry.metadata.append(text('Changed in candidate', kind='tag'))
    note = check.get('note')
    notes = [note] if note else []
    if not entries and check.get('checked_at'):
        notes.append('No saved commits match this filter.' if choice else 'No matched commits in this observation.')
    if entries:
        notes.append('Evidence covers Git source history. Build options and downstream RPM patches have separate provenance.')
    return [Section(id='commits', title=f'Upstream commits · {len(entries)}', fields=fields,
                    entries=list(entries.values()), notes=notes)]

"""Private, repeatable review inventory. An inventory never approves source facts."""
from collections import Counter, defaultdict
from datetime import date
import hashlib
import json
from pathlib import Path
import re

from .core import WindError, atomic_write, canonical, utc_now


def quarter_end(quarter):
    match = re.fullmatch(r'(\d{4})-Q([1-4])', quarter or '')
    if not match:
        return None
    year, quarter = map(int, match.groups())
    return date(year, quarter * 3, (31, 30, 30, 31)[quarter - 1]).isoformat()


def make_worklist(store):
    documents = {d['id']: d for d in store.all('documents')}
    inspected = set()
    for validation in store.all('validations'):
        document = documents.get(validation.get('document_id'))
        if (not document or validation.get('approved') is not True
                or validation.get('source_sha256') != document['sha256']):
            continue
        for number in validation.get('pages_visually_inspected', []):
            if type(number) is int and 1 <= number <= document.get('page_count', 0):
                inspected.add((document['id'], number))
    pages = {}
    for row in store.db.execute('SELECT * FROM pages ORDER BY document_id,page'):
        key = (row['document_id'], row['page'])
        pages[key] = {'document_id': key[0], 'page': key[1],
                      'status': row['status'], 'reviewed': bool(row['reviewed']),
                      'image_inspected': key in inspected,
                      'issue_counts': Counter(), 'row_ids': [], 'wind_rows': []}
    issues = Counter()
    classifications = Counter()
    observations = []
    number_groups = defaultdict(list)
    previous = None
    continuations = []
    linked_continuations = []
    for row in store.db.execute('SELECT * FROM extracted_rows ORDER BY document_id,page,position'):
        value = json.loads(row['data'])
        key = (row['document_id'], row['page'])
        page = pages[key]
        classifications[row['classification']] += 1
        page['row_ids'].append(row['id'])
        issue = value.get('issue')
        if row['classification'] == 'review':
            issue = issue or 'unresolved'
            issues[issue] += 1
            page['issue_counts'][issue] += 1
        candidate = value.get('candidate')
        if (issue == 'detached_continuation_or_ditto' and previous and previous['document_id'] == key[0]
                and key[1] - previous['page'] <= 1):
            continuations.append({'row_id': row['id'], 'page': key[1],
                                  'suggested_previous': dict(previous), 'review_state': 'pending'})
        parent_id = value.get('continuation_of_row_id')
        if parent_id and row['classification'] == 'review':
            linked_continuations.append({'document_id': key[0], 'page': key[1], 'row_id': row['id'],
                                         'continuation_of_row_id': parent_id, 'issue': issue,
                                         'review_state': 'pending'})
        if candidate:
            previous = {'document_id': key[0], 'page': key[1], 'row_id': row['id']}
        if not candidate or '풍력' not in candidate.get('fuel', ''):
            continue
        end = quarter_end(documents[key[0]].get('quarter'))
        observed_date = candidate.get('permit_or_change_date', {}).get('end')
        flags = []
        if candidate.get('fuel') != '육상풍력' and '해상' not in candidate.get('fuel', ''):
            flags.append('onshore_offshore_unconfirmed')
        if end and observed_date and observed_date > end:
            flags.append('disposition_after_document_quarter')
        if row['classification'] == 'review':
            flags.append(issue)
        if type(candidate.get('capacity_w')) is not int:
            flags.append('capacity_not_resolved')
        if not candidate.get('permit_or_change_date', {}).get('end'):
            flags.append('disposition_date_not_resolved')
        if not candidate.get('permit_year'):
            flags.append('register_year_not_resolved')
        item = {'document_id': key[0], 'page': key[1], 'row_id': row['id'],
                'source_sha256': documents[key[0]]['sha256'],
                'candidate': candidate, 'flags': flags, 'review_state': 'pending'}
        observations.append(item)
        page['wind_rows'].append(row['id'])
        if candidate.get('permit_year') and candidate.get('number'):
            number_groups[(key[0], candidate['permit_year'], candidate['number'])].append(item)
    repeats = []
    for key, items in number_groups.items():
        if len(items) > 1:
            repeats.append({'document_id': key[0], 'register_year': key[1], 'register_number': key[2],
                            'rows': [v['row_id'] for v in items],
                            'distinct_capacity_w': sorted({v['candidate']['capacity_w'] for v in items
                                                           if 'capacity_w' in v['candidate']}),
                            'identity_confirmed': False})
    source_checks = []
    for document in documents.values():
        asset = store.root / document['asset']
        valid = asset.is_file() and hashlib.sha256(asset.read_bytes()).hexdigest() == document['sha256']
        source_checks.append({'document_id': document['id'], 'hash_valid': valid})
    entries = []
    for page in pages.values():
        page['issue_counts'] = dict(page['issue_counts'])
        if not page['reviewed']:
            entries.append(page)
    entries.sort(key=lambda p: (p['image_inspected'], -bool(p['wind_rows']), -len(p['wind_rows']), p['document_id'], p['page']))
    return {'schema_version': 1, 'generated_at': utc_now(), 'inventory_only': True,
            'summary': {'documents': len(documents), 'pages': len(pages),
                        'reviewed_pages': sum(p['reviewed'] for p in pages.values()),
                        'visually_inspected_pages': len(inspected.intersection(pages)),
                        'pending_pages': len(entries), 'row_classifications': dict(classifications),
                        'review_issues': dict(issues), 'wind_observations': len(observations),
                        'number_groups_requiring_identity_review': len(repeats)},
            'source_checks': source_checks, 'pages': entries, 'wind_observations': observations,
            'repeated_register_numbers': repeats, 'continuation_suggestions': continuations,
            'linked_continuations': linked_continuations}


def export_worklist(store, output):
    target = Path(output).resolve()
    if not target.is_relative_to(store.root) or any(p.is_symlink() for p in (target, *target.parents)):
        raise WindError('Review worklist must stay inside private runtime without symlinks')
    worklist = make_worklist(store)
    if not all(c['hash_valid'] for c in worklist['source_checks']):
        raise WindError('Review source hash mismatch')
    atomic_write(target, canonical(worklist))
    return {'output': str(target), 'inventory_only': True, **worklist['summary']}

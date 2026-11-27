import copy
import json
from pathlib import Path
import tempfile
import unittest

from wind_map.core import WindError
from wind_map.review_worklist import export_worklist, make_worklist, quarter_end
from wind_map.store import Store
from wind_map.extract import parse_tables, register_date
from wind_map_helpers import seed


class ReviewWorklistTests(unittest.TestCase):
    def test_plan_text_markers_preserve_preparation_date_without_inventing_approval(self):
        header = ['NO', '', '상호(사업자)', '발전소 위치', '원동력', '용량(MW)',
                  '허가및변경일', '공사계획인가기간', '사업준비기간', '기타']
        for marker in ['해당없음', '기완료']:
            with self.subTest(marker=marker):
                source = ['2023', '1', '사업자', '주소', '육상풍력', '4.2',
                          '2023-08-01', marker, '2028-01-31', '사업준비기간 연장']
                rows, _ = parse_tables([[header, source]])
                candidate = rows[1]['candidate']
                self.assertEqual(candidate['preparation_deadline']['end'], '2028-01-31')
                self.assertIsNone(candidate['construction_plan_deadline']['end'])
                self.assertEqual(candidate['construction_plan_deadline_source_status'], marker)
                self.assertEqual(candidate['construction_plan_deadline_raw'], marker)
                self.assertEqual(rows[1]['cells'], source)
                self.assertEqual(rows[1]['classification'], 'candidate')

    def test_invalid_earlier_date_does_not_suppress_later_deadlines_or_review(self):
        header = ['NO', '', '상호(사업자)', '발전소 위치', '원동력', '용량(MW)',
                  '허가및변경일', '공사계획인가기간', '사업준비기간', '기타']
        source = ['2023', '1', '사업자', '주소', '육상풍력', '4.2',
                  '2023-11-31', '2027-08-31', '2029-08-31', '']
        rows, _ = parse_tables([[header, source]])
        candidate = rows[1]['candidate']
        self.assertNotIn('permit_or_change_date', candidate)
        self.assertEqual(candidate['construction_plan_deadline']['end'], '2027-08-31')
        self.assertEqual(candidate['preparation_deadline']['end'], '2029-08-31')
        self.assertIn('permit_or_change_raw', rows[1]['field_issues'])
        self.assertEqual(rows[1]['classification'], 'review')
        source[6:8] = ['2023-08-01', '2027-02-30']
        rows, _ = parse_tables([[header, source]])
        self.assertNotIn('construction_plan_deadline', rows[1]['candidate'])
        self.assertEqual(rows[1]['candidate']['preparation_deadline']['end'], '2029-08-31')
        self.assertIn('construction_plan_deadline_raw', rows[1]['field_issues'])
        self.assertEqual(rows[1]['classification'], 'review')

    def test_invalid_capacity_does_not_discard_valid_source_dates(self):
        header = ['NO', '상호(사업자)', '발전소 위치', '원동력', '용량(MW)',
                  '허가및변경일', '사업준비기간', '기타']
        rows, _ = parse_tables([[header, ['1', '사업자', '주소', '육상풍력', '미상',
                                         '2023-08-01', '2028-01-31', '']]], inherited_year=2023)
        self.assertNotIn('capacity_w', rows[1]['candidate'])
        self.assertEqual(rows[1]['candidate']['permit_or_change_date']['start'], '2023-08-01')
        self.assertEqual(rows[1]['candidate']['preparation_deadline']['end'], '2028-01-31')
        self.assertIn('capacity_raw', rows[1]['field_issues'])
        self.assertEqual(rows[1]['classification'], 'review')

    def test_unconfirmed_register_year_is_visible_without_inventing_identity(self):
        with tempfile.TemporaryDirectory() as temporary, Store(temporary) as store:
            document_id, _ = seed(store, approved=False)
            candidate = {'permit_year': None, 'number': '1', 'name': '사업자',
                         'fuel': '풍력', 'capacity_w': 90000000,
                         'permit_or_change_date': {'end': '2019-01-30'}}
            store.record_page(document_id, 1, [{'classification': 'review',
                                               'candidate': candidate}], status='extracted')
            report = make_worklist(store)
            self.assertIn('register_year_not_resolved', report['wind_observations'][0]['flags'])
            self.assertIsNone(report['wind_observations'][0]['candidate']['permit_year'])
            self.assertEqual(report['repeated_register_numbers'], [])

    def test_explicit_prior_register_reference_preserves_section_year(self):
        header = ['NO', '상호(사업자)', '발전소 위치', '원동력', '용량(MW)',
                  '허가및변경일', '사업준비기간', '기타']
        table = [header, ['2015\n-45', '사업자', '주소', '풍력', '20',
                          '2017-03-20', '2018-10-31', '사업준비기간 연장']]
        rows, _ = parse_tables([table], inherited_year=2017)
        candidate = rows[1]['candidate']
        self.assertEqual((candidate['permit_year'], candidate['number']), ('2015', '45'))
        self.assertEqual(candidate['register_section_year'], '2017')
        self.assertEqual(candidate['register_reference_raw'], '2015-45')
        self.assertEqual(candidate['permit_or_change_date']['start'], '2017-03-20')
        self.assertEqual(rows[1]['classification'], 'review')

    def test_unspecified_renewable_fuel_requires_review(self):
        header = ['NO', '상호(사업자)', '발전소 위치', '원동력', '용량(MW)',
                  '허가및변경일', '사업준비기간', '기타']
        table = [header] + [[str(i), '사업자', '주소', fuel, '97',
                             '2013-09-30', '', '']
                            for i, fuel in enumerate(['신재생', '신재생에너지', '재생에너지',
                                                      '신재생(태양광)', '신재생(풍력)', '신재생(풍'], 1)]
        rows, _ = parse_tables([table], inherited_year=2013)
        self.assertEqual([r['classification'] for r in rows[1:]],
                         ['review', 'review', 'review', 'out_of_scope', 'review', 'review'])
        self.assertEqual(rows[1]['issue'], 'renewable_subtype_unconfirmed')
        self.assertEqual(rows[-1]['issue'], 'truncated_wind_fuel_requires_context')

    def test_legacy_korean_months_preserve_precision_without_inventing_duration_dates(self):
        for raw, start, end in [("'07.3월",'2007-03-01','2007-03-31'),
                                ('2013년 9월','2013-09-01','2013-09-30'),
                                ('‘14년11월','2014-11-01','2014-11-30')]:
            value = register_date(raw)
            self.assertEqual((value['start'],value['end'],value['precision']),(start,end,'month'))
        for raw in ['19개월','4.26','2013년9월 이후']:
            with self.assertRaises(WindError):
                register_date(raw)

    def test_inventory_preserves_unresolved_subtype_repeated_numbers_and_future_quarter(self):
        with tempfile.TemporaryDirectory() as temporary, Store(temporary) as store:
            document_id, _ = seed(store, approved=False)
            initial = {'permit_year': '2025', 'number': '82', 'fuel': '육상풍력',
                       'name': '사업자', 'capacity_w': 15000000,
                       'permit_or_change_date': {'end': '2025-11-03'}}
            changed = copy.deepcopy(initial)
            changed.update(capacity_w=21000000, permit_or_change_date={'end': '2026-10-13'})
            ambiguous = {**initial, 'number': '83', 'fuel': '풍력'}
            store.record_page(document_id, 1, [
                {'classification': 'candidate', 'candidate': initial},
                {'classification': 'review', 'issue': 'detached_continuation_or_ditto', 'cells': ['', '력']},
                {'classification': 'candidate', 'candidate': changed},
                {'classification': 'review', 'issue': 'wind_subtype_unconfirmed', 'candidate': ambiguous},
            ], status='extracted')
            before = store.db.total_changes
            report = make_worklist(store)
            self.assertEqual(store.db.total_changes, before)
            self.assertEqual(report['summary']['reviewed_pages'], 0)
            self.assertEqual(report['summary']['wind_observations'], 3)
            self.assertEqual(report['repeated_register_numbers'][0]['distinct_capacity_w'], [15000000, 21000000])
            self.assertFalse(report['repeated_register_numbers'][0]['identity_confirmed'])
            self.assertIn('disposition_after_document_quarter', report['wind_observations'][1]['flags'])
            self.assertIn('onshore_offshore_unconfirmed', report['wind_observations'][2]['flags'])
            self.assertEqual(report['continuation_suggestions'][0]['review_state'], 'pending')

    def test_export_stays_private_and_rejects_corrupt_original(self):
        with tempfile.TemporaryDirectory() as temporary, Store(temporary) as store:
            document_id, _ = seed(store, approved=False)
            with self.assertRaises(WindError):
                export_worklist(store, Path(temporary).parent / 'public-review.json')
            output = Path(temporary) / 'operator' / 'worklist.json'
            export_worklist(store, output)
            self.assertTrue(json.loads(output.read_text())['inventory_only'])
            (store.root / store.document(document_id)['asset']).write_bytes(b'corrupt')
            with self.assertRaises(WindError):
                export_worklist(store, output)

    def test_unresolved_continuation_exposes_existing_parent_without_approval(self):
        with tempfile.TemporaryDirectory() as temporary, Store(temporary) as store:
            document_id, _ = seed(store, approved=False)
            store.record_page(document_id, 1, [
                {'id': 'parent', 'classification': 'review', 'issue': 'wind_subtype_unconfirmed',
                 'candidate': {'fuel': '풍력', 'name': '사업자'}},
                {'id': 'continuation', 'classification': 'review',
                 'issue': 'wind_continuation_subtype_unconfirmed', 'continuation_of_row_id': 'parent',
                 'cells': ['', '산 1']},
            ], status='extracted')
            linked = make_worklist(store)['linked_continuations']
            self.assertEqual(len(linked), 1)
            self.assertEqual(linked[0]['continuation_of_row_id'], 'parent')
            self.assertEqual(linked[0]['review_state'], 'pending')
            self.assertEqual(make_worklist(store)['summary']['reviewed_pages'], 0)

    def test_detached_candidate_never_suggests_itself_as_parent(self):
        with tempfile.TemporaryDirectory() as temporary, Store(temporary) as store:
            document_id, _ = seed(store, approved=False)
            store.record_page(document_id, 1, [
                {'id': 'parent', 'classification': 'candidate', 'candidate': {'fuel': '풍력'}},
                {'id': 'fragment', 'classification': 'review', 'issue': 'detached_continuation_or_ditto',
                 'candidate': {'fuel': ''}},
            ], status='extracted')
            suggestions = make_worklist(store)['continuation_suggestions']
            expected, = store.db.execute('SELECT id FROM extracted_rows WHERE document_id=? AND page=? AND position=?',
                                         (document_id, 1, 0)).fetchone()
            self.assertEqual(suggestions[0]['suggested_previous']['row_id'], expected)

    def test_quarter_boundaries(self):
        self.assertEqual(quarter_end('2026-Q1'), '2026-03-31')
        self.assertEqual(quarter_end('2026-Q3'), '2026-09-30')
        self.assertIsNone(quarter_end('unknown'))

    def test_visual_inspection_never_counts_as_page_approval_and_invalid_pages_are_ignored(self):
        with tempfile.TemporaryDirectory() as temporary, Store(temporary) as store:
            document_id, _ = seed(store, approved=False)
            store.import_bundle({'schema_version':1,'validations':[{
                'id':'partial','document_id':document_id,'kind':'partial_visual_review','approved':True,
                'source_sha256':store.document(document_id)['sha256'],
                'pages_visually_inspected':[1,1,0,2,True,'1'],
            }]},actor='test',reason='Separate inspection from full classification')
            summary = make_worklist(store)['summary']
            self.assertEqual(summary['visually_inspected_pages'],1)
            self.assertEqual(summary['reviewed_pages'],0)

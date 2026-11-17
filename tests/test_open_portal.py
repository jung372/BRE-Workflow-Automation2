import unicodedata
import unittest
from unittest.mock import Mock, patch

from tests import context  # noqa: F401

from scrapers.open_portal import (
    _read_result,
    MAINTENANCE_MESSAGE,
    fetch_open_portal,
    is_maintenance_page,
    dedupe,
    extract_dept,
    filter_rows_for_keyword,
    normalize,
    search_url,
    title_contains_exact_keyword,
    to_iso_date,
)

# 2026-08-06 open.go.kr infoList.ajax 실측 응답에서 가져온 레코드
REAL_RECORD = {
    "ALL_PROC_INSTT_NM": "경상북도 경주시",
    "CHARGER_NM": "홍길동",
    "CHRG_DEPT_NM": "도시계획과",
    "DOC_NO": "도시계획과-13792",
    "FILE_NM": "[본문] 회신.pdf",
    "INFO_SJ": "전기사업 허가 신청에 따른 회신(경주시루풍력 2호)",
    "NFLST_CHRG_DEPT_NM": "경상북도 경주시 도시개발국 도시계획과",
    "P_DATE": "20260803",
    "PRDCTN_DT": "20260803171129",
    "PRDCTN_INSTT_REGIST_NO": "DCT1B2D6C2EA3A14C62BCFE3A87A4868CBB",
    "R_DATE": "20260804",
}


class ApiResponseTest(unittest.TestCase):
    def response(self, body, status=200):
        return Mock(ok=200 <= status < 300, status=status, text=Mock(return_value=body))

    def test_reports_removed_endpoint_before_parsing_html(self):
        with self.assertRaisesRegex(ValueError, "HTTP 404"):
            _read_result(self.response("<html>Not Found</html>", 404))

    def test_does_not_treat_access_error_as_zero_results(self):
        for body in ('<html>로그인이 필요합니다</html>', '{}', '{"result": null}',
                     '{"result":{"rtnList":{}}}', '{"result":{"rtnList":[null]}}'):
            with self.subTest(body=body), self.assertRaises(ValueError):
                _read_result(self.response(body))

    def test_accepts_genuine_empty_search(self):
        self.assertEqual(
            _read_result(self.response('{"result":{"rtnList":[],"rtnTotal":0}}')),
            {"rtnList": [], "rtnTotal": 0},
        )


class MaintenanceTest(unittest.TestCase):
    NOTICE = (
        "정보공개포털 메뉴 점검 안내\n"
        "점검 중인 메뉴의 이용을 일시 제한합니다\n"
        "제한 기간 2026.11.12.부터 점검 및 정비 완료 시까지"
    )

    def test_detects_official_notice_with_whitespace(self):
        self.assertTrue(is_maintenance_page(self.NOTICE))

    def test_maintenance_link_in_navigation_is_not_an_outage(self):
        self.assertFalse(is_maintenance_page(
            '정보목록 검색 결과 43건 /html/service_inspection.html 점검 안내'
        ))

    def test_stops_before_keyword_requests_and_closes_browser(self):
        playwright = Mock()
        browser = playwright.chromium.launch.return_value
        context = browser.new_context.return_value
        page = context.new_page.return_value
        page.locator.return_value.inner_text.return_value = self.NOTICE
        with patch('scrapers.open_portal.goto_with_retry'):
            items, error = fetch_open_portal({'name': '정보공개 청구'}, playwright)
        self.assertIsNone(items)
        self.assertEqual(error, MAINTENANCE_MESSAGE)
        context.request.post.assert_not_called()
        browser.close.assert_called_once()


class ExtractDeptTest(unittest.TestCase):
    def test_prefers_chrg_dept_nm(self):
        self.assertEqual(extract_dept(REAL_RECORD), "도시계획과")

    def test_falls_back_to_last_token_of_full_path(self):
        """API 계층 구분자는 '>' 가 아니라 공백이다."""
        rec = {"NFLST_CHRG_DEPT_NM": "기후에너지환경부 자연보전국 환경영향평가과"}
        self.assertEqual(extract_dept(rec), "환경영향평가과")

    def test_institution_only_record(self):
        rec = {"CHRG_DEPT_NM": "한국에너지공단", "NFLST_CHRG_DEPT_NM": "한국에너지공단"}
        self.assertEqual(extract_dept(rec), "한국에너지공단")

    def test_blank_fields_yield_empty_string(self):
        self.assertEqual(extract_dept({}), "")
        self.assertEqual(extract_dept({"CHRG_DEPT_NM": "  ", "NFLST_CHRG_DEPT_NM": ""}), "")

    def test_none_values_do_not_raise(self):
        self.assertEqual(extract_dept({"CHRG_DEPT_NM": None, "NFLST_CHRG_DEPT_NM": None}), "")


class ToIsoDateTest(unittest.TestCase):
    def test_converts_yyyymmdd(self):
        self.assertEqual(to_iso_date("20260804"), "2026-08-04")

    def test_truncates_timestamp(self):
        self.assertEqual(to_iso_date("20260803171129"), "2026-08-03")

    def test_invalid_input_returns_empty(self):
        for bad in ("", None, "2026", "abcdefgh"):
            self.assertEqual(to_iso_date(bad), "")


class NormalizeTest(unittest.TestCase):
    def setUp(self):
        self.item = normalize(REAL_RECORD, "시루풍력")

    def test_maps_required_fields(self):
        self.assertEqual(self.item["title"], "전기사업 허가 신청에 따른 회신(경주시루풍력 2호)")
        self.assertEqual(self.item["dept"], "도시계획과")
        self.assertEqual(self.item["date"], "2026-08-04")
        self.assertEqual(self.item["prod_date"], "2026-08-03")
        self.assertEqual(self.item["keyword"], "시루풍력")
        self.assertEqual(self.item["uid"], REAL_RECORD["PRDCTN_INSTT_REGIST_NO"])

    def test_keeps_full_hierarchy_for_reference(self):
        self.assertEqual(self.item["dept_full"], "경상북도 경주시 도시개발국 도시계획과")
        self.assertEqual(self.item["instt"], "경상북도 경주시")

    def test_excludes_personal_information(self):
        """대시보드는 공개되므로 담당자 실명·첨부파일명을 발행하지 않는다."""
        self.assertNotIn("CHARGER_NM", self.item)
        self.assertNotIn("charger", self.item)
        self.assertNotIn("홍길동", str(self.item))
        self.assertNotIn("FILE_NM", self.item)
        self.assertNotIn("회신.pdf", str(self.item))

    def test_url_points_at_keyword_search(self):
        self.assertIn("uniSrhMoreList.do", self.item["url"])
        self.assertIn("kwd=", self.item["url"])

    def test_falls_back_to_composite_uid(self):
        rec = dict(REAL_RECORD)
        rec["PRDCTN_INSTT_REGIST_NO"] = ""
        item = normalize(rec, "시루풍력")
        self.assertTrue(item["uid"])
        self.assertIn("도시계획과-13792", item["uid"])

    def test_missing_fields_do_not_raise(self):
        item = normalize({}, "시루풍력")
        self.assertEqual(item["title"], "")
        self.assertEqual(item["dept"], "")
        self.assertEqual(item["date"], "")


class SearchUrlTest(unittest.TestCase):
    def test_encodes_korean_keyword(self):
        url = search_url("시루풍력")
        self.assertNotIn("시루풍력", url)
        self.assertIn("%EC%8B%9C%EB%A3%A8%ED%92%8D%EB%A0%A5", url)


class ExactTitleKeywordTest(unittest.TestCase):
    def test_accepts_contiguous_keyword(self):
        record = {"INFO_SJ": "개발행위 허가 통보(한국바람 주식회사)"}
        self.assertTrue(title_contains_exact_keyword(record, "한국바람"))

    def test_rejects_tokens_in_different_words(self):
        record = {"INFO_SJ": "한국동서발전 인천 하늬바람 해상풍력"}
        self.assertFalse(title_contains_exact_keyword(record, "한국바람"))

    def test_rejects_whitespace_between_words(self):
        record = {"INFO_SJ": "한국 바람 발전사업"}
        self.assertFalse(title_contains_exact_keyword(record, "한국바람"))

    def test_normalizes_unicode_without_removing_spacing(self):
        decomposed = unicodedata.normalize("NFD", "한국바람")
        record = {"INFO_SJ": f"{decomposed} 발전사업"}
        self.assertTrue(title_contains_exact_keyword(record, "한국바람"))

    def test_blank_title_does_not_match(self):
        self.assertFalse(title_contains_exact_keyword({}, "한국바람"))

    def test_filters_only_keywords_configured_for_exact_title_matching(self):
        rows = [
            {"INFO_SJ": "한국바람 주식회사"},
            {"INFO_SJ": "한국동서발전 인천 하늬바람 해상풍력"},
        ]
        self.assertEqual(
            filter_rows_for_keyword(rows, "한국바람", ("한국바람",)),
            rows[:1],
        )
        self.assertIs(filter_rows_for_keyword(rows, "한국바람", ()), rows)


class DedupeTest(unittest.TestCase):
    def _item(self, uid, date, keyword="시루풍력"):
        return {"uid": uid, "date": date, "keyword": keyword, "title": uid}

    def test_merges_same_document_from_two_keywords(self):
        items = dedupe([
            self._item("A", "2026-08-01", "시루풍력"),
            self._item("A", "2026-08-01", "왕신풍력"),
        ])
        self.assertEqual(len(items), 1)
        self.assertIn("시루풍력", items[0]["keyword"])
        self.assertIn("왕신풍력", items[0]["keyword"])

    def test_does_not_repeat_same_keyword(self):
        items = dedupe([
            self._item("A", "2026-08-01", "시루풍력"),
            self._item("A", "2026-08-01", "시루풍력"),
        ])
        self.assertEqual(items[0]["keyword"], "시루풍력")

    def test_sorts_newest_first(self):
        items = dedupe([
            self._item("A", "2026-08-01"),
            self._item("B", "2026-08-05"),
            self._item("C", "2026-07-20"),
        ])
        self.assertEqual([i["uid"] for i in items], ["B", "A", "C"])

    def test_empty_input(self):
        self.assertEqual(dedupe([]), [])


class StateIntegrationTest(unittest.TestCase):
    def test_item_id_uses_uid_for_open_portal_items(self):
        from state import item_id

        item = normalize(REAL_RECORD, "시루풍력")
        self.assertEqual(item_id(item), REAL_RECORD["PRDCTN_INSTT_REGIST_NO"])

    def test_item_id_unchanged_for_legacy_items(self):
        """기존 5개 사이트의 판정 결과가 바뀌면 안 된다."""
        from state import item_id

        legacy = {"title": "공고 제1호", "date": "2026-08-06", "comp_date": "", "status": ""}
        self.assertEqual(item_id(legacy), "공고 제1호||2026-08-06||||")

    def test_snapshot_entry_includes_dept_and_keyword(self):
        from state import _snapshot_entry

        entry = _snapshot_entry(normalize(REAL_RECORD, "시루풍력"))
        self.assertEqual(entry["dept"], "도시계획과")
        self.assertEqual(entry["keyword"], "시루풍력")

    def test_snapshot_entry_omits_empty_extras(self):
        """기존 사이트 항목에 빈 필드를 추가해 상태 파일을 키우지 않는다."""
        from state import _snapshot_entry

        entry = _snapshot_entry({"title": "공고", "date": "2026-08-06", "url": "u"})
        self.assertNotIn("dept", entry)
        self.assertNotIn("keyword", entry)


if __name__ == "__main__":
    unittest.main()

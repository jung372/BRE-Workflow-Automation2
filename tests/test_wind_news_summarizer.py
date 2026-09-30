import unittest

from news.summarizer import summarize_article, validate_summary


class Budget:
    def __init__(self, allowed=True):
        self.allowed, self.calls = allowed, []

    def reserve(self, *args):
        self.calls.append(args)
        return self.allowed


class Transport:
    def __init__(self, response):
        self.response, self.calls = response, []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.response


class Response:
    status_code = 200

    def __init__(self, payload):
        self.payload = payload

    def json(self):
        return self.payload


class SummarizerTests(unittest.TestCase):
    def setUp(self):
        self.article = {"title": "풍력 공급", "description": "대한풍력은 100MW 공급 MOU를 체결했다.", "evidence_scope": "description"}
        self.cfg = {"enabled": True, "provider": "configured-json", "model": "chosen-model",
                    "endpoint": "https://llm.example.com/json", "allowed_hosts": ["llm.example.com"],
                    "daily_budget": 1, "monthly_budget": 20, "max_request_cost": .02}

    def test_default_description_fallback_never_reads_body(self):
        article = dict(self.article, text="비허용 본문 속 1000MW 본계약")
        result = summarize_article(article)
        self.assertEqual(result["summary"], self.article["description"])
        self.assertTrue(result["review_required"])
        self.assertEqual(result["evidence_scope"], "description")
        self.assertNotIn("1000MW", result["summary"])

    def test_missing_budget_or_guard_never_calls_paid_adapter(self):
        transport = Transport(None)
        result = summarize_article(self.article, self.cfg, transport=transport)
        self.assertEqual(result["fallback_reason"], "LLM_BUDGET_OR_PROVIDER_UNCONFIGURED")
        self.assertEqual(transport.calls, [])

    def test_entities_number_and_negation_cannot_be_fabricated(self):
        evidence = self.article["description"]
        for summary in ["대한풍력은 1000MW 공급 MOU를 체결했다.", "대한풍력은 100MW 공급 본계약을 체결했다.", "대한풍력은 100MW 공급 MOU를 체결하지 않았다."]:
            result = {"summary": summary, "evidence_quotes": [evidence]}
            self.assertFalse(validate_summary(result, evidence))

    def test_valid_adapter_and_cache(self):
        payload = {"summary": self.article["description"], "evidence_quotes": [self.article["description"]], "companies": ["대한풍력"]}
        transport, budget, cache = Transport(Response(payload)), Budget(), {}
        first = summarize_article(self.article, self.cfg, transport, budget, cache)
        second = summarize_article(self.article, self.cfg, transport, budget, cache)
        self.assertEqual(first["validation_status"], "VALIDATED")
        self.assertEqual(first, second)
        self.assertEqual(len(transport.calls), 1)
        self.assertEqual(len(budget.calls), 1)
        self.assertFalse(transport.calls[0][1]["allow_redirects"])

    def test_budget_denial_and_invalid_provider_result_fall_back(self):
        transport = Transport(Response({"summary": "300MW", "evidence_quotes": ["300MW"]}))
        result = summarize_article(self.article, self.cfg, transport, Budget(False))
        self.assertEqual(result["fallback_reason"], "LLM_BUDGET_EXHAUSTED")
        self.assertEqual(transport.calls, [])
        result = summarize_article(self.article, self.cfg, transport, Budget())
        self.assertEqual(result["fallback_reason"], "LLM_EVIDENCE_INVALID")


if __name__ == "__main__":
    unittest.main()

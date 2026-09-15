import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import requests

from radar_v08.qwen import review_finalists


def finalists(*assets):
    return [{"asset": a, "setup_type": "BREAKOUT", "direction": "LONG"} for a in assets]


def ollama_response(reviews_obj):
    import json as _json
    return {"message": {"content": _json.dumps(reviews_obj)}}


def valid_review(asset="BTC"):
    return {
        "reviews": [
            {
                "asset": asset, "setup_type": "BREAKOUT", "direction": "LONG", "market": "SPOT",
                "veto": False, "call_sonnet": True, "call_fable": False, "confidence": "MEDIUM",
                "reason": "coherent breakout with volume", "data_quality_notes": [],
            }
        ]
    }


class TestQwenSchema(unittest.TestCase):
    def test_valid_schema_is_accepted(self):
        calls = []

        def post(_payload):
            calls.append(_payload)
            return ollama_response(valid_review("BTC"))

        result = review_finalists(finalists("BTC"), post_fn=post)
        self.assertEqual(result.status, "OK")
        self.assertIn("BTC", result.reviews)
        self.assertEqual(result.reviews["BTC"].direction, "LONG")
        self.assertEqual(len(calls), 1)  # no retry needed

    def test_no_finalists_is_a_trivial_ok(self):
        result = review_finalists([])
        self.assertEqual(result.status, "OK")
        self.assertEqual(result.reviews, {})

    def test_veto_field_is_parsed(self):
        review = valid_review("BTC")
        review["reviews"][0]["veto"] = True
        review["reviews"][0]["reason"] = "volume expansion contradicts breakout claim"

        result = review_finalists(finalists("BTC"), post_fn=lambda _p: ollama_response(review))
        self.assertEqual(result.status, "OK")
        self.assertTrue(result.reviews["BTC"].veto)

    def test_call_sonnet_field_is_parsed(self):
        review = valid_review("BTC")
        review["reviews"][0]["call_sonnet"] = True
        review["reviews"][0]["call_fable"] = False

        result = review_finalists(finalists("BTC"), post_fn=lambda _p: ollama_response(review))
        self.assertTrue(result.reviews["BTC"].call_sonnet)
        self.assertFalse(result.reviews["BTC"].call_fable)

    def test_call_fable_field_is_parsed(self):
        review = valid_review("BTC")
        review["reviews"][0]["call_sonnet"] = False
        review["reviews"][0]["call_fable"] = True
        review["reviews"][0]["confidence"] = "HIGH"

        result = review_finalists(finalists("BTC"), post_fn=lambda _p: ollama_response(review))
        self.assertTrue(result.reviews["BTC"].call_fable)
        self.assertEqual(result.reviews["BTC"].confidence, "HIGH")


class TestQwenInvalidJson(unittest.TestCase):
    def test_invalid_json_retries_once_then_unavailable(self):
        calls = {"n": 0}

        def post(_payload):
            calls["n"] += 1
            return {"message": {"content": "not json at all"}}

        result = review_finalists(finalists("BTC"), post_fn=post)
        self.assertEqual(result.status, "UNAVAILABLE")
        self.assertEqual(calls["n"], 2)  # exactly one retry

    def test_second_attempt_succeeding_after_first_invalid_recovers(self):
        calls = {"n": 0}

        def post(_payload):
            calls["n"] += 1
            if calls["n"] == 1:
                return {"message": {"content": "garbage"}}
            return ollama_response(valid_review("BTC"))

        result = review_finalists(finalists("BTC"), post_fn=post)
        self.assertEqual(result.status, "OK")
        self.assertEqual(calls["n"], 2)


class TestQwenUnknownSymbol(unittest.TestCase):
    def test_unknown_symbol_is_rejected(self):
        def post(_payload):
            return ollama_response(valid_review("NOTINPUT"))

        result = review_finalists(finalists("BTC"), post_fn=post)
        self.assertEqual(result.status, "UNAVAILABLE")  # rejected on both attempts

    def test_missing_review_for_a_finalist_is_rejected(self):
        def post(_payload):
            return ollama_response(valid_review("BTC"))

        result = review_finalists(finalists("BTC", "ETH"), post_fn=post)
        self.assertEqual(result.status, "UNAVAILABLE")


class TestQwenUnavailable(unittest.TestCase):
    def test_timeout_marks_unavailable_without_crashing(self):
        def post(_payload):
            raise requests.Timeout("simulated timeout")

        result = review_finalists(finalists("BTC"), post_fn=post)
        self.assertEqual(result.status, "TIMEOUT")
        self.assertEqual(result.reviews, {})

    def test_connection_error_marks_unavailable(self):
        def post(_payload):
            raise requests.ConnectionError("ollama not running")

        result = review_finalists(finalists("BTC"), post_fn=post)
        self.assertEqual(result.status, "UNAVAILABLE")


if __name__ == "__main__":
    unittest.main()

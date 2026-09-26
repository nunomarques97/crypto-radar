import os
import sys
import unittest
from unittest.mock import Mock, patch

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


class TestQwenMalformedResponse(unittest.TestCase):
    def malformed_responses(self):
        yield "deep-json", {"message": {"content": "[" * 5000 + "]" * 5000}}
        yield "huge-integer", {"message": {"content": '{"reviews":' + "1" * 5000 + "}"}}
        for value in (None, [], "text", 5, True):
            yield f"envelope-{value!r}", value
            yield f"message-{value!r}", {"message": value}
            yield f"root-{value!r}", ollama_response(value)
        for value in (None, [], {}, 5, True):
            yield f"content-{value!r}", {"message": {"content": value}}
        yield "missing-message", {}
        yield "missing-content", {"message": {}}
        fields = {
            "asset": (None, [], {}, 5, True),
            "reason": (None, [], {}, 5, True),
            "data_quality_notes": (None, "warning", {}, 5, True, [1], [None], [[]]),
        }
        for field, values in fields.items():
            for value in values:
                response = valid_review()
                response["reviews"][0][field] = value
                yield f"{field}-{value!r}", ollama_response(response)
            response = valid_review()
            del response["reviews"][0][field]
            yield f"missing-{field}", ollama_response(response)

    def test_wrong_shapes_retry_then_return_unavailable_without_partial_reviews(self):
        for name, response in self.malformed_responses():
            with self.subTest(case=name):
                post = Mock(return_value=response)
                result = review_finalists(finalists("BTC"), post_fn=post)
                self.assertEqual(result.status, "UNAVAILABLE")
                self.assertEqual(result.reviews, {})
                self.assertTrue(result.error)
                self.assertEqual(post.call_count, 2)

    def test_valid_second_response_recovers_from_each_wrong_shape(self):
        for name, response in self.malformed_responses():
            with self.subTest(case=name):
                post = Mock(side_effect=[response, ollama_response(valid_review())])
                result = review_finalists(finalists("BTC"), post_fn=post)
                self.assertEqual(result.status, "OK")
                self.assertEqual(set(result.reviews), {"BTC"})
                self.assertEqual(post.call_count, 2)

    def test_invalid_review_after_valid_review_rejects_entire_batch(self):
        response = valid_review("BTC")
        invalid = valid_review("ETH")["reviews"][0]
        invalid["data_quality_notes"] = "not an array"
        response["reviews"].append(invalid)
        post = Mock(return_value=ollama_response(response))
        result = review_finalists(finalists("BTC", "ETH"), post_fn=post)
        self.assertEqual(result.status, "UNAVAILABLE")
        self.assertEqual(result.reviews, {})
        self.assertEqual(post.call_count, 2)

    def test_valid_text_fields_keep_existing_reason_limit_and_notes(self):
        response = valid_review()
        response["reviews"][0]["reason"] = "r" * 250
        response["reviews"][0]["data_quality_notes"] = ["thin book", "quote fallback"]
        post = Mock(return_value=ollama_response(response))
        result = review_finalists(finalists("BTC"), post_fn=post)
        self.assertEqual(result.status, "OK")
        self.assertEqual(result.reviews["BTC"].reason, "r" * 200)
        self.assertEqual(result.reviews["BTC"].data_quality_notes, ["thin book", "quote fallback"])
        self.assertEqual(post.call_count, 1)

    def test_http_json_decoder_errors_retry_then_return_unavailable(self):
        for error_type in (ValueError, RecursionError):
            with self.subTest(error=error_type.__name__):
                response = Mock()
                response.json.side_effect = error_type("invalid envelope")
                with patch("radar_v08.qwen.requests.post", return_value=response) as post:
                    result = review_finalists(finalists("BTC"))
                self.assertEqual(result.status, "UNAVAILABLE")
                self.assertEqual(result.reviews, {})
                self.assertEqual(post.call_count, 2)
                self.assertEqual(response.raise_for_status.call_count, 2)

    def test_http_json_decoder_error_then_valid_response_recovers(self):
        for error_type in (ValueError, RecursionError):
            with self.subTest(error=error_type.__name__):
                response = Mock()
                response.json.side_effect = [error_type("invalid envelope"), ollama_response(valid_review())]
                with patch("radar_v08.qwen.requests.post", return_value=response) as post:
                    result = review_finalists(finalists("BTC"))
                self.assertEqual(result.status, "OK")
                self.assertEqual(set(result.reviews), {"BTC"})
                self.assertEqual(post.call_count, 2)

    def test_unrelated_post_error_is_not_swallowed(self):
        post = Mock(side_effect=RuntimeError("internal failure"))
        with self.assertRaisesRegex(RuntimeError, "internal failure"):
            review_finalists(finalists("BTC"), post_fn=post)
        self.assertEqual(post.call_count, 1)


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

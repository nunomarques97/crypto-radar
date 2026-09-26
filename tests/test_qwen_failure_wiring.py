"""Malformed model output must not abort the real, isolated heartbeat."""

from unittest import mock

import test_integrity_wiring as wiring
from test_qwen import ollama_response, valid_review

from radar_v08 import heartbeat, qwen


class TestQwenFailureWiring(wiring.IntegrityWiringBase):
    def assert_invalid_response_finishes_cycle(self, response):
        post = mock.Mock(return_value=response)

        def review(finalists):
            return qwen.review_finalists(finalists, post_fn=post)

        with mock.patch.object(heartbeat, "review_finalists", review):
            output = self.run_cycle(wiring.FakeKraken())

        self.assertEqual(post.call_count, 2)
        self.assertEqual(self.routed_assets(), ["BTC"])
        self.assertEqual(len(self.run_records), 1)
        self.assertEqual(output["data_quality"]["qwen"], "UNAVAILABLE")
        self.assertFalse(self.routed[0].qwen_veto)
        self.assertFalse(self.routed[0].qwen_call_sonnet)
        self.assertFalse(self.routed[0].qwen_call_fable)

    def test_wrong_shape_finishes_cycle_with_deterministic_routing(self):
        self.assert_invalid_response_finishes_cycle(ollama_response(None))

    def test_deep_json_finishes_cycle_with_deterministic_routing(self):
        self.assert_invalid_response_finishes_cycle({"message": {"content": "[" * 5000 + "]" * 5000}})

    def test_huge_integer_finishes_cycle_with_deterministic_routing(self):
        self.assert_invalid_response_finishes_cycle({"message": {"content": '{"reviews":' + "1" * 5000 + "}"}})

    def test_wrong_shape_then_valid_response_finishes_with_review(self):
        post = mock.Mock(side_effect=[ollama_response(None), ollama_response(valid_review())])

        def review(finalists):
            return qwen.review_finalists(finalists, post_fn=post)

        with mock.patch.object(heartbeat, "review_finalists", review):
            output = self.run_cycle(wiring.FakeKraken())

        self.assertEqual(post.call_count, 2)
        self.assertEqual(self.routed_assets(), ["BTC"])
        self.assertEqual(len(self.run_records), 1)
        self.assertEqual(output["data_quality"]["qwen"], "OK")
        self.assertTrue(self.routed[0].qwen_call_sonnet)

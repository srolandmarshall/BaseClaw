#!/usr/bin/env python3

import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
import trace_utils


class TraceUtilsTest(unittest.TestCase):
    def setUp(self):
        trace_utils.clear_trace_context()
        self._saved_env = dict(os.environ)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._saved_env)
        trace_utils.clear_trace_context()

    def test_start_request_trace_uses_incoming_request_id(self):
        os.environ["TRACE_RANKINGS"] = "1"
        ctx = trace_utils.start_request_trace(
            "/api/rankings",
            "GET",
            headers={"X-Request-Id": "req-123", "X-Research-Run-Id": "run-1"},
            args={"pos_type": "P", "count": "60"},
        )
        self.assertEqual(ctx["request_id"], "req-123")
        self.assertEqual(ctx["research_run_id"], "run-1")
        self.assertEqual(ctx["pos_type"], "P")
        self.assertEqual(ctx["count"], 60)

    def test_start_request_trace_generates_request_id(self):
        os.environ["TRACE_RANKINGS"] = "1"
        ctx = trace_utils.start_request_trace("/api/rankings", "GET", headers={}, args={})
        self.assertTrue(ctx["request_id"])

    def test_should_trace_rankings_respects_sample_flag(self):
        os.environ["TRACE_RANKINGS"] = "1"
        trace_utils.set_trace_context({"trace_sampled": True})
        self.assertTrue(trace_utils.should_trace_rankings())

    def test_should_trace_rankings_respects_slow_threshold(self):
        os.environ["TRACE_RANKINGS"] = "1"
        os.environ["TRACE_SLOW_MS"] = "1000"
        trace_utils.set_trace_context({"trace_sampled": False})
        self.assertFalse(trace_utils.should_trace_rankings(duration_ms=999))
        self.assertTrue(trace_utils.should_trace_rankings(duration_ms=1000))

    def test_log_trace_event_has_required_fields(self):
        os.environ["TRACE_RANKINGS"] = "1"
        trace_utils.set_trace_context(
            {
                "request_id": "req-abc",
                "route": "/api/rankings",
                "pos_type": "B",
                "count": 25,
                "trace_sampled": True,
            }
        )
        with patch("builtins.print") as mock_print:
            trace_utils.log_trace_event(
                event="rankings_stage",
                stage="cmd_rankings",
                duration_ms=123,
                cache_hit=True,
                status="ok",
                gate="rankings",
            )
        self.assertEqual(mock_print.call_count, 1)
        payload = mock_print.call_args.args[0]
        self.assertIn('"event":"rankings_stage"', payload)
        self.assertIn('"request_id":"req-abc"', payload)
        self.assertIn('"route":"/api/rankings"', payload)
        self.assertIn('"pos_type":"B"', payload)
        self.assertIn('"count":25', payload)
        self.assertIn('"duration_ms":123', payload)
        self.assertIn('"stage":"cmd_rankings"', payload)
        self.assertIn('"cache_hit":true', payload)
        self.assertIn('"status":"ok"', payload)


if __name__ == "__main__":
    unittest.main()

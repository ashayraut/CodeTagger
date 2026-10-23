"""
Unit tests for code-tagger.py.

Tests cover pure logic only — the claude CLI is mocked so no network
or authentication is needed to run the suite.

Run:
    python3 -m pytest tests/ -v
"""

import json
import sys
import os
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import code_tagger as ct


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def make_file_result(risk="LOW", review="HUMAN_REVIEW", depth="UNIT_ONLY",
                     attention=None, filepath="Foo.java", methods=None):
    return {
        "file": filepath,
        "fileSummary": {
            "overallRiskLevel": risk,
            "primaryAttentionTypes": attention or ["CORRECTNESS"],
            "recommendedReviewRequirement": review,
            "recommendedTestingDepth": depth,
        },
        "methods": methods or [],
    }


SAMPLE_METHOD = {
    "name": "validateToken",
    "className": "AuthService",
    "signature": "Optional<String> validateToken(String token)",
    "riskLevel": "CRITICAL",
    "attentionTypes": ["SECURITY"],
    "reviewRequirement": "SECURITY_REVIEW",
    "testingDepth": "INTEGRATION",
    "rationale": "Auth gate — bypass risk.",
}

VALID_RESPONSE = json.dumps({
    "file": "AuthService.java",
    "fileSummary": {
        "overallRiskLevel": "CRITICAL",
        "primaryAttentionTypes": ["SECURITY"],
        "recommendedReviewRequirement": "SECURITY_REVIEW",
        "recommendedTestingDepth": "INTEGRATION",
    },
    "methods": [SAMPLE_METHOD],
})


# ---------------------------------------------------------------------------
# roll_up tests
# ---------------------------------------------------------------------------

class TestRollUp(unittest.TestCase):

    def test_empty_list_returns_empty(self):
        self.assertEqual(ct.roll_up([]), {})

    def test_all_errors_returns_empty(self):
        result = ct.roll_up([{"file": "Foo.java", "error": "parse failed"}])
        self.assertEqual(result, {})

    def test_single_file_passthrough(self):
        r = ct.roll_up([make_file_result(risk="HIGH", review="SECURITY_REVIEW", depth="E2E")])
        self.assertEqual(r["overallRiskLevel"], "HIGH")
        self.assertEqual(r["recommendedReviewRequirement"], "SECURITY_REVIEW")
        self.assertEqual(r["recommendedTestingDepth"], "E2E")

    def test_picks_highest_risk(self):
        results = [
            make_file_result(risk="LOW"),
            make_file_result(risk="CRITICAL"),
            make_file_result(risk="MEDIUM"),
        ]
        self.assertEqual(ct.roll_up(results)["overallRiskLevel"], "CRITICAL")

    def test_picks_strictest_review(self):
        results = [
            make_file_result(review="HUMAN_REVIEW"),
            make_file_result(review="AUTO_MERGE"),
            make_file_result(review="ARCH_REVIEW"),
        ]
        self.assertEqual(ct.roll_up(results)["recommendedReviewRequirement"], "ARCH_REVIEW")

    def test_picks_deepest_testing(self):
        results = [
            make_file_result(depth="UNIT_ONLY"),
            make_file_result(depth="LOAD"),
            make_file_result(depth="INTEGRATION"),
        ]
        self.assertEqual(ct.roll_up(results)["recommendedTestingDepth"], "LOAD")

    def test_unions_attention_types(self):
        results = [
            make_file_result(attention=["SECURITY", "PERFORMANCE"]),
            make_file_result(attention=["STABILITY"]),
            make_file_result(attention=["SECURITY"]),
        ]
        self.assertEqual(
            sorted(ct.roll_up(results)["primaryAttentionTypes"]),
            ["PERFORMANCE", "SECURITY", "STABILITY"],
        )

    def test_file_counts(self):
        results = [
            make_file_result(),
            make_file_result(),
            {"file": "Bad.java", "error": "failed"},
        ]
        r = ct.roll_up(results)
        self.assertEqual(r["filesAnalyzed"], 2)
        self.assertEqual(r["filesWithErrors"], 1)

    def test_skips_results_without_file_summary(self):
        results = [
            make_file_result(risk="HIGH"),
            {"file": "NoSummary.java", "methods": []},  # no fileSummary key
        ]
        r = ct.roll_up(results)
        self.assertEqual(r["overallRiskLevel"], "HIGH")
        self.assertEqual(r["filesAnalyzed"], 1)


# ---------------------------------------------------------------------------
# analyze_file tests
# ---------------------------------------------------------------------------

class TestAnalyzeFile(unittest.TestCase):

    @patch("code_tagger.call_claude", return_value=VALID_RESPONSE)
    def test_parses_valid_response(self, _mock):
        result = ct.analyze_file("src/AuthService.java", "class AuthService {}")
        self.assertIn("fileSummary", result)
        self.assertEqual(result["fileSummary"]["overallRiskLevel"], "CRITICAL")
        self.assertEqual(len(result["methods"]), 1)

    @patch("code_tagger.call_claude", return_value="```json\n" + VALID_RESPONSE + "\n```")
    def test_strips_markdown_fences(self, _mock):
        result = ct.analyze_file("src/AuthService.java", "class AuthService {}")
        self.assertIn("fileSummary", result)

    @patch("code_tagger.call_claude", return_value="not json at all")
    def test_json_parse_error_returns_error_dict(self, _mock):
        result = ct.analyze_file("src/Broken.java", "class Broken {}")
        self.assertIn("error", result)
        self.assertIn("JSON parse failed", result["error"])

    @patch("code_tagger.call_claude", side_effect=RuntimeError("CLI not found"))
    def test_call_claude_exception_propagates(self, _mock):
        with self.assertRaises(RuntimeError):
            ct.analyze_file("src/Foo.java", "class Foo {}")

    @patch("code_tagger.call_claude", return_value=VALID_RESPONSE)
    def test_file_path_set_on_result(self, _mock):
        result = ct.analyze_file("path/to/AuthService.java", "class AuthService {}")
        self.assertEqual(result["file"], "path/to/AuthService.java")


# ---------------------------------------------------------------------------
# check_claude_cli tests
# ---------------------------------------------------------------------------

class TestCheckClaudeCli(unittest.TestCase):

    @patch("subprocess.check_output")
    def test_passes_when_claude_available(self, mock_check):
        mock_check.return_value = b"claude 1.0.0\n"
        ct.check_claude_cli()  # should not raise

    @patch("subprocess.check_output", side_effect=FileNotFoundError)
    def test_exits_when_claude_missing(self, _mock):
        with self.assertRaises(SystemExit):
            ct.check_claude_cli()


# ---------------------------------------------------------------------------
# Risk/review/depth ordering constants
# ---------------------------------------------------------------------------

class TestOrderingConstants(unittest.TestCase):

    def test_risk_order_is_monotone(self):
        order = ct.RISK_ORDER
        self.assertLess(order["LOW"], order["MEDIUM"])
        self.assertLess(order["MEDIUM"], order["HIGH"])
        self.assertLess(order["HIGH"], order["CRITICAL"])

    def test_review_order_is_monotone(self):
        order = ct.REVIEW_ORDER
        self.assertLess(order["AUTO_MERGE"], order["HUMAN_REVIEW"])
        self.assertLess(order["HUMAN_REVIEW"], order["SECURITY_REVIEW"])
        self.assertLess(order["SECURITY_REVIEW"], order["ARCH_REVIEW"])

    def test_depth_order_is_monotone(self):
        order = ct.DEPTH_ORDER
        self.assertLess(order["UNIT_ONLY"], order["INTEGRATION"])
        self.assertLess(order["INTEGRATION"], order["E2E"])
        self.assertLess(order["E2E"], order["LOAD"])


if __name__ == "__main__":
    unittest.main()

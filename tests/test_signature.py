"""
Unit tests for the pure functions in lambda/investigator/index.py.

These are the only investigator functions worth unit-testing without a
full AWS mock setup: they have no side effects and fully determine
whether two incidents are recognised as the same. The signature
function is what keeps the memory layer from blowing up the bill, so
it has to behave predictably across cosmetic variation.

Run with:
    python3 -m unittest tests.test_signature
"""

from __future__ import annotations

import importlib.util
import os
import pathlib
import sys
import unittest

# Load the investigator module directly without setting AWS env vars
# (boto3 client creation at import time would otherwise fail).
ROOT = pathlib.Path(__file__).resolve().parent.parent
INVESTIGATOR = ROOT / "lambda" / "investigator" / "index.py"

# Provide stubs for the env vars the module reads at top level.
os.environ.setdefault("ECS_CLUSTER", "test-cluster")
os.environ.setdefault("ECS_SERVICE", "test-service")
os.environ.setdefault("PIPELINE_NAME", "test-pipeline")
os.environ.setdefault("ECS_LOG_GROUP", "/aws/ecs/test")
os.environ.setdefault("SLACK_URL_PARAM", "/test/slack")

spec = importlib.util.spec_from_file_location("investigator", INVESTIGATOR)
investigator = importlib.util.module_from_spec(spec)
sys.modules["investigator"] = investigator
spec.loader.exec_module(investigator)


class NormalizeErrorTests(unittest.TestCase):
    def test_strips_iso_timestamps(self):
        a = investigator.normalize_error([
            "2026-04-30T13:35:00.000Z TypeError: Cannot read properties of undefined"
        ])
        b = investigator.normalize_error([
            "2026-05-01T08:12:34.567Z TypeError: Cannot read properties of undefined"
        ])
        self.assertEqual(a, b, "different timestamps should normalise to the same key")

    def test_strips_request_ids(self):
        rid_a = "f855e0e6-6789-45a8-acbe-3c9bb453a6fa"
        rid_b = "9a2364d6-017b-4885-8961-be735222c9b0"
        a = investigator.normalize_error([f"[ERROR] {rid_a} something blew up"])
        b = investigator.normalize_error([f"[ERROR] {rid_b} something blew up"])
        self.assertEqual(a, b, "different request UUIDs should normalise away")

    def test_keeps_error_substance(self):
        out = investigator.normalize_error([
            "2026-04-30T00:00:00Z [ERROR] f855e0e6-6789-45a8-acbe-3c9bb453a6fa "
            "TypeError: Cannot read properties of undefined (reading 'substring')"
        ])
        self.assertIn("typeerror", out)
        self.assertIn("cannot read properties", out)
        self.assertIn("substring", out)

    def test_falls_back_to_recent_lines_when_no_error(self):
        # No "Error" / "Exception" / etc. keywords; should still produce
        # *something* deterministic so signatures are computable.
        out = investigator.normalize_error([
            "GET /health 200",
            "GET /info 200",
            "starting up",
        ])
        self.assertTrue(out)


class IncidentSignatureTests(unittest.TestCase):
    LOGS_BROKEN = [
        "2026-04-30T13:35:00Z [ERROR] req-12345 TypeError: Cannot read properties of undefined",
    ]
    LOGS_OTHER = [
        "2026-04-30T13:35:00Z [ERROR] req-67890 ENOENT: no such file or directory '/etc/missing'",
    ]
    PIPELINE_FAILED = [{"id": "exec-1", "status": "Failed"}]
    PIPELINE_OK = [{"id": "exec-2", "status": "Succeeded"}]

    def test_starts_with_sig_prefix(self):
        sig = investigator.build_incident_signature(
            "alarm-x", "svc-x", self.LOGS_BROKEN, self.PIPELINE_FAILED
        )
        self.assertTrue(sig.startswith("sig_"), sig)

    def test_same_incident_same_signature(self):
        a = investigator.build_incident_signature(
            "task-count-drop", "svc-y", self.LOGS_BROKEN, self.PIPELINE_FAILED
        )
        b = investigator.build_incident_signature(
            "task-count-drop", "svc-y", self.LOGS_BROKEN, self.PIPELINE_FAILED
        )
        self.assertEqual(a, b)

    def test_different_alarm_different_signature(self):
        a = investigator.build_incident_signature(
            "task-count-drop", "svc-y", self.LOGS_BROKEN, self.PIPELINE_FAILED
        )
        b = investigator.build_incident_signature(
            "high-5xx", "svc-y", self.LOGS_BROKEN, self.PIPELINE_FAILED
        )
        self.assertNotEqual(a, b)

    def test_different_error_different_signature(self):
        a = investigator.build_incident_signature(
            "task-count-drop", "svc-y", self.LOGS_BROKEN, self.PIPELINE_FAILED
        )
        b = investigator.build_incident_signature(
            "task-count-drop", "svc-y", self.LOGS_OTHER, self.PIPELINE_FAILED
        )
        self.assertNotEqual(a, b)

    def test_pipeline_status_change_changes_signature(self):
        a = investigator.build_incident_signature(
            "task-count-drop", "svc-y", self.LOGS_BROKEN, self.PIPELINE_FAILED
        )
        b = investigator.build_incident_signature(
            "task-count-drop", "svc-y", self.LOGS_BROKEN, self.PIPELINE_OK
        )
        self.assertNotEqual(a, b, "failed vs succeeded pipeline should affect signature")


class AutoActionGateTests(unittest.TestCase):
    """Exercise the auto-remediation gate decision tree."""

    def setUp(self):
        # Force a clean env for these tests.
        investigator.AUTO_ENABLED = True
        investigator.AUTO_THRESHOLD = 0.85
        investigator.AUTO_ALLOWED = {"rollback", "create_pr"}
        investigator.AUTO_REQUIRE_LOW_RISK = True

    def _decision(self, **overrides):
        base = {
            "confidence": 0.9,
            "risk_level": "LOW",
            "auto_remediation_safe": True,
            "recommended_option": "option_b",
            "patches": [],
        }
        base.update(overrides)
        return base

    def test_low_confidence_blocks(self):
        action, reason = investigator.auto_action_for(self._decision(confidence=0.5))
        self.assertIsNone(action)
        self.assertIn("confidence", reason)

    def test_high_risk_blocks(self):
        action, reason = investigator.auto_action_for(self._decision(risk_level="HIGH"))
        self.assertIsNone(action)
        self.assertIn("HIGH", reason)

    def test_unsafe_flag_blocks(self):
        action, _ = investigator.auto_action_for(self._decision(auto_remediation_safe=False))
        self.assertIsNone(action)

    def test_rollback_path_passes_when_all_gates_satisfied(self):
        action, _ = investigator.auto_action_for(self._decision())
        self.assertEqual(action, "rollback")

    def test_create_pr_requires_patches(self):
        action, reason = investigator.auto_action_for(
            self._decision(recommended_option="option_pr", patches=[])
        )
        self.assertIsNone(action)
        self.assertIn("patches", reason)

    def test_create_pr_passes_with_patches(self):
        action, _ = investigator.auto_action_for(
            self._decision(
                recommended_option="option_pr",
                patches=[{"file_path": "App/server.js", "find": "x", "replace": "y"}],
            )
        )
        self.assertEqual(action, "create_pr")

    def test_master_switch_off_blocks_everything(self):
        investigator.AUTO_ENABLED = False
        action, reason = investigator.auto_action_for(self._decision())
        self.assertIsNone(action)
        self.assertIn("disabled", reason)


if __name__ == "__main__":
    unittest.main()

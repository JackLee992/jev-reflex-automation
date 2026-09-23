#!/usr/bin/env python3
from __future__ import annotations

import io
import copy
import json
import math
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import jev_judge
import jev_policy


def choice_policy(
    *,
    risk: str = "low",
    mode: str = "active",
    calibration_status: str = "calibrated",
    window_size: int = 2,
) -> dict[str, object]:
    calibration: dict[str, object] = {"status": calibration_status}
    if calibration_status == "calibrated":
        calibration.update({
            "dataset_version": "routes-holdout-v1",
            "sample_size": 100,
            "report_sha256": "7" * 64,
            "evaluated_at": "2026-01-01T00:00:00+00:00",
        })
    return {
        "policy_version": "route-v1",
        "model": "jev-1.13.0",
        "question_type": "choice",
        "question_id": "route_choice",
        "question_contract_sha256": "1" * 64,
        "state_projection_version": "route-state-v1",
        "risk": risk,
        "action_type": "read_only",
        "autonomous_actions": [] if risk == "high" else ["inspect_route"],
        "governance": {
            "owner": "automation-team",
            "approval_status": "approved",
            "approved_by": "policy-reviewer",
            "approved_at": "2026-01-01T00:00:00+00:00",
            "expires_at": "2099-01-01T00:00:00+00:00",
        },
        "mode": mode,
        "calibration": calibration,
        "thresholds": {
            "routing_signal": "confidence",
            "review_min": 0.60,
            "auto_min": 0.85,
        },
        "monitoring": {
            "minimum_labeled": 1,
            "window_size": window_size,
            "max_brier": 0.15,
            "max_ece": 0.15,
            "max_brier_delta": 0.05,
            "max_ece_delta": 0.05,
            "require_health_report": False,
            "max_health_report_age_seconds": 86400,
            "baseline": {
                "baseline_id": "routes-baseline-v1",
                "window_size": 100,
                "labeled": 100,
                "brier": 0.01,
                "ece": 0.10,
                "report_sha256": "2" * 64,
            },
        },
        "choices": ["a", "b", "none"],
        "abstain_values": ["none"],
    }


def noul_policy() -> dict[str, object]:
    return {
        "policy_version": "noul-v1",
        "model": "jev-1.13.0",
        "question_type": "noul",
        "question_id": "route_noul",
        "question_contract_sha256": "3" * 64,
        "state_projection_version": "noul-state-v1",
        "risk": "low",
        "action_type": "classify_only",
        "autonomous_actions": ["record_label"],
        "governance": {
            "owner": "automation-team",
            "approval_status": "approved",
            "approved_by": "policy-reviewer",
            "approved_at": "2026-01-01T00:00:00+00:00",
            "expires_at": "2099-01-01T00:00:00+00:00",
        },
        "mode": "active",
        "calibration": {
            "status": "calibrated",
            "dataset_version": "noul-holdout-v1",
            "sample_size": 80,
            "report_sha256": "8" * 64,
            "evaluated_at": "2026-01-01T00:00:00+00:00",
        },
        "thresholds": {
            "auto_no_max": 0.10,
            "review_no_max": 0.30,
            "review_yes_min": 0.70,
            "auto_yes_min": 0.90,
        },
        "monitoring": {
            "minimum_labeled": 1,
            "window_size": 2,
            "max_brier": 0.25,
            "max_ece": 0.25,
            "max_brier_delta": 0.10,
            "max_ece_delta": 0.10,
            "require_health_report": False,
            "max_health_report_age_seconds": 86400,
            "baseline": {
                "baseline_id": "noul-baseline-v1",
                "window_size": 80,
                "labeled": 80,
                "brier": 0.08,
                "ece": 0.06,
                "report_sha256": "4" * 64,
            },
        },
    }


def score_policy() -> dict[str, object]:
    return {
        "policy_version": "score-v1",
        "model": "jev-1.13.0",
        "question_type": "score",
        "question_id": "route_score",
        "question_contract_sha256": "5" * 64,
        "state_projection_version": "score-state-v1",
        "risk": "medium",
        "action_type": "classify_only",
        "autonomous_actions": ["record_severity"],
        "governance": {
            "owner": "automation-team",
            "approval_status": "approved",
            "approved_by": "policy-reviewer",
            "approved_at": "2026-01-01T00:00:00+00:00",
            "expires_at": "2099-01-01T00:00:00+00:00",
        },
        "mode": "active",
        "calibration": {
            "status": "calibrated",
            "dataset_version": "severity-holdout-v1",
            "sample_size": 90,
            "report_sha256": "9" * 64,
            "evaluated_at": "2026-01-01T00:00:00+00:00",
        },
        "thresholds": {
            "routing_signal": "confidence",
            "review_min": 0.65,
            "auto_min": 0.90,
        },
        "monitoring": {
            "minimum_labeled": 1,
            "window_size": 2,
            "max_brier": 0.25,
            "max_ece": 0.25,
            "max_brier_delta": 0.10,
            "max_ece_delta": 0.10,
            "require_health_report": False,
            "max_health_report_age_seconds": 86400,
            "baseline": {
                "baseline_id": "score-baseline-v1",
                "window_size": 90,
                "labeled": 90,
                "brier": 0.12,
                "ece": 0.08,
                "report_sha256": "6" * 64,
            },
        },
        "levels": ["low", "medium", "high"],
    }


def registry(policy: dict[str, object] | None = None) -> dict[str, object]:
    return {
        "schema_version": jev_policy.REGISTRY_SCHEMA,
        "registry_version": "engineering-policies-v1",
        "policies": {"route": policy or choice_policy()},
    }


def choice_answer(confidence: float, *, choice: str = "a", selected_probability: float | None = None) -> dict[str, object]:
    probability = confidence if selected_probability is None else selected_probability
    if choice == "a":
        probabilities = {"a": probability, "b": 1.0 - probability, "none": 0.0}
    elif choice == "b":
        probabilities = {"a": 1.0 - probability, "b": probability, "none": 0.0}
    else:
        probabilities = {"a": 0.05, "b": 0.05, "none": 0.90}
    return {
        "type": "choice",
        "choice": choice,
        "confidence": confidence,
        "probabilities": probabilities,
    }


def envelope(
    policy: dict[str, object],
    answer: dict[str, object],
    *,
    judgment_hex: str = "a" * 32,
    request_hash: str = "b" * 64,
    **meta_overrides: object,
) -> dict[str, object]:
    question_id = str(policy["question_id"])
    meta: dict[str, object] = {
        "schema_version": "1",
        "judgment_id": f"jdg_{judgment_hex}",
        "ts": "2026-03-01T00:00:00+00:00",
        "kind": "jev_judgment",
        "request_hash": request_hash,
        "question_contract_hash": policy["question_contract_sha256"],
        "requested_model": policy["model"],
        "response_model": policy["model"],
        "endpoint": jev_judge.DEFAULT_ENDPOINT,
        "question_keys": [question_id],
        "status": "ok",
        "cached": False,
        "state_projection_version": policy["state_projection_version"],
    }
    meta.update(meta_overrides)
    return {
        "response": {
            "model": policy["model"],
            "answers": {question_id: answer},
            "usage": {"input_tokens": 1, "output_tokens": 1},
        },
        "meta": meta,
    }


def trusted_choice_input(
    policy: dict[str, object],
    confidence: float,
    *,
    choice: str = "a",
    selected_probability: float | None = None,
    judgment_hex: str = "a" * 32,
) -> dict[str, object]:
    return envelope(
        policy,
        choice_answer(confidence, choice=choice, selected_probability=selected_probability),
        judgment_hex=judgment_hex,
    )


class PolicyValidationTests(unittest.TestCase):
    def test_repository_shadow_example_binds_exact_request_contract(self) -> None:
        examples = Path(__file__).resolve().parent.parent / "examples"
        request = jev_judge._load_json(str(examples / "route-request.json"))
        registry_value = jev_policy.load_json(str(examples / "policy-shadow.example.json"))
        body, _, _ = jev_judge.prepare_request(request, request["model"])
        policy = jev_policy.validate_registry(registry_value)["policies"]["route_policy"]
        self.assertEqual(policy["question_id"], "route_choice")
        self.assertEqual(
            policy["question_contract_sha256"],
            jev_judge.question_contract_hash(body),
        )
        self.assertEqual(
            policy["state_projection_version"],
            body["state"]["state_projection_version"],
        )

    def test_requires_versioned_per_policy_thresholds(self) -> None:
        valid = jev_policy.validate_registry(registry())
        self.assertEqual(valid["registry_version"], "engineering-policies-v1")
        broken = registry()
        del broken["policies"]["route"]["thresholds"]  # type: ignore[index]
        with self.assertRaisesRegex(jev_policy.PolicyError, "thresholds"):
            jev_policy.validate_registry(broken)

    def test_rejects_nonfinite_threshold_and_unknown_fields(self) -> None:
        broken = registry()
        broken["policies"]["route"]["thresholds"]["auto_min"] = math.nan  # type: ignore[index]
        with self.assertRaises(jev_policy.PolicyError):
            jev_policy.validate_registry(broken)
        broken = registry()
        broken["default_threshold"] = 0.8
        with self.assertRaisesRegex(jev_policy.PolicyError, "unexpected"):
            jev_policy.validate_registry(broken)

        broken = registry()
        broken["policies"]["route"]["thresholds"]["auto_min"] = 10**400  # type: ignore[index]
        with self.assertRaisesRegex(jev_policy.PolicyError, "finite"):
            jev_policy.validate_registry(broken)

    def test_registry_binds_contract_projection_authority_and_approval(self) -> None:
        for field in (
            "question_contract_sha256",
            "question_id",
            "state_projection_version",
            "action_type",
            "autonomous_actions",
            "governance",
        ):
            broken = registry()
            del broken["policies"]["route"][field]  # type: ignore[index]
            with self.assertRaises(jev_policy.PolicyError):
                jev_policy.validate_registry(broken)

        broken = registry()
        broken["policies"]["route"]["action_type"] = "destructive"  # type: ignore[index]
        with self.assertRaisesRegex(jev_policy.PolicyError, "autonomous"):
            jev_policy.validate_registry(broken)

    def test_rejects_invalid_noul_band(self) -> None:
        policy = noul_policy()
        policy["thresholds"]["review_no_max"] = 0.8  # type: ignore[index]
        with self.assertRaises(jev_policy.PolicyError):
            jev_policy.validate_registry(registry(policy))


class RoutingTests(unittest.TestCase):
    def test_choice_routing_boundaries(self) -> None:
        policy = choice_policy()
        reg = registry(policy)
        self.assertEqual(
            jev_policy.evaluate(reg, "route", trusted_choice_input(policy, 0.85),
                                resolved_model="jev-1.13.0", proposed_action="inspect_route")["route"],
            "auto",
        )
        self.assertEqual(
            jev_policy.evaluate(reg, "route", trusted_choice_input(policy, 0.60),
                                resolved_model="jev-1.13.0", proposed_action="inspect_route")["route"],
            "review",
        )
        self.assertEqual(
            jev_policy.evaluate(reg, "route", trusted_choice_input(policy, 0.59),
                                resolved_model="jev-1.13.0", proposed_action="inspect_route")["route"],
            "abstain",
        )
        self.assertEqual(
            jev_policy.evaluate(reg, "route", trusted_choice_input(policy, 0.99, choice="none"),
                                resolved_model="jev-1.13.0", proposed_action="inspect_route")["route"],
            "abstain",
        )

    def test_uncalibrated_policy_is_forced_to_shadow(self) -> None:
        result = jev_policy.evaluate(
            registry(choice_policy(calibration_status="uncalibrated")),
            "route",
            choice_answer(0.99),
            resolved_model="jev-1.13.0",
        )
        self.assertEqual(result["route"], "shadow")
        self.assertIn("uncalibrated_policy", result["reason_codes"])

    def test_high_risk_never_routes_auto(self) -> None:
        policy = choice_policy(risk="high")
        result = jev_policy.evaluate(
            registry(policy),
            "route",
            trusted_choice_input(policy, 0.99),
            resolved_model="jev-1.13.0",
            proposed_action="inspect_route",
        )
        self.assertEqual(result["route"], "review")
        self.assertIn("high_risk_requires_review", result["reason_codes"])

    def test_auto_requires_trusted_envelope_and_exact_authorized_action(self) -> None:
        policy = choice_policy()
        reg = registry(policy)
        bare = jev_policy.evaluate(
            reg, "route", choice_answer(0.99), resolved_model="jev-1.13.0",
            proposed_action="inspect_route",
        )
        self.assertEqual(bare["route"], "shadow")
        self.assertIn("untrusted_judgment_provenance", bare["reason_codes"])

        missing = jev_policy.evaluate(
            reg, "route", trusted_choice_input(policy, 0.99), resolved_model="jev-1.13.0"
        )
        self.assertEqual(missing["route"], "review")
        self.assertIn("action_not_autonomously_authorized", missing["reason_codes"])

        wrong = jev_policy.evaluate(
            reg, "route", trusted_choice_input(policy, 0.99),
            resolved_model="jev-1.13.0", proposed_action="different_action",
        )
        self.assertEqual(wrong["route"], "review")

        destructive = choice_policy()
        destructive["action_type"] = "destructive"
        destructive["autonomous_actions"] = []
        high_impact = jev_policy.evaluate(
            registry(destructive), "route", trusted_choice_input(destructive, 0.99),
            resolved_model="jev-1.13.0", proposed_action="inspect_route",
        )
        self.assertEqual(high_impact["route"], "review")
        self.assertIn("high_impact_requires_review", high_impact["reason_codes"])

    def test_auto_requires_live_official_service_identity(self) -> None:
        policy = choice_policy()
        reg = registry(policy)
        for name, overrides in (
            ("custom_endpoint", {"endpoint": "https://collector.example/v1/systemone"}),
            ("unauthenticated_cache", {"cached": True}),
        ):
            with self.subTest(name=name):
                judgment = envelope(policy, choice_answer(0.99), **overrides)
                result = jev_policy.evaluate(
                    reg,
                    "route",
                    judgment,
                    resolved_model="jev-1.13.0",
                    proposed_action="inspect_route",
                )
                self.assertEqual(result["route"], "shadow")
                self.assertFalse(result["judgment_provenance"]["trusted"])
                self.assertIn("untrusted_judgment_provenance", result["reason_codes"])

    def test_envelope_binds_contract_models_question_judgment_request_and_projection(self) -> None:
        policy = choice_policy()
        good = trusted_choice_input(policy, 0.99)
        result = jev_policy.evaluate(
            registry(policy), "route", good, resolved_model="jev-1.13.0",
            proposed_action="inspect_route",
        )
        self.assertEqual(result["route"], "auto")
        self.assertTrue(result["judgment_provenance"]["trusted"])

        mutations = {
            "contract": ("question_contract_hash", "f" * 64),
            "requested_model": ("requested_model", "jev-1.12.0"),
            "response_model": ("response_model", "jev-1.12.0"),
            "judgment_id": ("judgment_id", "bad"),
            "request_hash": ("request_hash", "bad"),
            "projection": ("state_projection_version", "other-projection-v1"),
        }
        for name, (field, value) in mutations.items():
            with self.subTest(name=name):
                broken = copy.deepcopy(good)
                broken["meta"][field] = value  # type: ignore[index]
                with self.assertRaises(jev_policy.PolicyError):
                    jev_policy.evaluate(
                        registry(policy), "route", broken, resolved_model="jev-1.13.0",
                        proposed_action="inspect_route",
                    )

        wrong_question = copy.deepcopy(good)
        wrong_question["meta"]["question_keys"] = ["other_question"]  # type: ignore[index]
        with self.assertRaises(jev_policy.PolicyError):
            jev_policy.evaluate(
                registry(policy), "route", wrong_question, resolved_model="jev-1.13.0",
                proposed_action="inspect_route",
            )

        wrong_body_model = copy.deepcopy(good)
        wrong_body_model["response"]["model"] = "jev-1.12.0"  # type: ignore[index]
        with self.assertRaisesRegex(jev_policy.PolicyError, "model"):
            jev_policy.evaluate(
                registry(policy), "route", wrong_body_model, resolved_model="jev-1.13.0",
                proposed_action="inspect_route",
            )

        separate_projection = copy.deepcopy(good)
        del separate_projection["meta"]["state_projection_version"]  # type: ignore[index]
        recovered = jev_policy.evaluate(
            registry(policy), "route", separate_projection, resolved_model="jev-1.13.0",
            proposed_action="inspect_route", state_projection_version="route-state-v1",
        )
        self.assertEqual(recovered["route"], "auto")

    def test_noul_routes_both_tails_and_calibrates_predicted_label(self) -> None:
        policy = noul_policy()
        reg = registry(policy)
        yes = jev_policy.evaluate(reg, "route", envelope(policy, {"type": "noul", "noul": 0.92}),
                                  resolved_model="jev-1.13.0", proposed_action="record_label")
        no = jev_policy.evaluate(reg, "route", envelope(policy, {"type": "noul", "noul": 0.08}),
                                 resolved_model="jev-1.13.0", proposed_action="record_label")
        middle = jev_policy.evaluate(reg, "route", envelope(policy, {"type": "noul", "noul": 0.5}),
                                     resolved_model="jev-1.13.0", proposed_action="record_label")
        self.assertEqual((yes["route"], no["route"], middle["route"]), ("auto", "auto", "abstain"))
        self.assertIs(yes["calibration_label"], True)
        self.assertEqual(yes["calibration_probability"], 0.92)
        self.assertIs(no["calibration_label"], False)
        self.assertEqual(no["calibration_probability"], 0.92)

    def test_choice_separates_routing_confidence_from_calibration_probability(self) -> None:
        policy = choice_policy()
        result = jev_policy.evaluate(
            registry(policy),
            "route",
            trusted_choice_input(policy, 0.91, selected_probability=0.72),
            resolved_model="jev-1.13.0",
            proposed_action="inspect_route",
        )
        self.assertEqual(result["routing_confidence"], 0.91)
        self.assertEqual(result["routing_signal"], 0.91)
        self.assertEqual(result["routing_signal_basis"], "confidence")
        self.assertEqual(result["calibration_probability"], 0.72)
        self.assertEqual(result["metric_basis"], "selected_choice_correctness")

        selected_signal = choice_policy()
        selected_signal["thresholds"]["routing_signal"] = "selected_probability"  # type: ignore[index]
        result = jev_policy.evaluate(
            registry(selected_signal),
            "route",
            trusted_choice_input(selected_signal, 0.99, selected_probability=0.72),
            resolved_model="jev-1.13.0",
            proposed_action="inspect_route",
        )
        self.assertEqual(result["routing_confidence"], 0.99)
        self.assertEqual(result["routing_signal"], 0.72)
        self.assertEqual(result["route"], "review")

    def test_fractional_score_needs_explicit_calibration_pair(self) -> None:
        answer = {
            "type": "score",
            "score": 1.4,
            "confidence": 0.92,
            "legend": {"0": "low", "1": "medium", "2": "high"},
            "probabilities": {"0": 0.1, "1": 0.6, "2": 0.3},
        }
        plain = jev_policy.evaluate(registry(score_policy()), "route", answer, resolved_model="jev-1.13.0")
        self.assertIsNone(plain["calibration_probability"])
        self.assertEqual(plain["calibration_status"], "missing_explicit_score_target")
        explicit = jev_policy.evaluate(
            registry(score_policy()),
            "route",
            answer,
            resolved_model="jev-1.13.0",
            calibration_label=1,
            calibration_probability=0.60,
        )
        self.assertEqual(explicit["calibration_label"], 1)
        self.assertEqual(explicit["calibration_probability"], 0.60)
        with self.assertRaisesRegex(jev_policy.PolicyError, "must equal"):
            jev_policy.evaluate(
                registry(score_policy()),
                "route",
                answer,
                resolved_model="jev-1.13.0",
                calibration_label=1,
                calibration_probability=0.75,
            )
        with self.assertRaises(jev_policy.PolicyError):
            jev_policy.evaluate(
                registry(score_policy()),
                "route",
                answer,
                resolved_model="jev-1.13.0",
                calibration_probability=0.75,
            )

    def test_integer_expected_score_is_not_inferred_as_a_level_label(self) -> None:
        answer = {
            "type": "score",
            "score": 1.0,
            "confidence": 0.95,
            "legend": {"0": "low", "1": "medium", "2": "high"},
            "probabilities": {"0": 0.5, "1": 0.0, "2": 0.5},
        }
        result = jev_policy.evaluate(
            registry(score_policy()), "route", answer, resolved_model="jev-1.13.0"
        )
        self.assertIsNone(result["calibration_label"])
        self.assertIsNone(result["calibration_probability"])
        self.assertEqual(result["metric_basis"], "declared_score_level_correctness")

    def test_expired_approval_and_unhealthy_report_force_shadow(self) -> None:
        policy = choice_policy()
        policy["governance"]["expires_at"] = "2026-02-01T00:00:00+00:00"  # type: ignore[index]
        result = jev_policy.evaluate(
            registry(policy),
            "route",
            choice_answer(0.99),
            resolved_model="jev-1.13.0",
            now=jev_policy._parse_timestamp("2026-03-01T00:00:00+00:00"),
        )
        self.assertEqual(result["route"], "shadow")
        self.assertIn("policy_approval_invalid", result["reason_codes"])

        policy = choice_policy()
        policy["monitoring"]["require_health_report"] = True  # type: ignore[index]
        result = jev_policy.evaluate(
            registry(policy), "route", choice_answer(0.99), resolved_model="jev-1.13.0"
        )
        self.assertEqual(result["route"], "shadow")
        self.assertIn("health_report_required", result["reason_codes"])

    def test_external_health_cannot_enable_auto_but_in_process_ledger_can(self) -> None:
        policy = choice_policy(window_size=1)
        policy["monitoring"]["require_health_report"] = True  # type: ignore[index]
        reg = registry(policy)
        external = {
            "schema_version": "jev-policy-report-v1",
            "registry_version": reg["registry_version"],
            "policy_id": "route",
            "policy_version": policy["policy_version"],
            "model": policy["model"],
            "generated_at": jev_policy._utc_now(),
            "drift_status": "stable",
        }
        untrusted = jev_policy.evaluate(
            reg, "route", trusted_choice_input(policy, 0.99),
            resolved_model="jev-1.13.0", proposed_action="inspect_route",
            health_report=external,
        )
        self.assertEqual(untrusted["route"], "shadow")
        self.assertIn("trusted_health_report_required", untrusted["reason_codes"])

        with tempfile.TemporaryDirectory() as tmp:
            events = Path(tmp) / "health.jsonl"
            decision = jev_policy.record_decision(
                reg,
                "route",
                trusted_choice_input(policy, 0.99, judgment_hex="1" * 32),
                events,
                resolved_model="jev-1.13.0",
                proposed_action="inspect_route",
            )
            jev_policy.record_outcome(events, decision["decision_id"], "a")
            trusted = jev_policy.evaluate(
                reg,
                "route",
                trusted_choice_input(policy, 0.99, judgment_hex="2" * 32),
                resolved_model="jev-1.13.0",
                proposed_action="inspect_route",
                health_events=events,
            )
            self.assertEqual(trusted["route"], "auto")
            self.assertTrue(trusted["policy_snapshot"]["health_trusted"])

    def test_rejects_model_mismatch_and_malformed_answer(self) -> None:
        with self.assertRaisesRegex(jev_policy.PolicyError, "model"):
            jev_policy.evaluate(registry(), "route", choice_answer(0.9), resolved_model="jev-other")
        malformed = choice_answer(0.9)
        malformed["probabilities"] = {"a": 0.8, "b": 0.1, "none": 0.0}
        with self.assertRaisesRegex(jev_policy.PolicyError, "sum"):
            jev_policy.evaluate(registry(), "route", malformed, resolved_model="jev-1.13.0")
        malformed = choice_answer(0.9)
        malformed["confidence"] = float("inf")
        with self.assertRaises(jev_policy.PolicyError):
            jev_policy.evaluate(registry(), "route", malformed, resolved_model="jev-1.13.0")


class LedgerAndReportTests(unittest.TestCase):
    def test_record_persists_provenance_action_and_judgment_is_one_shot(self) -> None:
        policy = choice_policy()
        reg = registry(policy)
        judgment = trusted_choice_input(policy, 0.99, judgment_hex="3" * 32)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "policy.jsonl"
            event = jev_policy.record_decision(
                reg, "route", judgment, path, resolved_model="jev-1.13.0",
                proposed_action="inspect_route",
            )
            self.assertEqual(event["route"], "auto")
            self.assertEqual(event["proposed_action"], "inspect_route")
            self.assertEqual(event["judgment_provenance"]["judgment_id"], "jdg_" + "3" * 32)
            with self.assertRaisesRegex(jev_policy.PolicyError, "duplicate decision for judgment"):
                jev_policy.record_decision(
                    reg, "route", judgment, path, resolved_model="jev-1.13.0",
                    proposed_action="inspect_route",
                )

            tampered = copy.deepcopy(event)
            tampered["proposed_action"] = "different_action"
            with self.assertRaisesRegex(jev_policy.PolicyError, "route"):
                jev_policy._validate_event(tampered)

    def test_decision_outcome_pairing_permissions_and_missing_outcome(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "policy.jsonl"
            path.write_text("", encoding="utf-8")
            path.chmod(0o644)
            first = jev_policy.record_decision(
                registry(), "route", choice_answer(0.8), path, resolved_model="jev-1.13.0"
            )
            jev_policy.record_outcome(path, first["decision_id"], "a")
            jev_policy.record_decision(
                registry(), "route", choice_answer(0.7), path, resolved_model="jev-1.13.0"
            )
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            report = jev_policy.build_report(registry(), "route", path, bins=2)
            self.assertEqual(report["counts"]["decisions"], 2)
            self.assertEqual(report["counts"]["labeled"], 1)
            self.assertEqual(report["counts"]["missing_outcome"], 1)
            self.assertEqual(report["counts"]["scored"], 1)
            with self.assertRaisesRegex(jev_policy.PolicyError, "already"):
                jev_policy.record_outcome(path, first["decision_id"], "a")
            with self.assertRaisesRegex(jev_policy.PolicyError, "unknown"):
                jev_policy.record_outcome(path, "00000000-0000-4000-8000-000000000000", "a")

    def test_brier_ece_and_policy_limit_alerts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "policy.jsonl"
            first = jev_policy.record_decision(
                registry(), "route", choice_answer(0.95, selected_probability=0.8), path,
                resolved_model="jev-1.13.0",
            )
            jev_policy.record_outcome(path, first["decision_id"], "a")
            second = jev_policy.record_decision(
                registry(), "route", choice_answer(0.95, selected_probability=0.6), path,
                resolved_model="jev-1.13.0",
            )
            jev_policy.record_outcome(path, second["decision_id"], "b")
            report = jev_policy.build_report(registry(), "route", path, bins=2)
            self.assertAlmostEqual(report["overall"]["brier"], 0.20)
            self.assertAlmostEqual(report["overall"]["ece"], 0.20)
            self.assertEqual(report["metric_basis"], "selected_choice_correctness")
            codes = {alert["code"] for alert in report["alerts"]}
            self.assertIn("brier_policy_limit_exceeded", codes)
            self.assertIn("ece_policy_limit_exceeded", codes)

    def test_baseline_window_drift_alerts(self) -> None:
        reg = registry(choice_policy(window_size=2))
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "policy.jsonl"
            for outcome in ("a", "a", "b", "b"):
                decision = jev_policy.record_decision(
                    reg,
                    "route",
                    choice_answer(0.95, selected_probability=0.9),
                    path,
                    resolved_model="jev-1.13.0",
                )
                jev_policy.record_outcome(path, decision["decision_id"], outcome)
            report = jev_policy.build_report(reg, "route", path, bins=2)
            self.assertAlmostEqual(report["baseline_window"]["brier"], 0.01)
            self.assertEqual(report["baseline_window"]["baseline_id"], "routes-baseline-v1")
            self.assertEqual(report["baseline_window"]["source"], "policy_registry_frozen_baseline")
            self.assertAlmostEqual(report["current_window"]["brier"], 0.81)
            codes = {alert["code"] for alert in report["alerts"]}
            self.assertIn("brier_baseline_drift", codes)
            self.assertIn("ece_baseline_drift", codes)
            gated = jev_policy.evaluate(
                reg,
                "route",
                choice_answer(0.99),
                resolved_model="jev-1.13.0",
                health_report=report,
            )
            self.assertEqual(gated["route"], "shadow")
            self.assertIn("policy_health_alert", gated["reason_codes"])

    def test_missing_score_calibration_probability_is_not_zero(self) -> None:
        answer = {
            "type": "score",
            "score": 1.25,
            "confidence": 0.92,
            "legend": {"0": "low", "1": "medium", "2": "high"},
            "probabilities": {"0": 0.1, "1": 0.7, "2": 0.2},
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "policy.jsonl"
            decision = jev_policy.record_decision(
                registry(score_policy()), "route", answer, path, resolved_model="jev-1.13.0"
            )
            jev_policy.record_outcome(path, decision["decision_id"], 1)
            report = jev_policy.build_report(registry(score_policy()), "route", path, bins=5)
            self.assertEqual(report["counts"]["labeled"], 1)
            self.assertEqual(report["counts"]["missing_calibration_probability"], 1)
            self.assertEqual(report["counts"]["scored"], 0)
            self.assertIsNone(report["overall"]["brier"])
            self.assertIsNone(report["overall"]["ece"])

    def test_rejects_bad_outcome_and_corrupt_ledger(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "policy.jsonl"
            decision = jev_policy.record_decision(
                registry(), "route", choice_answer(0.9), path, resolved_model="jev-1.13.0"
            )
            with self.assertRaises(jev_policy.PolicyError):
                jev_policy.record_outcome(path, decision["decision_id"], "not-an-option")
            path.write_text('{"event_type":"decision","routing_signal":NaN}\n', encoding="utf-8")
            with self.assertRaises(jev_policy.PolicyError):
                jev_policy.build_report(registry(), "route", path)

    def test_raw_state_cannot_enter_a_decision_event(self) -> None:
        answer = choice_answer(0.9)
        answer["state"] = {"private": "raw-sensitive-sentinel"}
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "policy.jsonl"
            with self.assertRaisesRegex(jev_policy.PolicyError, "unexpected"):
                jev_policy.record_decision(
                    registry(), "route", answer, path, resolved_model="jev-1.13.0"
                )
            self.assertFalse(path.exists())

    def test_semantically_inconsistent_decision_events_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "policy.jsonl"
            event = jev_policy.record_decision(
                registry(), "route", choice_answer(0.95), path, resolved_model="jev-1.13.0"
            )
            wrong_label = copy.deepcopy(event)
            wrong_label["calibration_label"] = "b"
            with self.assertRaisesRegex(jev_policy.PolicyError, "calibration_label"):
                jev_policy._validate_event(wrong_label)

            wrong_signal = copy.deepcopy(event)
            wrong_signal["routing_signal"] = 0.01
            with self.assertRaisesRegex(jev_policy.PolicyError, "routing_signal"):
                jev_policy._validate_event(wrong_signal)

            unsafe = copy.deepcopy(event)
            unsafe["risk"] = "high"
            unsafe["policy_snapshot"]["autonomous_actions"] = []
            unsafe["route"] = "auto"
            with self.assertRaisesRegex(jev_policy.PolicyError, "route"):
                jev_policy._validate_event(unsafe)

    def test_concurrent_outcome_writers_allow_exactly_one(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "policy.jsonl"
            decision = jev_policy.record_decision(
                registry(), "route", choice_answer(0.9), path, resolved_model="jev-1.13.0"
            )
            command = [
                sys.executable,
                str(Path(jev_policy.__file__).resolve()),
                "record-outcome",
                str(path),
                decision["decision_id"],
                "--outcome-json",
                json.dumps("a"),
            ]
            processes = [
                subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                for _ in range(6)
            ]
            results = [process.communicate(timeout=10) + (process.returncode,) for process in processes]
            self.assertEqual(sum(returncode == 0 for _, _, returncode in results), 1)
            events = jev_policy._read_events(path)
            self.assertEqual(sum(event["event_type"] == "outcome" for event in events), 1)


class CliTests(unittest.TestCase):
    def test_cli_exposes_all_commands_and_check_policy(self) -> None:
        commands = set(jev_policy.build_parser()._subparsers._group_actions[0].choices)
        self.assertEqual(
            commands,
            {"check-policy", "evaluate", "record-decision", "record-outcome", "report"},
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "registry.json"
            path.write_text(json.dumps(registry()), encoding="utf-8")
            output = io.StringIO()
            with redirect_stdout(output), redirect_stderr(io.StringIO()):
                code = jev_policy.main(["check-policy", str(path)])
            self.assertEqual(code, 0)
            self.assertTrue(json.loads(output.getvalue())["ok"])

            answer_path = Path(tmp) / "answer.json"
            answer_path.write_text(json.dumps(choice_answer(0.9)), encoding="utf-8")
            output = io.StringIO()
            with redirect_stdout(output), redirect_stderr(io.StringIO()):
                code = jev_policy.main([
                    "evaluate", str(path), "route", str(answer_path), "--model", "jev-1.13.0"
                ])
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(output.getvalue())["route"], "shadow")

            envelope_path = Path(tmp) / "envelope.json"
            envelope_path.write_text(
                json.dumps(trusted_choice_input(choice_policy(), 0.9)), encoding="utf-8"
            )
            output = io.StringIO()
            with redirect_stdout(output), redirect_stderr(io.StringIO()):
                code = jev_policy.main([
                    "evaluate", str(path), "route", str(envelope_path),
                    "--model", "jev-1.13.0", "--proposed-action", "inspect_route",
                ])
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(output.getvalue())["route"], "auto")

            events = Path(tmp) / "events.jsonl"
            output = io.StringIO()
            with redirect_stdout(output), redirect_stderr(io.StringIO()):
                code = jev_policy.main([
                    "record-decision", str(path), "route", str(answer_path),
                    "--model", "jev-1.13.0", "--events", str(events),
                ])
            self.assertEqual(code, 0)
            decision = json.loads(output.getvalue())

            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                code = jev_policy.main([
                    "record-outcome", str(events), decision["decision_id"],
                    "--outcome-json", json.dumps("a"),
                ])
            self.assertEqual(code, 0)

            output = io.StringIO()
            with redirect_stdout(output), redirect_stderr(io.StringIO()):
                code = jev_policy.main([
                    "report", str(path), "route", str(events), "--bins", "2"
                ])
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(output.getvalue())["counts"]["labeled"], 1)

    def test_json_loader_rejects_nan(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bad.json"
            path.write_text('{"x": NaN}', encoding="utf-8")
            with self.assertRaises(jev_policy.PolicyError):
                jev_policy.load_json(str(path))
            path.write_text('{"x": 1, "x": 2}', encoding="utf-8")
            with self.assertRaisesRegex(jev_policy.PolicyError, "duplicate"):
                jev_policy.load_json(str(path))


if __name__ == "__main__":
    unittest.main()

#!/usr/bin/env python3
"""Apply versioned JEV routing policies and measure their calibration.

This helper accepts typed JEV answers for shadow evaluation or a trusted
``jev_judge run`` envelope for an automatic route, plus compact policy metadata.
It never accepts or records raw request state. Policy decisions and later
observed outcomes are paired by ``decision_id`` in an append-only JSONL ledger.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import math
import os
import re
import stat
import sys
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import jev_judge


REGISTRY_SCHEMA = "jev-policy-registry-v2"
EVENT_SCHEMA = "jev-policy-event-v2"
QUESTION_TYPES = {"choice", "noul", "score"}
ROUTES = {"shadow", "auto", "review", "abstain"}
RISKS = {"low", "medium", "high"}
MODES = {"active", "shadow"}
ACTION_TYPES = {
    "classify_only",
    "read_only",
    "reversible_write",
    "external_message",
    "destructive",
    "financial",
    "permission_change",
}
HIGH_IMPACT_ACTION_TYPES = {
    "external_message",
    "destructive",
    "financial",
    "permission_change",
}
HEALTH_STATUSES = {"not_provided", "stable", "alert", "insufficient_data", "stale"}
SAFE_ID = re.compile(r"^[A-Za-z][A-Za-z0-9_.:-]{0,127}$")
SHA256 = re.compile(r"^(?:sha256:)?[0-9a-f]{64}$")
JUDGMENT_ID = re.compile(r"^jdg_[0-9a-f]{32}$")


class PolicyError(RuntimeError):
    """A safe, user-displayable policy or ledger validation error."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _exact_object(value: Any, required: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise PolicyError(f"{label} must be a JSON object")
    missing = required - set(value)
    extra = set(value) - required
    if missing:
        raise PolicyError(f"{label} is missing required fields: {', '.join(sorted(map(str, missing)))}")
    if extra:
        raise PolicyError(f"{label} has unexpected fields: {', '.join(sorted(map(str, extra)))}")
    return value


def _identifier(value: Any, label: str) -> str:
    if not isinstance(value, str) or not SAFE_ID.fullmatch(value):
        raise PolicyError(f"{label} must be a stable identifier")
    return value


def _nonempty_string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PolicyError(f"{label} must be a non-empty string")
    return value


def _positive_int(value: Any, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise PolicyError(f"{label} must be a positive integer")
    return value


def _finite_number(value: Any, label: str) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise PolicyError(f"{label} must be a finite number")
    try:
        number = float(value)
    except (OverflowError, ValueError):
        raise PolicyError(f"{label} must be a finite number") from None
    if not math.isfinite(number):
        raise PolicyError(f"{label} must be a finite number")
    return number


def _probability(value: Any, label: str) -> float:
    number = _finite_number(value, label)
    if not 0.0 <= number <= 1.0:
        raise PolicyError(f"{label} must be in [0, 1]")
    return number


def _enum(value: Any, allowed: set[str], label: str) -> str:
    if not isinstance(value, str) or value not in allowed:
        raise PolicyError(f"{label} must be one of: {', '.join(sorted(allowed))}")
    return value


def _pinned_model(value: Any, label: str) -> str:
    model = _nonempty_string(value, label)
    lowered = model.lower()
    if (
        any(alias in lowered for alias in ("latest", "stable", "default"))
        or not any(character.isdigit() for character in model)
        or any(character.isspace() for character in model)
    ):
        raise PolicyError(f"{label} must be a concrete, versioned model identifier")
    return model


def _validate_distribution(value: Any, keys: list[str], label: str) -> dict[str, float]:
    if not isinstance(value, dict) or set(value) != set(keys):
        raise PolicyError(f"{label} must contain exactly: {', '.join(keys)}")
    clean = {key: _probability(value[key], f"{label}.{key}") for key in keys}
    if not math.isclose(sum(clean.values()), 1.0, rel_tol=0.0, abs_tol=1e-6):
        raise PolicyError(f"{label} probabilities must sum to 1")
    return clean


def _validate_calibration(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise PolicyError(f"{label} must be a JSON object")
    status = value.get("status")
    if status == "uncalibrated":
        _exact_object(value, {"status"}, label)
        return value
    if status == "calibrated":
        _exact_object(
            value,
            {"status", "dataset_version", "sample_size", "report_sha256", "evaluated_at"},
            label,
        )
        _identifier(value["dataset_version"], f"{label}.dataset_version")
        _positive_int(value["sample_size"], f"{label}.sample_size")
        if not isinstance(value["report_sha256"], str) or not SHA256.fullmatch(value["report_sha256"]):
            raise PolicyError(f"{label}.report_sha256 must be a SHA-256 digest")
        _timestamp(value["evaluated_at"], f"{label}.evaluated_at")
        return value
    raise PolicyError(f"{label}.status must be 'calibrated' or 'uncalibrated'")


def _validate_monitoring(value: Any, label: str) -> dict[str, Any]:
    required = {
        "minimum_labeled",
        "window_size",
        "max_brier",
        "max_ece",
        "max_brier_delta",
        "max_ece_delta",
        "require_health_report",
        "max_health_report_age_seconds",
        "baseline",
    }
    monitoring = _exact_object(value, required, label)
    minimum = _positive_int(monitoring["minimum_labeled"], f"{label}.minimum_labeled")
    window = _positive_int(monitoring["window_size"], f"{label}.window_size")
    if window < minimum:
        raise PolicyError(f"{label}.window_size must be at least minimum_labeled")
    for field in {
        "max_brier", "max_ece", "max_brier_delta", "max_ece_delta"
    }:
        _probability(monitoring[field], f"{label}.{field}")
    if not isinstance(monitoring["require_health_report"], bool):
        raise PolicyError(f"{label}.require_health_report must be a boolean")
    age = _finite_number(
        monitoring["max_health_report_age_seconds"],
        f"{label}.max_health_report_age_seconds",
    )
    if age <= 0:
        raise PolicyError(f"{label}.max_health_report_age_seconds must be positive")
    baseline = _exact_object(
        monitoring["baseline"],
        {"baseline_id", "window_size", "labeled", "brier", "ece", "report_sha256"},
        f"{label}.baseline",
    )
    _identifier(baseline["baseline_id"], f"{label}.baseline.baseline_id")
    baseline_window = _positive_int(baseline["window_size"], f"{label}.baseline.window_size")
    baseline_labeled = _positive_int(baseline["labeled"], f"{label}.baseline.labeled")
    if baseline_window < minimum or baseline_labeled < minimum:
        raise PolicyError(f"{label}.baseline is below minimum_labeled")
    _probability(baseline["brier"], f"{label}.baseline.brier")
    _probability(baseline["ece"], f"{label}.baseline.ece")
    if not isinstance(baseline["report_sha256"], str) or not SHA256.fullmatch(baseline["report_sha256"]):
        raise PolicyError(f"{label}.baseline.report_sha256 must be a SHA-256 digest")
    return monitoring


def _validate_governance(value: Any, label: str) -> dict[str, Any]:
    governance = _exact_object(
        value,
        {"owner", "approval_status", "approved_by", "approved_at", "expires_at"},
        label,
    )
    _identifier(governance["owner"], f"{label}.owner")
    status_value = _enum(
        governance["approval_status"], {"approved", "draft"}, f"{label}.approval_status"
    )
    if status_value == "approved":
        _identifier(governance["approved_by"], f"{label}.approved_by")
        approved_at = _timestamp(governance["approved_at"], f"{label}.approved_at")
        expires_at = _timestamp(governance["expires_at"], f"{label}.expires_at")
        if _parse_timestamp(approved_at) >= _parse_timestamp(expires_at):
            raise PolicyError(f"{label}.expires_at must be after approved_at")
    elif any(governance[field] is not None for field in ("approved_by", "approved_at", "expires_at")):
        raise PolicyError(f"{label} draft approval fields must be null")
    return governance


def _validate_thresholds(value: Any, question_type: str, label: str) -> dict[str, Any]:
    if question_type in {"choice", "score"}:
        thresholds = _exact_object(value, {"routing_signal", "review_min", "auto_min"}, label)
        signal_options = {"confidence", "selected_probability"}
        if question_type == "score":
            # A Score value is commonly an expectation, even when numerically
            # integral; it is never evidence that one discrete level was chosen.
            signal_options = {"confidence"}
        _enum(thresholds["routing_signal"], signal_options, f"{label}.routing_signal")
        review_min = _probability(thresholds["review_min"], f"{label}.review_min")
        auto_min = _probability(thresholds["auto_min"], f"{label}.auto_min")
        if review_min > auto_min:
            raise PolicyError(f"{label}.review_min must not exceed auto_min")
        return thresholds

    thresholds = _exact_object(
        value,
        {"auto_no_max", "review_no_max", "review_yes_min", "auto_yes_min"},
        label,
    )
    auto_no = _probability(thresholds["auto_no_max"], f"{label}.auto_no_max")
    review_no = _probability(thresholds["review_no_max"], f"{label}.review_no_max")
    review_yes = _probability(thresholds["review_yes_min"], f"{label}.review_yes_min")
    auto_yes = _probability(thresholds["auto_yes_min"], f"{label}.auto_yes_min")
    if not auto_no <= review_no < review_yes <= auto_yes:
        raise PolicyError(
            f"{label} must satisfy auto_no_max <= review_no_max < "
            "review_yes_min <= auto_yes_min"
        )
    return thresholds


def _validate_policy(value: Any, policy_id: str) -> dict[str, Any]:
    common = {
        "policy_version",
        "model",
        "question_type",
        "question_id",
        "question_contract_sha256",
        "state_projection_version",
        "risk",
        "action_type",
        "autonomous_actions",
        "governance",
        "mode",
        "calibration",
        "thresholds",
        "monitoring",
    }
    if not isinstance(value, dict):
        raise PolicyError(f"policy {policy_id!r} must be a JSON object")
    question_type = _enum(value.get("question_type"), QUESTION_TYPES, f"policy {policy_id}.question_type")
    type_fields: set[str]
    if question_type == "choice":
        type_fields = {"choices", "abstain_values"}
    elif question_type == "score":
        type_fields = {"levels"}
    else:
        type_fields = set()
    policy = _exact_object(value, common | type_fields, f"policy {policy_id}")
    _identifier(policy["policy_version"], f"policy {policy_id}.policy_version")
    _pinned_model(policy["model"], f"policy {policy_id}.model")
    _identifier(policy["question_id"], f"policy {policy_id}.question_id")
    if (
        not isinstance(policy["question_contract_sha256"], str)
        or not SHA256.fullmatch(policy["question_contract_sha256"])
    ):
        raise PolicyError(f"policy {policy_id}.question_contract_sha256 must be a SHA-256 digest")
    _identifier(
        policy["state_projection_version"],
        f"policy {policy_id}.state_projection_version",
    )
    risk = _enum(policy["risk"], RISKS, f"policy {policy_id}.risk")
    action_type = _enum(
        policy["action_type"], ACTION_TYPES, f"policy {policy_id}.action_type"
    )
    autonomous_actions = policy["autonomous_actions"]
    if not isinstance(autonomous_actions, list):
        raise PolicyError(f"policy {policy_id}.autonomous_actions must be an array")
    clean_actions = [
        _identifier(item, f"policy {policy_id}.autonomous_actions")
        for item in autonomous_actions
    ]
    if len(set(clean_actions)) != len(clean_actions):
        raise PolicyError(f"policy {policy_id}.autonomous_actions must be unique")
    if (risk == "high" or action_type in HIGH_IMPACT_ACTION_TYPES) and clean_actions:
        raise PolicyError(
            f"policy {policy_id} must not grant autonomous actions to high-risk work"
        )
    _validate_governance(policy["governance"], f"policy {policy_id}.governance")
    _enum(policy["mode"], MODES, f"policy {policy_id}.mode")
    calibration = _validate_calibration(policy["calibration"], f"policy {policy_id}.calibration")
    monitoring = _validate_monitoring(policy["monitoring"], f"policy {policy_id}.monitoring")
    _validate_thresholds(policy["thresholds"], question_type, f"policy {policy_id}.thresholds")
    if calibration["status"] == "calibrated" and calibration["sample_size"] < monitoring["minimum_labeled"]:
        raise PolicyError(f"policy {policy_id}.calibration.sample_size is below minimum_labeled")

    if question_type == "choice":
        choices = policy["choices"]
        abstentions = policy["abstain_values"]
        if not isinstance(choices, list) or not 2 <= len(choices) <= 255:
            raise PolicyError(f"policy {policy_id}.choices must contain 2..255 identifiers")
        clean_choices = [_identifier(item, f"policy {policy_id}.choices") for item in choices]
        if len(set(clean_choices)) != len(clean_choices):
            raise PolicyError(f"policy {policy_id}.choices must be unique")
        if not isinstance(abstentions, list):
            raise PolicyError(f"policy {policy_id}.abstain_values must be an array")
        clean_abstentions = [_identifier(item, f"policy {policy_id}.abstain_values") for item in abstentions]
        if len(set(clean_abstentions)) != len(clean_abstentions):
            raise PolicyError(f"policy {policy_id}.abstain_values must be unique")
        if not set(clean_abstentions) <= set(clean_choices):
            raise PolicyError(f"policy {policy_id}.abstain_values must be choices")
    elif question_type == "score":
        levels = policy["levels"]
        if not isinstance(levels, list) or not 2 <= len(levels) <= 255:
            raise PolicyError(f"policy {policy_id}.levels must contain 2..255 descriptions")
        if any(not isinstance(item, str) or not item.strip() for item in levels):
            raise PolicyError(f"policy {policy_id}.levels must be non-empty strings")
        if len(set(levels)) != len(levels):
            raise PolicyError(f"policy {policy_id}.levels must be unique")
    return policy


def validate_registry(value: Any) -> dict[str, Any]:
    """Validate a registry without adding any policy or threshold defaults."""
    registry = _exact_object(value, {"schema_version", "registry_version", "policies"}, "registry")
    if registry["schema_version"] != REGISTRY_SCHEMA:
        raise PolicyError(f"registry.schema_version must be {REGISTRY_SCHEMA!r}")
    _identifier(registry["registry_version"], "registry.registry_version")
    policies = registry["policies"]
    if not isinstance(policies, dict) or not policies:
        raise PolicyError("registry.policies must be a non-empty object")
    for policy_id, policy in policies.items():
        _identifier(policy_id, "policy id")
        _validate_policy(policy, policy_id)
    return registry


def _get_policy(registry: dict[str, Any], policy_id: str) -> dict[str, Any]:
    validate_registry(registry)
    _identifier(policy_id, "policy id")
    if policy_id not in registry["policies"]:
        raise PolicyError(f"unknown policy {policy_id!r}")
    return registry["policies"][policy_id]


def _validate_answer(policy: dict[str, Any], answer: Any) -> dict[str, Any]:
    question_type = policy["question_type"]
    if question_type == "noul":
        clean = _exact_object(answer, {"type", "noul"}, "answer")
        if clean["type"] != "noul":
            raise PolicyError("answer.type does not match policy.question_type")
        _probability(clean["noul"], "answer.noul")
        return clean

    if question_type == "choice":
        clean = _exact_object(answer, {"type", "choice", "confidence", "probabilities"}, "answer")
        if clean["type"] != "choice":
            raise PolicyError("answer.type does not match policy.question_type")
        if clean["choice"] not in policy["choices"]:
            raise PolicyError("answer.choice is not in the policy choices")
        _probability(clean["confidence"], "answer.confidence")
        _validate_distribution(clean["probabilities"], policy["choices"], "answer.probabilities")
        return clean

    clean = _exact_object(
        answer,
        {"type", "score", "confidence", "legend", "probabilities"},
        "answer",
    )
    if clean["type"] != "score":
        raise PolicyError("answer.type does not match policy.question_type")
    score = _finite_number(clean["score"], "answer.score")
    if not 0.0 <= score <= len(policy["levels"]) - 1:
        raise PolicyError("answer.score is outside the policy level range")
    _probability(clean["confidence"], "answer.confidence")
    keys = [str(index) for index in range(len(policy["levels"]))]
    legend = clean["legend"]
    if not isinstance(legend, dict) or set(legend) != set(keys):
        raise PolicyError("answer.legend does not exactly match the policy levels")
    if any(legend[key] != policy["levels"][int(key)] for key in keys):
        raise PolicyError("answer.legend descriptions do not match the policy levels")
    _validate_distribution(clean["probabilities"], keys, "answer.probabilities")
    return clean


def _digest_hex(value: Any, label: str) -> str:
    if not isinstance(value, str) or not SHA256.fullmatch(value):
        raise PolicyError(f"{label} must be a SHA-256 digest")
    return value.removeprefix("sha256:")


def _validate_judgment_input(
    policy: dict[str, Any],
    value: Any,
    *,
    resolved_model: str,
    state_projection_version: str | None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Extract one answer and prove its production provenance when available.

    A bare typed answer remains useful for previews and migration, but it is
    deliberately untrusted and therefore cannot authorize an automatic route.
    """
    model = _pinned_model(resolved_model, "resolved_model")
    if model != policy["model"]:
        raise PolicyError("resolved model does not match the policy's pinned model")

    if not (
        isinstance(value, dict)
        and isinstance(value.get("response"), dict)
        and isinstance(value.get("meta"), dict)
    ):
        answer = _validate_answer(policy, value)
        return answer, {
            "source": "bare_typed_answer",
            "trusted": False,
            "judgment_id": None,
            "request_hash": None,
            "question_contract_sha256": policy["question_contract_sha256"],
            "question_id": policy["question_id"],
            "requested_model": model,
            "resolved_model": model,
            "endpoint": None,
            "judged_at": None,
            "cached": None,
            "state_projection_version": state_projection_version,
        }

    response = value["response"]
    meta = value["meta"]
    required_meta = {
        "schema_version", "judgment_id", "ts", "kind", "request_hash",
        "question_contract_hash", "requested_model", "response_model", "endpoint",
        "question_keys", "status", "cached",
    }
    missing = required_meta - set(meta)
    if missing:
        raise PolicyError(
            "judgment envelope meta is missing required fields: "
            + ", ".join(sorted(missing))
        )
    if meta["schema_version"] != "1" or meta["kind"] != "jev_judgment" or meta["status"] != "ok":
        raise PolicyError("judgment envelope is not a successful supported JEV judgment")
    judgment_id = meta["judgment_id"]
    if not isinstance(judgment_id, str) or not JUDGMENT_ID.fullmatch(judgment_id):
        raise PolicyError("judgment envelope has an invalid judgment_id")
    _timestamp(meta["ts"], "judgment envelope meta.ts")
    request_hash = _digest_hex(meta["request_hash"], "judgment envelope meta.request_hash")
    contract_hash = _digest_hex(
        meta["question_contract_hash"],
        "judgment envelope meta.question_contract_hash",
    )
    expected_contract = _digest_hex(
        policy["question_contract_sha256"],
        "policy.question_contract_sha256",
    )
    if contract_hash != expected_contract:
        raise PolicyError("judgment question contract does not match the policy")
    requested_model = _pinned_model(meta["requested_model"], "judgment requested_model")
    response_model = _pinned_model(meta["response_model"], "judgment response_model")
    body_model = _pinned_model(response.get("model"), "judgment response.model")
    if {requested_model, response_model, body_model, model} != {policy["model"]}:
        raise PolicyError("judgment requested/resolved model does not match the policy")
    question_keys = meta["question_keys"]
    answers = response.get("answers")
    if (
        not isinstance(question_keys, list)
        or any(not isinstance(item, str) for item in question_keys)
        or len(question_keys) != len(set(question_keys))
        or not isinstance(answers, dict)
        or set(question_keys) != set(answers)
    ):
        raise PolicyError("judgment question_keys do not exactly match response answers")
    question_id = policy["question_id"]
    if question_id not in answers:
        raise PolicyError("judgment does not contain the policy question_id")
    raw_endpoint = _nonempty_string(meta["endpoint"], "judgment envelope meta.endpoint")
    try:
        endpoint = jev_judge.validate_endpoint(raw_endpoint)
    except (jev_judge.JevError, TypeError, ValueError) as exc:
        raise PolicyError(f"judgment envelope endpoint is invalid: {exc}") from None
    if endpoint != raw_endpoint:
        raise PolicyError("judgment envelope endpoint must be canonical")
    if not isinstance(meta["cached"], bool):
        raise PolicyError("judgment envelope meta.cached must be a boolean")
    trusted_service = endpoint == jev_judge.DEFAULT_ENDPOINT and not meta["cached"]

    projection = meta.get("state_projection_version", state_projection_version)
    if state_projection_version is not None and meta.get("state_projection_version") not in {
        None, state_projection_version
    }:
        raise PolicyError("judgment state projection declarations disagree")
    if projection != policy["state_projection_version"]:
        raise PolicyError("judgment state projection does not match the policy")

    answer = _validate_answer(policy, answers[question_id])
    return answer, {
        "source": "jev_run_envelope",
        "trusted": trusted_service,
        "judgment_id": judgment_id,
        "request_hash": request_hash,
        "question_contract_sha256": contract_hash,
        "question_id": question_id,
        "requested_model": requested_model,
        "resolved_model": response_model,
        "endpoint": endpoint,
        "judged_at": meta["ts"],
        "cached": meta["cached"],
        "state_projection_version": projection,
    }


def _explicit_score_calibration(
    policy: dict[str, Any],
    answer: dict[str, Any],
    label: Any,
    probability: Any,
) -> tuple[int, float]:
    if not isinstance(label, int) or isinstance(label, bool) or not 0 <= label < len(policy["levels"]):
        raise PolicyError("calibration_label must be an integer score level")
    supplied = _probability(probability, "calibration_probability")
    level_probability = _probability(
        answer["probabilities"][str(label)],
        "answer.probabilities.calibration_level",
    )
    if not math.isclose(supplied, level_probability, rel_tol=0.0, abs_tol=1e-9):
        raise PolicyError(
            "calibration_probability must equal the JEV probability for calibration_label"
        )
    return label, supplied


def _approval_is_valid(policy: dict[str, Any], now: datetime) -> bool:
    governance = policy["governance"]
    if governance["approval_status"] != "approved":
        return False
    return _parse_timestamp(governance["approved_at"]) <= now < _parse_timestamp(
        governance["expires_at"]
    )


def _health_status(
    registry: dict[str, Any],
    policy_id: str,
    policy: dict[str, Any],
    report: dict[str, Any] | None,
    now: datetime,
) -> str:
    if report is None:
        return "not_provided"
    if not isinstance(report, dict):
        raise PolicyError("health_report must be a policy report object")
    expected = {
        "schema_version": "jev-policy-report-v1",
        "registry_version": registry["registry_version"],
        "policy_id": policy_id,
        "policy_version": policy["policy_version"],
        "model": policy["model"],
    }
    for key, value in expected.items():
        if report.get(key) != value:
            raise PolicyError(f"health_report.{key} does not match the evaluated policy")
    generated_at = _timestamp(report.get("generated_at"), "health_report.generated_at")
    age = (now - _parse_timestamp(generated_at)).total_seconds()
    if age < -300:
        raise PolicyError("health_report.generated_at is implausibly in the future")
    if age > policy["monitoring"]["max_health_report_age_seconds"]:
        return "stale"
    status_value = report.get("drift_status")
    if status_value not in {"stable", "alert", "insufficient_data"}:
        raise PolicyError("health_report.drift_status is invalid")
    return status_value


def _route_from_facts(
    policy: dict[str, Any],
    prediction: Any,
    routing_signal: float,
    *,
    approval_valid: bool,
    health_status: str,
    health_trusted: bool,
    judgment_trusted: bool,
    proposed_action: str | None,
) -> tuple[str, list[str]]:
    reasons: list[str] = []
    if policy["mode"] == "shadow":
        reasons.append("policy_shadow_mode")
    if policy["calibration"]["status"] != "calibrated":
        reasons.append("uncalibrated_policy")
    if not approval_valid:
        reasons.append("policy_approval_invalid")
    if health_status != "not_provided" and health_status != "stable":
        reasons.append(f"policy_health_{health_status}")
    if policy["monitoring"]["require_health_report"] and health_status == "not_provided":
        reasons.append("health_report_required")
    if policy["monitoring"]["require_health_report"] and not health_trusted:
        reasons.append("trusted_health_report_required")
    if not judgment_trusted:
        reasons.append("untrusted_judgment_provenance")
    if reasons:
        return "shadow", reasons
    if policy["question_type"] == "choice" and prediction in policy["abstain_values"]:
        return "abstain", ["explicit_abstain_value"]

    thresholds = policy["thresholds"]
    if policy["question_type"] == "noul":
        auto_eligible = (
            routing_signal <= thresholds["auto_no_max"]
            or routing_signal >= thresholds["auto_yes_min"]
        )
        review_eligible = (
            routing_signal <= thresholds["review_no_max"]
            or routing_signal >= thresholds["review_yes_min"]
        )
    else:
        auto_eligible = routing_signal >= thresholds["auto_min"]
        review_eligible = routing_signal >= thresholds["review_min"]
    if auto_eligible:
        if policy["risk"] == "high":
            return "review", ["review_threshold_met", "high_risk_requires_review"]
        if policy["action_type"] in HIGH_IMPACT_ACTION_TYPES:
            return "review", ["review_threshold_met", "high_impact_requires_review"]
        if proposed_action is None or proposed_action not in policy["autonomous_actions"]:
            return "review", ["review_threshold_met", "action_not_autonomously_authorized"]
        return "auto", ["auto_threshold_met", "autonomous_action_authorized"]
    if review_eligible:
        return "review", ["review_threshold_met"]
    return "abstain", ["uncertainty_band"]


def evaluate(
    registry: dict[str, Any],
    policy_id: str,
    answer: dict[str, Any],
    *,
    resolved_model: str,
    proposed_action: str | None = None,
    state_projection_version: str | None = None,
    calibration_label: Any = None,
    calibration_probability: Any = None,
    health_report: dict[str, Any] | None = None,
    health_events: Path | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Validate one bare answer or trusted judgment envelope and route it."""
    policy = _get_policy(registry, policy_id)
    if health_report is not None and health_events is not None:
        raise PolicyError("health_report and health_events are mutually exclusive")
    clean_answer, provenance = _validate_judgment_input(
        policy,
        answer,
        resolved_model=resolved_model,
        state_projection_version=state_projection_version,
    )
    model = provenance["resolved_model"]
    if proposed_action is not None:
        proposed_action = _identifier(proposed_action, "proposed_action")
    supplied_label = calibration_label is not None
    supplied_probability = calibration_probability is not None
    if supplied_label != supplied_probability:
        raise PolicyError("calibration_label and calibration_probability must be supplied together")

    question_type = policy["question_type"]
    thresholds = policy["thresholds"]
    prediction: Any
    metric_label: Any
    metric_probability: float | None
    metric_probability_basis: str | None
    calibration_status = "ready"
    evaluation_time = now or datetime.now(timezone.utc)
    if evaluation_time.tzinfo is None:
        raise PolicyError("now must include a timezone")

    if question_type == "choice":
        if supplied_label:
            raise PolicyError("choice calibration is derived from its selected-option probability")
        prediction = clean_answer["choice"]
        selected_probability = _probability(
            clean_answer["probabilities"][prediction],
            "answer.probabilities.selected",
        )
        if thresholds["routing_signal"] == "confidence":
            routing_signal = _probability(clean_answer["confidence"], "answer.confidence")
            routing_basis = "confidence"
        else:
            routing_signal = selected_probability
            routing_basis = "selected_probability"
        routing_confidence = _probability(clean_answer["confidence"], "answer.confidence")
        metric_label = prediction
        metric_probability = selected_probability
        metric_probability_basis = "selected_option_probability"
    elif question_type == "noul":
        if supplied_label:
            raise PolicyError("noul calibration is derived from its predicted boolean label")
        probability_yes = _probability(clean_answer["noul"], "answer.noul")
        prediction = probability_yes >= 0.5
        routing_signal = probability_yes
        routing_basis = "noul_probability_yes"
        routing_confidence = None
        metric_label = prediction
        metric_probability = probability_yes if prediction else 1.0 - probability_yes
        metric_probability_basis = "predicted_boolean_probability"
    else:
        score = _finite_number(clean_answer["score"], "answer.score")
        prediction = score
        routing_signal = _probability(clean_answer["confidence"], "answer.confidence")
        routing_basis = "confidence"
        routing_confidence = _probability(clean_answer["confidence"], "answer.confidence")
        if supplied_label:
            metric_label, metric_probability = _explicit_score_calibration(
                policy, clean_answer, calibration_label, calibration_probability
            )
            metric_probability_basis = "caller_supplied_score_level_probability"
        else:
            metric_label = None
            metric_probability = None
            metric_probability_basis = None
            calibration_status = "missing_explicit_score_target"

    approval_valid = _approval_is_valid(policy, evaluation_time)
    health_trusted = False
    effective_health_report = health_report
    if health_events is not None:
        effective_health_report = build_report(registry, policy_id, health_events)
        health_trusted = True
    health_status = _health_status(
        registry, policy_id, policy, effective_health_report, evaluation_time
    )
    route, reasons = _route_from_facts(
        policy,
        prediction,
        routing_signal,
        approval_valid=approval_valid,
        health_status=health_status,
        health_trusted=health_trusted,
        judgment_trusted=provenance["trusted"],
        proposed_action=proposed_action,
    )
    metric_basis = {
        "choice": "selected_choice_correctness",
        "noul": "predicted_boolean_correctness",
        "score": "declared_score_level_correctness",
    }[question_type]
    calibration_report = policy["calibration"].get("report_sha256")
    policy_snapshot = {
        "question_id": policy["question_id"],
        "question_contract_sha256": policy["question_contract_sha256"],
        "state_projection_version": policy["state_projection_version"],
        "action_type": policy["action_type"],
        "autonomous_actions": list(policy["autonomous_actions"]),
        "governance": dict(policy["governance"]),
        "policy_mode": policy["mode"],
        "calibration_status": policy["calibration"]["status"],
        "calibration_report_sha256": calibration_report,
        "approval_valid": approval_valid,
        "health_report_required": policy["monitoring"]["require_health_report"],
        "health_status": health_status,
        "health_trusted": health_trusted,
        "thresholds": dict(policy["thresholds"]),
        "abstain_values": list(policy.get("abstain_values", [])),
    }

    return {
        "registry_version": registry["registry_version"],
        "policy_id": policy_id,
        "policy_version": policy["policy_version"],
        "model": model,
        "question_type": question_type,
        "risk": policy["risk"],
        "route": route,
        "proposed_action": proposed_action,
        "prediction": prediction,
        "routing_confidence": routing_confidence,
        "routing_signal": routing_signal,
        "routing_signal_basis": routing_basis,
        "calibration_label": metric_label,
        "calibration_probability": metric_probability,
        "calibration_probability_basis": metric_probability_basis,
        "calibration_status": calibration_status,
        "metric_basis": metric_basis,
        "reason_codes": reasons,
        "policy_snapshot": policy_snapshot,
        "typed_answer": clean_answer,
        "judgment_provenance": provenance,
        "evaluated_at": evaluation_time.isoformat(),
    }


def _event_id(value: Any, label: str) -> str:
    if not isinstance(value, str):
        raise PolicyError(f"{label} must be a UUID")
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError):
        raise PolicyError(f"{label} must be a UUID") from None
    if str(parsed) != value.lower():
        raise PolicyError(f"{label} must use canonical UUID form")
    return value


def _parse_timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _timestamp(value: Any, label: str) -> str:
    if not isinstance(value, str):
        raise PolicyError(f"{label} must be an ISO-8601 timestamp")
    try:
        parsed = _parse_timestamp(value)
    except ValueError:
        raise PolicyError(f"{label} must be an ISO-8601 timestamp") from None
    if parsed.tzinfo is None:
        raise PolicyError(f"{label} must include a timezone")
    return value


def _validate_outcome_contract(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or "kind" not in value:
        raise PolicyError(f"{label} must be an outcome contract")
    kind = value["kind"]
    if kind == "boolean":
        return _exact_object(value, {"kind"}, label)
    if kind == "choice":
        contract = _exact_object(value, {"kind", "allowed"}, label)
        if not isinstance(contract["allowed"], list) or len(contract["allowed"]) < 2:
            raise PolicyError(f"{label}.allowed must contain choices")
        choices = [_identifier(item, f"{label}.allowed") for item in contract["allowed"]]
        if len(set(choices)) != len(choices):
            raise PolicyError(f"{label}.allowed must be unique")
        return contract
    if kind == "score_level":
        contract = _exact_object(value, {"kind", "minimum", "maximum"}, label)
        minimum = contract["minimum"]
        maximum = contract["maximum"]
        if (
            not isinstance(minimum, int)
            or isinstance(minimum, bool)
            or not isinstance(maximum, int)
            or isinstance(maximum, bool)
            or minimum < 0
            or maximum < minimum
        ):
            raise PolicyError(f"{label} has an invalid score range")
        return contract
    raise PolicyError(f"{label}.kind is unsupported")


def _validate_outcome_value(value: Any, contract: dict[str, Any]) -> Any:
    if contract["kind"] == "boolean":
        if not isinstance(value, bool):
            raise PolicyError("noul outcome must be a JSON boolean")
    elif contract["kind"] == "choice":
        if not isinstance(value, str) or value not in contract["allowed"]:
            raise PolicyError("choice outcome is not an allowed option")
    elif not isinstance(value, int) or isinstance(value, bool) or not contract["minimum"] <= value <= contract["maximum"]:
        raise PolicyError("score outcome must be an integer policy level")
    return value


def _validate_policy_snapshot(
    value: Any,
    question_type: str,
    contract: dict[str, Any],
    risk: str,
    label: str,
) -> dict[str, Any]:
    required = {
        "question_id",
        "question_contract_sha256",
        "state_projection_version",
        "action_type",
        "autonomous_actions",
        "governance",
        "policy_mode",
        "calibration_status",
        "calibration_report_sha256",
        "approval_valid",
        "health_report_required",
        "health_status",
        "health_trusted",
        "thresholds",
        "abstain_values",
    }
    snapshot = _exact_object(value, required, label)
    _identifier(snapshot["question_id"], f"{label}.question_id")
    if (
        not isinstance(snapshot["question_contract_sha256"], str)
        or not SHA256.fullmatch(snapshot["question_contract_sha256"])
    ):
        raise PolicyError(f"{label}.question_contract_sha256 must be a SHA-256 digest")
    _identifier(snapshot["state_projection_version"], f"{label}.state_projection_version")
    action_type = _enum(snapshot["action_type"], ACTION_TYPES, f"{label}.action_type")
    actions = snapshot["autonomous_actions"]
    if not isinstance(actions, list):
        raise PolicyError(f"{label}.autonomous_actions must be an array")
    clean_actions = [_identifier(item, f"{label}.autonomous_actions") for item in actions]
    if len(clean_actions) != len(set(clean_actions)):
        raise PolicyError(f"{label}.autonomous_actions must be unique")
    if (risk == "high" or action_type in HIGH_IMPACT_ACTION_TYPES) and clean_actions:
        raise PolicyError(f"{label} grants autonomous actions to high-risk work")
    _validate_governance(snapshot["governance"], f"{label}.governance")
    _enum(snapshot["policy_mode"], MODES, f"{label}.policy_mode")
    _enum(
        snapshot["calibration_status"],
        {"calibrated", "uncalibrated"},
        f"{label}.calibration_status",
    )
    calibration_report = snapshot["calibration_report_sha256"]
    if snapshot["calibration_status"] == "calibrated":
        if not isinstance(calibration_report, str) or not SHA256.fullmatch(calibration_report):
            raise PolicyError(f"{label}.calibration_report_sha256 must be a SHA-256 digest")
    elif calibration_report is not None:
        raise PolicyError(f"{label}.calibration_report_sha256 must be null when uncalibrated")
    if not isinstance(snapshot["approval_valid"], bool):
        raise PolicyError(f"{label}.approval_valid must be a boolean")
    if not isinstance(snapshot["health_report_required"], bool):
        raise PolicyError(f"{label}.health_report_required must be a boolean")
    _enum(snapshot["health_status"], HEALTH_STATUSES, f"{label}.health_status")
    if not isinstance(snapshot["health_trusted"], bool):
        raise PolicyError(f"{label}.health_trusted must be a boolean")
    if snapshot["health_trusted"] and snapshot["health_status"] == "not_provided":
        raise PolicyError(f"{label}.health_trusted is inconsistent")
    _validate_thresholds(snapshot["thresholds"], question_type, f"{label}.thresholds")
    abstentions = snapshot["abstain_values"]
    if not isinstance(abstentions, list):
        raise PolicyError(f"{label}.abstain_values must be an array")
    if question_type == "choice":
        clean_abstentions = [_identifier(item, f"{label}.abstain_values") for item in abstentions]
        if len(clean_abstentions) != len(set(clean_abstentions)):
            raise PolicyError(f"{label}.abstain_values must be unique")
        if not set(clean_abstentions) <= set(contract["allowed"]):
            raise PolicyError(f"{label}.abstain_values are outside the choice contract")
    elif abstentions:
        raise PolicyError(f"{label}.abstain_values must be empty for {question_type}")
    return snapshot


def _semantic_event_values(
    event: dict[str, Any],
    contract: dict[str, Any],
    snapshot: dict[str, Any],
    label: str,
) -> tuple[Any, float | None, float, str, Any, float | None, str | None, str, str]:
    question_type = event["question_type"]
    answer = event["typed_answer"]
    if question_type == "choice":
        policy = {"question_type": "choice", "choices": contract["allowed"]}
        clean = _validate_answer(policy, answer)
        prediction = clean["choice"]
        routing_confidence = _probability(clean["confidence"], f"{label}.typed_answer.confidence")
        selected = _probability(
            clean["probabilities"][prediction],
            f"{label}.typed_answer.probabilities.selected",
        )
        basis = snapshot["thresholds"]["routing_signal"]
        routing_signal = routing_confidence if basis == "confidence" else selected
        return (
            prediction,
            routing_confidence,
            routing_signal,
            basis,
            prediction,
            selected,
            "selected_option_probability",
            "ready",
            "selected_choice_correctness",
        )
    if question_type == "noul":
        clean = _validate_answer({"question_type": "noul"}, answer)
        probability_yes = _probability(clean["noul"], f"{label}.typed_answer.noul")
        prediction = probability_yes >= 0.5
        return (
            prediction,
            None,
            probability_yes,
            "noul_probability_yes",
            prediction,
            probability_yes if prediction else 1.0 - probability_yes,
            "predicted_boolean_probability",
            "ready",
            "predicted_boolean_correctness",
        )

    if not isinstance(answer, dict) or not isinstance(answer.get("legend"), dict):
        raise PolicyError(f"{label}.typed_answer must contain a score legend")
    keys = [str(index) for index in range(contract["maximum"] + 1)]
    if set(answer["legend"]) != set(keys):
        raise PolicyError(f"{label}.typed_answer.legend does not match the score contract")
    levels = [answer["legend"][key] for key in keys]
    clean = _validate_answer({"question_type": "score", "levels": levels}, answer)
    score = _finite_number(clean["score"], f"{label}.typed_answer.score")
    routing_confidence = _probability(clean["confidence"], f"{label}.typed_answer.confidence")
    basis = snapshot["thresholds"]["routing_signal"]
    routing_signal = routing_confidence
    calibration_label = event["calibration_label"]
    calibration_probability = event["calibration_probability"]
    if calibration_label is None and calibration_probability is None:
        probability_basis = None
        calibration_status = "missing_explicit_score_target"
    else:
        calibration_label, calibration_probability = _explicit_score_calibration(
            {"levels": levels}, clean, calibration_label, calibration_probability
        )
        probability_basis = "caller_supplied_score_level_probability"
        calibration_status = "ready"
    return (
        score,
        routing_confidence,
        routing_signal,
        basis,
        calibration_label,
        calibration_probability,
        probability_basis,
        calibration_status,
        "declared_score_level_correctness",
    )


def _validate_judgment_provenance(
    value: Any,
    *,
    event: dict[str, Any],
    snapshot: dict[str, Any],
    label: str,
) -> dict[str, Any]:
    required = {
        "source", "trusted", "judgment_id", "request_hash",
        "question_contract_sha256", "question_id", "requested_model",
        "resolved_model", "endpoint", "judged_at", "cached",
        "state_projection_version",
    }
    provenance = _exact_object(value, required, label)
    source = _enum(
        provenance["source"],
        {"bare_typed_answer", "jev_run_envelope"},
        f"{label}.source",
    )
    if not isinstance(provenance["trusted"], bool):
        raise PolicyError(f"{label}.trusted must be a boolean")
    if source == "bare_typed_answer" and provenance["trusted"]:
        raise PolicyError(f"{label}.trusted does not match source")
    if provenance["question_id"] != snapshot["question_id"]:
        raise PolicyError(f"{label}.question_id does not match policy snapshot")
    if _digest_hex(
        provenance["question_contract_sha256"],
        f"{label}.question_contract_sha256",
    ) != _digest_hex(
        snapshot["question_contract_sha256"],
        f"{label}.policy question contract",
    ):
        raise PolicyError(f"{label}.question contract does not match policy snapshot")
    for field in ("requested_model", "resolved_model"):
        if _pinned_model(provenance[field], f"{label}.{field}") != event["model"]:
            raise PolicyError(f"{label}.{field} does not match event model")

    if source == "bare_typed_answer":
        for field in ("judgment_id", "request_hash", "endpoint", "judged_at", "cached"):
            if provenance[field] is not None:
                raise PolicyError(f"{label}.{field} must be null for a bare answer")
        if provenance["state_projection_version"] not in {
            None, snapshot["state_projection_version"]
        }:
            raise PolicyError(f"{label}.state_projection_version does not match policy snapshot")
        return provenance

    judgment_id = provenance["judgment_id"]
    if not isinstance(judgment_id, str) or not JUDGMENT_ID.fullmatch(judgment_id):
        raise PolicyError(f"{label}.judgment_id is invalid")
    request_hash = _digest_hex(provenance["request_hash"], f"{label}.request_hash")
    if event["request_hash"] != request_hash:
        raise PolicyError(f"{label}.request_hash does not match event request_hash")
    raw_endpoint = _nonempty_string(provenance["endpoint"], f"{label}.endpoint")
    try:
        endpoint = jev_judge.validate_endpoint(raw_endpoint)
    except (jev_judge.JevError, TypeError, ValueError) as exc:
        raise PolicyError(f"{label}.endpoint is invalid: {exc}") from None
    if endpoint != raw_endpoint:
        raise PolicyError(f"{label}.endpoint must be canonical")
    _timestamp(provenance["judged_at"], f"{label}.judged_at")
    if not isinstance(provenance["cached"], bool):
        raise PolicyError(f"{label}.cached must be a boolean")
    expected_trusted = endpoint == jev_judge.DEFAULT_ENDPOINT and not provenance["cached"]
    if provenance["trusted"] != expected_trusted:
        raise PolicyError(f"{label}.trusted does not match endpoint/cache provenance")
    if provenance["state_projection_version"] != snapshot["state_projection_version"]:
        raise PolicyError(f"{label}.state_projection_version does not match policy snapshot")
    return provenance


def _validate_event(value: Any, line_number: int | None = None) -> dict[str, Any]:
    label = f"ledger line {line_number}" if line_number is not None else "event"
    if not isinstance(value, dict):
        raise PolicyError(f"{label} must be a JSON object")
    event_type = value.get("event_type")
    if event_type == "decision":
        required = {
            "schema_version", "event_type", "event_id", "decision_id", "occurred_at",
            "registry_version", "policy_id", "policy_version", "model", "question_type",
            "risk", "route", "proposed_action", "prediction", "routing_confidence", "routing_signal", "routing_signal_basis",
            "calibration_label", "calibration_probability", "calibration_probability_basis",
            "calibration_status", "metric_basis", "reason_codes", "request_hash",
            "outcome_contract", "typed_answer", "policy_snapshot", "judgment_provenance",
        }
        event = _exact_object(value, required, label)
        if event["schema_version"] != EVENT_SCHEMA:
            raise PolicyError(f"{label}.schema_version is unsupported")
        _event_id(event["event_id"], f"{label}.event_id")
        _event_id(event["decision_id"], f"{label}.decision_id")
        _timestamp(event["occurred_at"], f"{label}.occurred_at")
        for field in ("registry_version", "policy_id", "policy_version"):
            _identifier(event[field], f"{label}.{field}")
        _pinned_model(event["model"], f"{label}.model")
        question_type = _enum(
            event["question_type"], QUESTION_TYPES, f"{label}.question_type"
        )
        risk = _enum(event["risk"], RISKS, f"{label}.risk")
        _enum(event["route"], ROUTES, f"{label}.route")
        if event["proposed_action"] is not None:
            _identifier(event["proposed_action"], f"{label}.proposed_action")
        if not isinstance(event["reason_codes"], list):
            raise PolicyError(f"{label}.reason_codes must be an array")
        for reason in event["reason_codes"]:
            _identifier(reason, f"{label}.reason_codes")
        if event["request_hash"] is not None and (
            not isinstance(event["request_hash"], str) or not SHA256.fullmatch(event["request_hash"])
        ):
            raise PolicyError(f"{label}.request_hash must be a SHA-256 digest")
        contract = _validate_outcome_contract(event["outcome_contract"], f"{label}.outcome_contract")
        if question_type == "choice" and contract["kind"] != "choice":
            raise PolicyError(f"{label}.outcome_contract does not match question_type")
        if question_type == "noul" and contract["kind"] != "boolean":
            raise PolicyError(f"{label}.outcome_contract does not match question_type")
        if question_type == "score" and contract["kind"] != "score_level":
            raise PolicyError(f"{label}.outcome_contract does not match question_type")
        snapshot = _validate_policy_snapshot(
            event["policy_snapshot"], question_type, contract, risk, f"{label}.policy_snapshot"
        )
        provenance = _validate_judgment_provenance(
            event["judgment_provenance"],
            event=event,
            snapshot=snapshot,
            label=f"{label}.judgment_provenance",
        )
        expected_approval_valid = _approval_is_valid(
            {"governance": snapshot["governance"]},
            _parse_timestamp(event["occurred_at"]),
        )
        if snapshot["approval_valid"] != expected_approval_valid:
            raise PolicyError(f"{label}.policy_snapshot.approval_valid is inconsistent")
        (
            expected_prediction,
            expected_confidence,
            expected_signal,
            expected_signal_basis,
            expected_calibration_label,
            expected_calibration_probability,
            expected_probability_basis,
            expected_calibration_status,
            expected_metric_basis,
        ) = _semantic_event_values(event, contract, snapshot, label)

        if type(event["prediction"]) is not type(expected_prediction) or event["prediction"] != expected_prediction:
            raise PolicyError(f"{label}.prediction does not match typed_answer")
        if event["routing_confidence"] != expected_confidence:
            raise PolicyError(f"{label}.routing_confidence does not match typed_answer")
        if event["routing_signal"] != expected_signal:
            raise PolicyError(f"{label}.routing_signal does not match typed_answer and thresholds")
        if event["routing_signal_basis"] != expected_signal_basis:
            raise PolicyError(f"{label}.routing_signal_basis does not match thresholds")
        if type(event["calibration_label"]) is not type(expected_calibration_label) or event["calibration_label"] != expected_calibration_label:
            raise PolicyError(f"{label}.calibration_label does not match typed_answer")
        if event["calibration_probability"] != expected_calibration_probability:
            raise PolicyError(f"{label}.calibration_probability does not match typed_answer")
        if event["calibration_probability_basis"] != expected_probability_basis:
            raise PolicyError(f"{label}.calibration_probability_basis does not match typed_answer")
        if event["calibration_status"] != expected_calibration_status:
            raise PolicyError(f"{label}.calibration_status does not match typed_answer")
        if event["metric_basis"] != expected_metric_basis:
            raise PolicyError(f"{label}.metric_basis does not match question_type")

        replay_policy = {
            "mode": snapshot["policy_mode"],
            "calibration": {"status": snapshot["calibration_status"]},
            "monitoring": {"require_health_report": snapshot["health_report_required"]},
            "question_type": question_type,
            "risk": risk,
            "action_type": snapshot["action_type"],
            "autonomous_actions": snapshot["autonomous_actions"],
            "thresholds": snapshot["thresholds"],
            "abstain_values": snapshot["abstain_values"],
        }
        expected_route, expected_reasons = _route_from_facts(
            replay_policy,
            expected_prediction,
            expected_signal,
            approval_valid=snapshot["approval_valid"],
            health_status=snapshot["health_status"],
            health_trusted=snapshot["health_trusted"],
            judgment_trusted=provenance["trusted"],
            proposed_action=event["proposed_action"],
        )
        if event["route"] != expected_route or event["reason_codes"] != expected_reasons:
            raise PolicyError(f"{label} route and reasons do not match its policy snapshot")
        return event
    if event_type == "outcome":
        event = _exact_object(
            value,
            {"schema_version", "event_type", "event_id", "decision_id", "occurred_at", "outcome"},
            label,
        )
        if event["schema_version"] != EVENT_SCHEMA:
            raise PolicyError(f"{label}.schema_version is unsupported")
        _event_id(event["event_id"], f"{label}.event_id")
        _event_id(event["decision_id"], f"{label}.decision_id")
        _timestamp(event["occurred_at"], f"{label}.occurred_at")
        if isinstance(event["outcome"], (dict, list)) or event["outcome"] is None:
            raise PolicyError(f"{label}.outcome must be a scalar label")
        if isinstance(event["outcome"], float) and not math.isfinite(event["outcome"]):
            raise PolicyError(f"{label}.outcome must be finite")
        return event
    raise PolicyError(f"{label}.event_type must be 'decision' or 'outcome'")


@contextmanager
def _locked_ledger(path: Path, *, exclusive: bool, create: bool):
    if path.is_symlink():
        raise PolicyError("ledger path must not be a symbolic link")
    if create:
        path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_RDWR | (os.O_CREAT if create else 0)
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor: int | None = None
    handle: Any = None
    try:
        descriptor = os.open(path, flags, 0o600)
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise PolicyError("ledger must be a regular file")
        if hasattr(os, "getuid") and metadata.st_uid != os.getuid():
            raise PolicyError("ledger must be owned by the current user")
        os.fchmod(descriptor, 0o600)
        handle = os.fdopen(descriptor, "r+", encoding="utf-8")
        descriptor = None
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
        yield handle
    except FileNotFoundError:
        raise
    except PolicyError:
        raise
    except OSError as exc:
        raise PolicyError(f"cannot open policy ledger: {exc}") from None
    finally:
        if handle is not None:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            finally:
                handle.close()
        elif descriptor is not None:
            os.close(descriptor)


def _append_locked(handle: Any, event: dict[str, Any]) -> None:
    _validate_event(event)
    try:
        payload = json.dumps(
            event,
            ensure_ascii=False,
            sort_keys=True,
            allow_nan=False,
            separators=(",", ":"),
        ) + "\n"
        handle.seek(0, os.SEEK_END)
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    except (OSError, TypeError, ValueError) as exc:
        raise PolicyError(f"cannot append policy ledger: {exc}") from None


def _append_event(path: Path, event: dict[str, Any]) -> None:
    with _locked_ledger(path, exclusive=True, create=True) as handle:
        existing_events = _read_events_locked(handle)
        decisions, _ = _index_events(existing_events)
        if event.get("event_type") == "decision" and event.get("decision_id") in decisions:
            raise PolicyError(f"duplicate decision event for {event['decision_id']}")
        if event.get("event_type") == "decision":
            judgment_id = event.get("judgment_provenance", {}).get("judgment_id")
            if judgment_id is not None and any(
                existing.get("event_type") == "decision"
                and existing.get("judgment_provenance", {}).get("judgment_id") == judgment_id
                for existing in existing_events
            ):
                raise PolicyError(f"duplicate decision for judgment {judgment_id}")
        _append_locked(handle, event)


def _outcome_contract(policy: dict[str, Any]) -> dict[str, Any]:
    if policy["question_type"] == "choice":
        return {"kind": "choice", "allowed": list(policy["choices"])}
    if policy["question_type"] == "noul":
        return {"kind": "boolean"}
    return {"kind": "score_level", "minimum": 0, "maximum": len(policy["levels"]) - 1}


def record_decision(
    registry: dict[str, Any],
    policy_id: str,
    answer: dict[str, Any],
    path: Path,
    *,
    resolved_model: str,
    proposed_action: str | None = None,
    state_projection_version: str | None = None,
    request_hash: str | None = None,
    calibration_label: Any = None,
    calibration_probability: Any = None,
    health_report: dict[str, Any] | None = None,
    health_events: Path | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Append a minimal decision event; raw request state is not accepted."""
    result = evaluate(
        registry,
        policy_id,
        answer,
        resolved_model=resolved_model,
        proposed_action=proposed_action,
        state_projection_version=state_projection_version,
        calibration_label=calibration_label,
        calibration_probability=calibration_probability,
        health_report=health_report,
        health_events=health_events,
        now=now,
    )
    provenance_hash = result["judgment_provenance"]["request_hash"]
    if request_hash is not None:
        request_hash = _digest_hex(request_hash, "request_hash")
    if provenance_hash is not None:
        if request_hash is not None and request_hash != provenance_hash:
            raise PolicyError("request_hash does not match judgment provenance")
        request_hash = provenance_hash
    policy = registry["policies"][policy_id]
    event = {
        "schema_version": EVENT_SCHEMA,
        "event_type": "decision",
        "event_id": str(uuid.uuid4()),
        "decision_id": str(uuid.uuid4()),
        "occurred_at": result["evaluated_at"],
        "registry_version": result["registry_version"],
        "policy_id": result["policy_id"],
        "policy_version": result["policy_version"],
        "model": result["model"],
        "question_type": result["question_type"],
        "risk": result["risk"],
        "route": result["route"],
        "proposed_action": result["proposed_action"],
        "prediction": result["prediction"],
        "routing_confidence": result["routing_confidence"],
        "routing_signal": result["routing_signal"],
        "routing_signal_basis": result["routing_signal_basis"],
        "calibration_label": result["calibration_label"],
        "calibration_probability": result["calibration_probability"],
        "calibration_probability_basis": result["calibration_probability_basis"],
        "calibration_status": result["calibration_status"],
        "metric_basis": result["metric_basis"],
        "reason_codes": result["reason_codes"],
        "request_hash": request_hash,
        "outcome_contract": _outcome_contract(policy),
        "typed_answer": result["typed_answer"],
        "policy_snapshot": result["policy_snapshot"],
        "judgment_provenance": result["judgment_provenance"],
    }
    _append_event(path, event)
    return event


def _reject_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON number {value!r} is not allowed")


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON object key {key!r}")
        result[key] = value
    return result


def _parse_events(lines: list[str]) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for index, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            value = json.loads(
                line,
                parse_constant=_reject_constant,
                object_pairs_hook=_reject_duplicate_keys,
            )
        except (json.JSONDecodeError, ValueError) as exc:
            raise PolicyError(f"invalid JSON on ledger line {index}: {exc}") from None
        events.append(_validate_event(value, index))
    return events


def _read_events_locked(handle: Any) -> list[dict[str, Any]]:
    handle.seek(0)
    return _parse_events(handle.read().splitlines())


def _read_events(path: Path) -> list[dict[str, Any]]:
    try:
        with _locked_ledger(path, exclusive=False, create=False) as handle:
            return _read_events_locked(handle)
    except FileNotFoundError:
        return []


def _index_events(events: list[dict[str, Any]]) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    decisions: dict[str, dict[str, Any]] = {}
    outcomes: dict[str, dict[str, Any]] = {}
    event_ids: set[str] = set()
    judgment_ids: set[str] = set()
    for event in events:
        if event["event_id"] in event_ids:
            raise PolicyError(f"duplicate event_id {event['event_id']}")
        event_ids.add(event["event_id"])
        decision_id = event["decision_id"]
        if event["event_type"] == "decision":
            if decision_id in decisions:
                raise PolicyError(f"duplicate decision event for {decision_id}")
            judgment_id = event["judgment_provenance"]["judgment_id"]
            if judgment_id is not None:
                if judgment_id in judgment_ids:
                    raise PolicyError(f"duplicate decision for judgment {judgment_id}")
                judgment_ids.add(judgment_id)
            decisions[decision_id] = event
        else:
            if decision_id not in decisions:
                raise PolicyError(f"outcome precedes or references unknown decision {decision_id}")
            if decision_id in outcomes:
                raise PolicyError(f"duplicate outcome event for {decision_id}")
            if _parse_timestamp(event["occurred_at"]) < _parse_timestamp(
                decisions[decision_id]["occurred_at"]
            ):
                raise PolicyError(f"outcome timestamp precedes decision {decision_id}")
            outcomes[decision_id] = event
    for decision_id, outcome in outcomes.items():
        _validate_outcome_value(outcome["outcome"], decisions[decision_id]["outcome_contract"])
    return decisions, outcomes


def record_outcome(path: Path, decision_id: str, outcome: Any) -> dict[str, Any]:
    """Append one observed label for an existing decision."""
    _event_id(decision_id, "decision_id")
    try:
        with _locked_ledger(path, exclusive=True, create=False) as handle:
            decisions, outcomes = _index_events(_read_events_locked(handle))
            if decision_id not in decisions:
                raise PolicyError(f"unknown decision {decision_id}")
            if decision_id in outcomes:
                raise PolicyError(f"decision {decision_id} already has an outcome")
            clean_outcome = _validate_outcome_value(
                outcome, decisions[decision_id]["outcome_contract"]
            )
            event = {
                "schema_version": EVENT_SCHEMA,
                "event_type": "outcome",
                "event_id": str(uuid.uuid4()),
                "decision_id": decision_id,
                "occurred_at": _utc_now(),
                "outcome": clean_outcome,
            }
            _append_locked(handle, event)
            return event
    except FileNotFoundError:
        raise PolicyError(f"unknown decision {decision_id}") from None


def _labels_equal(predicted: Any, observed: Any) -> bool:
    if type(predicted) is not type(observed):
        return False
    return bool(predicted == observed)


def _metrics(samples: list[tuple[float, bool]], bins: int) -> dict[str, Any]:
    if not samples:
        return {"count": 0, "brier": None, "ece": None, "bins": []}
    brier = sum((probability - float(correct)) ** 2 for probability, correct in samples) / len(samples)
    buckets: list[list[tuple[float, bool]]] = [[] for _ in range(bins)]
    for probability, correct in samples:
        bucket = min(int(probability * bins), bins - 1)
        buckets[bucket].append((probability, correct))
    details: list[dict[str, Any]] = []
    weighted_gap = 0.0
    for index, bucket in enumerate(buckets):
        if not bucket:
            continue
        average_probability = sum(item[0] for item in bucket) / len(bucket)
        accuracy = sum(1 for _, correct in bucket if correct) / len(bucket)
        gap = abs(average_probability - accuracy)
        weighted_gap += gap * len(bucket)
        details.append({
            "index": index,
            "lower_inclusive": index / bins,
            "upper_exclusive": (index + 1) / bins,
            "includes_probability_one": index == bins - 1,
            "count": len(bucket),
            "average_probability": average_probability,
            "accuracy": accuracy,
            "gap": gap,
        })
    return {
        "count": len(samples),
        "brier": brier,
        "ece": weighted_gap / len(samples),
        "bins": details,
    }


def _policy_contract_matches(event: dict[str, Any], policy: dict[str, Any]) -> bool:
    snapshot = event["policy_snapshot"]
    calibration_report = policy["calibration"].get("report_sha256")
    return (
        event["outcome_contract"] == _outcome_contract(policy)
        and event["risk"] == policy["risk"]
        and snapshot["question_id"] == policy["question_id"]
        and snapshot["question_contract_sha256"] == policy["question_contract_sha256"]
        and snapshot["state_projection_version"] == policy["state_projection_version"]
        and snapshot["action_type"] == policy["action_type"]
        and snapshot["autonomous_actions"] == policy["autonomous_actions"]
        and snapshot["governance"] == policy["governance"]
        and snapshot["policy_mode"] == policy["mode"]
        and snapshot["calibration_status"] == policy["calibration"]["status"]
        and snapshot["calibration_report_sha256"] == calibration_report
        and snapshot["health_report_required"] == policy["monitoring"]["require_health_report"]
        and snapshot["thresholds"] == policy["thresholds"]
        and snapshot["abstain_values"] == policy.get("abstain_values", [])
    )


def build_report(
    registry: dict[str, Any],
    policy_id: str,
    path: Path,
    *,
    bins: int = 10,
) -> dict[str, Any]:
    """Report pairing, Brier/ECE, policy caps, and frozen-baseline drift."""
    policy = _get_policy(registry, policy_id)
    if not isinstance(bins, int) or isinstance(bins, bool) or not 1 <= bins <= 1000:
        raise PolicyError("bins must be an integer in [1, 1000]")
    events = _read_events(path)
    _, outcomes = _index_events(events)
    matching: list[dict[str, Any]] = []
    ignored = 0
    for event in events:
        if event["event_type"] != "decision":
            continue
        if (
            event["registry_version"] == registry["registry_version"]
            and event["policy_id"] == policy_id
            and event["policy_version"] == policy["policy_version"]
            and event["model"] == policy["model"]
        ):
            if event["question_type"] != policy["question_type"] or not _policy_contract_matches(event, policy):
                raise PolicyError(f"decision {event['decision_id']} conflicts with the current policy contract")
            matching.append(event)
        else:
            ignored += 1

    labeled = 0
    missing_outcome = 0
    missing_probability = 0
    samples: list[tuple[float, bool]] = []
    labeled_records: list[tuple[float, bool] | None] = []
    for decision in matching:
        outcome_event = outcomes.get(decision["decision_id"])
        if outcome_event is None:
            missing_outcome += 1
            continue
        observed = _validate_outcome_value(outcome_event["outcome"], decision["outcome_contract"])
        labeled += 1
        probability = decision["calibration_probability"]
        label = decision["calibration_label"]
        if probability is None or label is None:
            missing_probability += 1
            labeled_records.append(None)
            continue
        sample = (_probability(probability, "calibration_probability"), _labels_equal(label, observed))
        samples.append(sample)
        labeled_records.append(sample)

    monitoring = policy["monitoring"]
    window_size = monitoring["window_size"]
    current_records = labeled_records[-window_size:]
    current_samples = [sample for sample in current_records if sample is not None]
    overall = _metrics(samples, bins)
    current = _metrics(current_samples, bins)
    current["labeled_count"] = len(current_records)
    current["missing_calibration_probability"] = len(current_records) - len(current_samples)
    frozen_baseline = dict(monitoring["baseline"])
    frozen_baseline["source"] = "policy_registry_frozen_baseline"
    alerts: list[dict[str, Any]] = []
    minimum = monitoring["minimum_labeled"]
    if len(current_records) < minimum:
        alerts.append({
            "code": "insufficient_current_labeled",
            "observed": len(current_records),
            "required": minimum,
        })
    elif len(current_samples) < minimum:
        alerts.append({
            "code": "insufficient_current_scored",
            "observed": len(current_samples),
            "required": minimum,
            "missing_calibration_probability": len(current_records) - len(current_samples),
        })
    else:
        for metric in ("brier", "ece"):
            limit = monitoring[f"max_{metric}"]
            observed = current[metric]
            if observed is not None and observed > limit:
                alerts.append({
                    "code": f"{metric}_policy_limit_exceeded",
                    "observed": observed,
                    "limit": limit,
                })
        for metric in ("brier", "ece"):
            current_value = current[metric]
            baseline_value = frozen_baseline[metric]
            limit = monitoring[f"max_{metric}_delta"]
            if current_value is None:
                continue
            delta = current_value - baseline_value
            if delta > limit:
                alerts.append({
                    "code": f"{metric}_baseline_drift",
                    "baseline_id": frozen_baseline["baseline_id"],
                    "observed_delta": delta,
                    "limit": limit,
                    "baseline": baseline_value,
                    "current": current_value,
                })

    drift_codes = [alert for alert in alerts if not alert["code"].startswith("insufficient_")]
    if drift_codes:
        drift_status = "alert"
    elif len(current_records) < minimum or len(current_samples) < minimum:
        drift_status = "insufficient_data"
    else:
        drift_status = "stable"
    metric_basis = {
        "choice": "selected_choice_correctness",
        "noul": "predicted_boolean_correctness",
        "score": "declared_score_level_correctness",
    }[policy["question_type"]]
    return {
        "schema_version": "jev-policy-report-v1",
        "generated_at": _utc_now(),
        "registry_version": registry["registry_version"],
        "policy_id": policy_id,
        "policy_version": policy["policy_version"],
        "model": policy["model"],
        "metric_basis": metric_basis,
        "ece_bins": bins,
        "counts": {
            "decisions": len(matching),
            "labeled": labeled,
            "missing_outcome": missing_outcome,
            "missing_calibration_probability": missing_probability,
            "scored": len(samples),
            "ignored_other_policy_versions": ignored,
        },
        "overall": overall,
        "baseline_window": frozen_baseline,
        "current_window": current,
        "window_size": window_size,
        "minimum_labeled": minimum,
        "policy_limits": {
            key: monitoring[key]
            for key in ("max_brier", "max_ece", "max_brier_delta", "max_ece_delta")
        },
        "drift_status": drift_status,
        "authority_recommendation": "eligible" if drift_status == "stable" else "shadow",
        "alerts": alerts,
    }


def load_json(path: str) -> Any:
    try:
        if path == "-":
            return json.load(
                sys.stdin,
                parse_constant=_reject_constant,
                object_pairs_hook=_reject_duplicate_keys,
            )
        with open(path, encoding="utf-8") as handle:
            return json.load(
                handle,
                parse_constant=_reject_constant,
                object_pairs_hook=_reject_duplicate_keys,
            )
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        raise PolicyError(f"cannot read JSON {path!r}: {exc}") from None


def _json_literal(value: str, label: str) -> Any:
    try:
        return json.loads(
            value,
            parse_constant=_reject_constant,
            object_pairs_hook=_reject_duplicate_keys,
        )
    except (json.JSONDecodeError, ValueError) as exc:
        raise PolicyError(f"invalid {label}: {exc}") from None


def _evaluation_args(parser: argparse.ArgumentParser, *, ledger: bool = False) -> None:
    parser.add_argument("registry", help="versioned policy registry JSON")
    parser.add_argument("policy_id", help="policy identifier in the registry")
    parser.add_argument("answer", help="typed JEV answer or jev_judge run envelope JSON, or - for stdin")
    parser.add_argument("--model", required=True, help="resolved model reported by JEV")
    parser.add_argument("--proposed-action", help="exact stable action ID proposed for this decision")
    parser.add_argument(
        "--state-projection-version",
        help="projection version bound to the judgment request; required for trusted envelopes unless present in meta",
    )
    parser.add_argument("--calibration-label", help="JSON scalar; required with --calibration-probability")
    parser.add_argument("--calibration-probability", type=float)
    parser.add_argument(
        "--health-report",
        help="untrusted external report; it may downgrade but cannot enable auto",
    )
    parser.add_argument(
        "--health-events",
        type=Path,
        help="policy event ledger used to recompute trusted health in-process",
    )
    if ledger:
        parser.add_argument("--events", type=Path, required=True, help="append-only JSONL ledger")
        parser.add_argument("--request-hash", help="optional SHA-256 of the redacted request")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    check = sub.add_parser("check-policy", help="strictly validate a versioned registry")
    check.add_argument("registry")

    evaluate_parser = sub.add_parser("evaluate", help="route one JEV judgment without writing")
    _evaluation_args(evaluate_parser)

    decision = sub.add_parser("record-decision", help="evaluate and append a minimal decision event")
    _evaluation_args(decision, ledger=True)

    outcome = sub.add_parser("record-outcome", help="append an observed outcome for a decision")
    outcome.add_argument("events", type=Path)
    outcome.add_argument("decision_id")
    outcome.add_argument("--outcome-json", required=True, help="observed scalar label encoded as JSON")

    report = sub.add_parser("report", help="report pairing, Brier, ECE, and drift alerts")
    report.add_argument("registry")
    report.add_argument("policy_id")
    report.add_argument("events", type=Path)
    report.add_argument("--bins", type=int, default=10)
    return parser


def _calibration_cli_values(args: argparse.Namespace) -> tuple[Any, Any]:
    label = None
    if args.calibration_label is not None:
        label = _json_literal(args.calibration_label, "calibration label")
    return label, args.calibration_probability


def _print_json(value: Any, *, stream: Any = None) -> None:
    print(
        json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False),
        file=sys.stdout if stream is None else stream,
    )


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "check-policy":
            registry = validate_registry(load_json(args.registry))
            _print_json({
                "ok": True,
                "registry_version": registry["registry_version"],
                "policies": sorted(registry["policies"]),
            })
            return 0
        if args.command == "record-outcome":
            event = record_outcome(
                args.events,
                args.decision_id,
                _json_literal(args.outcome_json, "outcome"),
            )
            _print_json(event)
            return 0

        registry = validate_registry(load_json(args.registry))
        if args.command == "report":
            _print_json(build_report(registry, args.policy_id, args.events, bins=args.bins))
            return 0

        answer = load_json(args.answer)
        label, probability = _calibration_cli_values(args)
        health_report = load_json(args.health_report) if args.health_report else None
        if args.command == "evaluate":
            result = evaluate(
                registry,
                args.policy_id,
                answer,
                resolved_model=args.model,
                proposed_action=args.proposed_action,
                state_projection_version=args.state_projection_version,
                calibration_label=label,
                calibration_probability=probability,
                health_report=health_report,
                health_events=args.health_events,
            )
        else:
            result = record_decision(
                registry,
                args.policy_id,
                answer,
                args.events,
                resolved_model=args.model,
                proposed_action=args.proposed_action,
                state_projection_version=args.state_projection_version,
                request_hash=args.request_hash,
                calibration_label=label,
                calibration_probability=probability,
                health_report=health_report,
                health_events=args.health_events,
            )
        _print_json(result)
        return 0
    except (PolicyError, OSError, TypeError, ValueError) as exc:
        _print_json({"ok": False, "error": str(exc)}, stream=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

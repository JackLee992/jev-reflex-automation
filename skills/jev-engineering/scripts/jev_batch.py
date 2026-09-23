#!/usr/bin/env python3
"""Batch-map and evaluate pinned JEV judgments with safe local defaults.

``map`` validates and previews requests unless ``--send`` is explicit. ``eval``
adds typed offline metrics; a holdout set never selects its own Noul threshold.
Only compact, state-free summaries are printed. Use ``--output`` for the full,
redacted result written atomically as a private JSON file.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import json
import math
import os
from pathlib import Path
import re
import stat
import sys
import tempfile
from typing import Any, Callable, Iterable

import jev_judge


DEFAULT_CONCURRENCY = 4
MAX_CONCURRENCY = 32
DEFAULT_MAX_ROWS = 20
MAX_STDOUT_ROWS = 100
DEFAULT_MAX_ERRORS = 20
MAX_ERRORS = 100
CONCRETE_MODEL = re.compile(r"^jev-[0-9]+\.[0-9]+(?:\.[0-9]+)?$")
CONFIDENCE_LEVELS = (0.0, 0.5, 0.6, 0.7, 0.8, 0.9, 0.95)


class BatchError(RuntimeError):
    """A safe, user-displayable batch validation error."""


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-standard JSON number {value!r} is not allowed")


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, child in pairs:
        if key in value:
            raise ValueError(f"duplicate JSON object key {key!r} is not allowed")
        value[key] = child
    return value


def _load_json_file(path: str, label: str) -> Any:
    try:
        if path == "-":
            return json.load(
                sys.stdin,
                parse_constant=_reject_json_constant,
                object_pairs_hook=_unique_json_object,
            )
        with open(path, encoding="utf-8") as handle:
            return json.load(
                handle,
                parse_constant=_reject_json_constant,
                object_pairs_hook=_unique_json_object,
            )
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise BatchError(f"cannot read {label} {path!r}: {exc}") from None


def _ensure_json_value(value: Any, path: str = "value", depth: int = 0) -> None:
    if depth > 100:
        raise BatchError(f"{path} is nested too deeply")
    if value is None or isinstance(value, (str, bool, int)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise BatchError(f"{path} must not contain NaN or infinity")
        return
    if isinstance(value, list):
        for index, child in enumerate(value):
            _ensure_json_value(child, f"{path}[{index}]", depth + 1)
        return
    if isinstance(value, dict):
        for key, child in value.items():
            if not isinstance(key, str):
                raise BatchError(f"{path} object keys must be strings")
            _ensure_json_value(child, f"{path}.{key}", depth + 1)
        return
    raise BatchError(f"{path} contains non-JSON value {type(value).__name__}")


def validate_items(items: Any) -> list[dict[str, Any]]:
    """Validate stable identities and state envelopes, preserving input order."""
    if not isinstance(items, list):
        raise BatchError("dataset must be a JSON array or an object containing an items array")
    seen: set[str] = set()
    validated: list[dict[str, Any]] = []
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            raise BatchError(f"dataset item {index} must be an object")
        item_id = item.get("id")
        if not isinstance(item_id, str) or not jev_judge.SAFE_ID.fullmatch(item_id):
            raise BatchError(
                f"dataset item {index} id must be a stable identifier, not free text or sensitive data"
            )
        if item_id in seen:
            raise BatchError(f"dataset contains duplicate id {item_id!r}")
        seen.add(item_id)
        state_value = item.get("state")
        if not isinstance(state_value, dict):
            raise BatchError(f"dataset item {item_id!r} state must be a JSON object")
        _ensure_json_value(state_value, f"dataset item {item_id!r}.state")
        unknown = set(item) - {"id", "state", "label"}
        if unknown:
            names = ", ".join(sorted(str(name) for name in unknown))
            raise BatchError(f"dataset item {item_id!r} has unsupported fields: {names}")
        if "label" in item:
            _ensure_json_value(item["label"], f"dataset item {item_id!r}.label")
        validated.append(dict(item))
    return validated


def load_items(path: str) -> list[dict[str, Any]]:
    """Load a JSON array/object or line-delimited JSON dataset."""
    suffix = Path(path).suffix.lower() if path != "-" else ""
    if suffix in {".jsonl", ".ndjson"}:
        rows: list[Any] = []
        try:
            with open(path, encoding="utf-8") as handle:
                for line_number, line in enumerate(handle, 1):
                    if not line.strip():
                        continue
                    try:
                        rows.append(json.loads(
                            line,
                            parse_constant=_reject_json_constant,
                            object_pairs_hook=_unique_json_object,
                        ))
                    except (json.JSONDecodeError, ValueError) as exc:
                        raise BatchError(
                            f"cannot read dataset {path!r} line {line_number}: {exc}"
                        ) from None
        except BatchError:
            raise
        except (OSError, UnicodeError) as exc:
            raise BatchError(f"cannot read dataset {path!r}: {exc}") from None
        return validate_items(rows)

    document = _load_json_file(path, "dataset")
    if isinstance(document, dict) and "items" in document:
        document = document["items"]
    return validate_items(document)


def load_questions(path: str, wrapper_name: str = "questions") -> dict[str, dict[str, Any]]:
    """Load and validate a direct typed contract or a named wrapper object."""
    document = _load_json_file(path, wrapper_name)
    candidates = [document]
    if isinstance(document, dict) and wrapper_name in document:
        candidates.append(document[wrapper_name])
    last_error: BaseException | None = None
    for candidate in candidates:
        try:
            jev_judge.validate_request({"state": {}, "questions": candidate})
            document = candidate
            break
        except (jev_judge.JevError, TypeError, ValueError) as exc:
            last_error = exc
    else:
        raise BatchError(f"invalid {wrapper_name}: {last_error}") from None
    # JSON loading guarantees string keys and JSON values. Copy the outer map so
    # callers cannot accidentally mutate an enclosing wrapper after validation.
    return dict(document)


def validate_model(model: Any) -> str:
    """Require a concrete model version; moving aliases are not reproducible."""
    if not isinstance(model, str) or not CONCRETE_MODEL.fullmatch(model):
        raise BatchError(
            "model must be a concrete pinned version such as 'jev-1.13.0'; aliases are refused"
        )
    return model


def _validate_runtime_options(concurrency: int, max_string: int) -> None:
    if isinstance(concurrency, bool) or not isinstance(concurrency, int) or not 1 <= concurrency <= MAX_CONCURRENCY:
        raise BatchError(f"concurrency must be an integer in 1..{MAX_CONCURRENCY}")
    if isinstance(max_string, bool) or not isinstance(max_string, int) or max_string < 200:
        raise BatchError("max_string must be an integer of at least 200")


def _validate_call_options(
    *, timeout: float, retries: int, cache_ttl_seconds: float, send: bool
) -> None:
    if not isinstance(send, bool):
        raise BatchError("send must be boolean")
    if (
        isinstance(timeout, bool) or not isinstance(timeout, (int, float))
        or not math.isfinite(timeout) or timeout <= 0
    ):
        raise BatchError("timeout must be a finite positive number")
    if isinstance(retries, bool) or not isinstance(retries, int) or not 1 <= retries <= 20:
        raise BatchError("retries must be an integer in 1..20")
    if (
        isinstance(cache_ttl_seconds, bool) or not isinstance(cache_ttl_seconds, (int, float))
        or not math.isfinite(cache_ttl_seconds) or cache_ttl_seconds < 0
    ):
        raise BatchError("cache_ttl_seconds must be a finite non-negative number")


def _safe_error(exc: BaseException, max_string: int) -> dict[str, str]:
    clean_message, _ = jev_judge.redact(str(exc), max_string=max_string)
    if not isinstance(clean_message, str):
        clean_message = "batch item failed"
    return {"type": type(exc).__name__, "message": clean_message}


def _prepare_item(
    item: dict[str, Any],
    questions: dict[str, dict[str, Any]],
    model: str,
    max_string: int,
) -> tuple[dict[str, Any], int, str]:
    payload = {"state": item["state"], "questions": questions}
    try:
        return jev_judge.prepare_request(payload, model, max_string)
    except (jev_judge.JevError, TypeError, ValueError) as exc:
        raise BatchError(str(exc)) from None


def _safe_meta(value: Any, max_string: int) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    allowed = {
        "cached", "latency_ms", "request_hash", "question_contract_hash", "requested_model",
        "response_model", "status", "redactions", "cache_schema", "endpoint",
    }
    selected = {key: value[key] for key in allowed if key in value}
    clean, _ = jev_judge.redact(selected, max_string=max_string)
    return clean if isinstance(clean, dict) else {}


def run_batch(
    items: Any,
    questions: dict[str, dict[str, Any]],
    *,
    send: bool = False,
    model: str = jev_judge.DEFAULT_MODEL,
    concurrency: int = DEFAULT_CONCURRENCY,
    runner: Callable[..., dict[str, Any]] | None = None,
    endpoint: str = jev_judge.DEFAULT_ENDPOINT,
    timeout: float = 15.0,
    retries: int = 4,
    cache_dir: Path | None = None,
    audit: Path | None = None,
    cache_ttl_seconds: float = jev_judge.DEFAULT_CACHE_TTL_SECONDS,
    max_string: int = 6000,
) -> dict[str, Any]:
    """Preview or execute a typed contract over items with bounded concurrency."""
    validated_items = validate_items(items)
    validate_model(model)
    _validate_runtime_options(concurrency, max_string)
    _validate_call_options(
        timeout=timeout,
        retries=retries,
        cache_ttl_seconds=cache_ttl_seconds,
        send=send,
    )
    try:
        jev_judge.validate_request({"state": {}, "questions": questions})
        # Fail once, before any worker/network activity, if the shared contract
        # would be changed by redaction or violates the request-size policy.
        jev_judge.prepare_request({"state": {}, "questions": questions}, model, max_string)
    except (jev_judge.JevError, TypeError, ValueError) as exc:
        raise BatchError(f"invalid questions: {exc}") from None
    resolved_runner = runner if runner is not None else jev_judge.run_request

    def process(item: dict[str, Any]) -> dict[str, Any]:
        row: dict[str, Any] = {"id": item["id"]}
        if "label" in item:
            clean_label, _ = jev_judge.redact(item["label"], max_string=max_string)
            row["label"] = clean_label
        try:
            body, redactions, request_hash = _prepare_item(item, questions, model, max_string)
            if not send:
                row.update({
                    "status": "preview",
                    "request_hash": request_hash,
                    "redactions": redactions,
                    "request": body,
                })
                return row

            # The custom/default runner sees only the already-redacted request.
            call_payload = {"state": body["state"], "questions": body["questions"]}
            envelope = resolved_runner(
                call_payload,
                model=model,
                endpoint=endpoint,
                timeout=timeout,
                retries=retries,
                cache_dir=cache_dir,
                audit=audit,
                cache_ttl_seconds=cache_ttl_seconds,
                max_string=max_string,
            )
            if not isinstance(envelope, dict) or not isinstance(envelope.get("response"), dict):
                raise BatchError("JEV runner must return a response envelope")
            clean_envelope, response_redactions = jev_judge.redact(envelope, max_string=max_string)
            if not isinstance(clean_envelope, dict) or not isinstance(clean_envelope.get("response"), dict):
                raise BatchError("JEV runner returned an invalid response envelope")
            _ensure_json_value(clean_envelope["response"], "JEV response")
            validated_response = jev_judge.validate_response(body, clean_envelope["response"])
            row.update({
                "status": "ok",
                "request_hash": request_hash,
                "redactions": redactions + response_redactions,
                "model": validated_response["model"],
                "answers": validated_response["answers"],
                "usage": validated_response["usage"],
                "meta": _safe_meta(clean_envelope.get("meta"), max_string),
            })
        except Exception as exc:  # Per-item isolation is deliberate at this boundary.
            row.update({"status": "error", "error": _safe_error(exc, max_string)})
        return row

    if send and len(validated_items) > 1:
        # executor.map yields in input order while the pool keeps work bounded.
        with ThreadPoolExecutor(max_workers=concurrency, thread_name_prefix="jev-batch") as pool:
            rows = list(pool.map(process, validated_items))
    else:
        rows = [process(item) for item in validated_items]

    succeeded = sum(row["status"] in {"ok", "preview"} for row in rows)
    return {
        "schema_version": "1",
        "mode": "map",
        "network": send,
        "model": model,
        "question_keys": list(questions),
        "total": len(rows),
        "succeeded": succeeded,
        "failed": len(rows) - succeeded,
        "rows": rows,
    }


def _label_for(row: dict[str, Any], name: str, variant_count: int) -> Any:
    if "label" not in row:
        raise BatchError(f"evaluation row {row.get('id', '<unknown>')!r} is missing label")
    label = row["label"]
    if isinstance(label, dict):
        if name not in label:
            raise BatchError(f"evaluation row {row.get('id', '<unknown>')!r} label is missing {name!r}")
        return label[name]
    if variant_count != 1:
        raise BatchError(
            f"evaluation row {row.get('id', '<unknown>')!r} needs a label object keyed by variant"
        )
    return label


def _binary_label(value: Any, location: str) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)) and not isinstance(value, bool) and value in {0, 1}:
        return int(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "yes", "y", "1", "positive"}:
            return 1
        if normalized in {"false", "no", "n", "0", "negative"}:
            return 0
    raise BatchError(f"{location} must be a binary Noul label")


def _choice_label(value: Any, options: Iterable[str], location: str) -> str:
    if isinstance(value, str) and value in set(options):
        return value
    raise BatchError(f"{location} must equal one of the choice option identifiers")


def _score_label(value: Any, criteria: list[Any], location: str) -> float:
    maximum = len(criteria) - 1
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        numeric = float(value)
        if math.isfinite(numeric) and 0 <= numeric <= maximum:
            return numeric
    if isinstance(value, str):
        exact = [index for index, criterion in enumerate(criteria) if value == str(criterion)]
        if len(exact) == 1:
            return float(exact[0])
        folded = [
            index for index, criterion in enumerate(criteria)
            if value.strip().casefold() == str(criterion).strip().casefold()
        ]
        if len(folded) == 1:
            return float(folded[0])
    raise BatchError(f"{location} must be a rubric index or exact rubric label")


def _validate_eval_labels(items: list[dict[str, Any]], variants: dict[str, dict[str, Any]]) -> None:
    count = len(variants)
    for item in items:
        for name, question in variants.items():
            location = f"evaluation item {item['id']!r} label for {name!r}"
            value = _label_for(item, name, count)
            if question["type"] == "noul":
                _binary_label(value, location)
            elif question["type"] == "choice":
                _choice_label(value, question["criteria"], location)
            else:
                _score_label(value, question["criteria"], location)


def _validate_eval_options(
    variants: dict[str, dict[str, Any]],
    *,
    dataset_role: str,
    threshold: float | None,
    thresholds: dict[str, float] | None,
    max_errors: int,
) -> dict[str, float]:
    if dataset_role not in {"tuning", "holdout"}:
        raise BatchError("dataset_role must be explicitly 'tuning' or 'holdout'")
    if isinstance(max_errors, bool) or not isinstance(max_errors, int) or not 0 <= max_errors <= MAX_ERRORS:
        raise BatchError(f"max_errors must be an integer in 0..{MAX_ERRORS}")
    noul_names = [name for name, question in variants.items() if question["type"] == "noul"]
    has_scalar = threshold is not None
    has_mapping = thresholds is not None

    if dataset_role == "tuning":
        if has_scalar or has_mapping:
            raise BatchError(
                "tuning selects thresholds; do not provide frozen --threshold or --thresholds"
            )
        return {}
    if not noul_names:
        if has_scalar or has_mapping:
            raise BatchError("threshold options are invalid because the contract has no Noul variants")
        return {}
    if has_scalar and has_mapping:
        raise BatchError("provide either --threshold or --thresholds, not both")

    if len(noul_names) > 1:
        if has_scalar:
            raise BatchError(
                "multiple Noul variants require --thresholds JSON with one threshold per variant"
            )
        if not has_mapping:
            raise BatchError(
                "holdout evaluation with multiple Noul variants requires --thresholds JSON"
            )
    elif not has_scalar and not has_mapping:
        raise BatchError(
            "holdout Noul evaluation requires --threshold, or an exact --thresholds JSON mapping"
        )

    if has_scalar:
        if (
            isinstance(threshold, bool) or not isinstance(threshold, (int, float))
            or not math.isfinite(threshold) or not 0 <= threshold <= 1
        ):
            raise BatchError("threshold must be a finite number in [0, 1]")
        return {noul_names[0]: float(threshold)}

    if not isinstance(thresholds, dict):
        raise BatchError("thresholds must be a JSON object keyed by Noul variant")
    actual_keys = set(thresholds)
    expected_keys = set(noul_names)
    if actual_keys != expected_keys:
        missing = sorted(expected_keys - actual_keys)
        unexpected = sorted((actual_keys - expected_keys), key=str)
        details = []
        if missing:
            details.append(f"missing {missing}")
        if unexpected:
            details.append(f"unexpected {unexpected}")
        raise BatchError(
            "thresholds keys must exactly match all Noul variants: " + "; ".join(details)
        )
    resolved: dict[str, float] = {}
    for name in noul_names:
        value = thresholds[name]
        if (
            isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(value) or not 0 <= value <= 1
        ):
            raise BatchError(f"thresholds[{name!r}] must be a finite number in [0, 1]")
        resolved[name] = float(value)
    return resolved


def _threshold_metrics(pairs: list[tuple[str, int, float]], threshold: float) -> dict[str, Any]:
    tp = fp = tn = fn = 0
    for _item_id, label, probability in pairs:
        predicted = int(probability >= threshold)
        if label and predicted:
            tp += 1
        elif not label and predicted:
            fp += 1
        elif label and not predicted:
            fn += 1
        else:
            tn += 1
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    total = len(pairs)
    return {
        "threshold": threshold,
        "tp": tp,
        "fp": fp,
        "tn": tn,
        "fn": fn,
        "precision": precision,
        "recall": recall,
        "f1": f1,
        "accuracy": (tp + tn) / total if total else None,
    }


def _binary_auc(pairs: list[tuple[str, int, float]]) -> float | None:
    positives = sum(label for _item_id, label, _probability in pairs)
    negatives = len(pairs) - positives
    if not positives or not negatives:
        return None
    wins = 0.0
    negatives_below = 0
    ordered = sorted((probability, label) for _item_id, label, probability in pairs)
    index = 0
    while index < len(ordered):
        end = index + 1
        while end < len(ordered) and ordered[end][0] == ordered[index][0]:
            end += 1
        group = ordered[index:end]
        group_positives = sum(label for _probability, label in group)
        group_negatives = len(group) - group_positives
        wins += group_positives * (negatives_below + 0.5 * group_negatives)
        negatives_below += group_negatives
        index = end
    return wins / (positives * negatives)


def _ece(pairs: list[tuple[str, int, float]], bin_count: int = 10) -> float | None:
    if not pairs:
        return None
    total_error = 0.0
    for index in range(bin_count):
        lower = index / bin_count
        upper = (index + 1) / bin_count
        bucket = [
            (label, probability) for _item_id, label, probability in pairs
            if lower <= probability < upper or (index == bin_count - 1 and probability == 1.0)
        ]
        if not bucket:
            continue
        mean_probability = sum(probability for _label, probability in bucket) / len(bucket)
        event_rate = sum(label for label, _probability in bucket) / len(bucket)
        total_error += len(bucket) / len(pairs) * abs(mean_probability - event_rate)
    return total_error


def _worst_noul_misses(
    pairs: list[tuple[str, int, float]], threshold: float, maximum: int
) -> list[dict[str, Any]]:
    misses = []
    for item_id, label, probability in pairs:
        predicted = int(probability >= threshold)
        if predicted != label:
            misses.append({
                "id": item_id,
                "label": bool(label),
                "predicted": bool(predicted),
                "probability": probability,
                "error": abs(probability - label),
            })
    misses.sort(key=lambda row: (-row["error"], row["id"]))
    return misses[:maximum]


def _noul_metrics(
    pairs: list[tuple[str, int, float]],
    *,
    dataset_role: str,
    threshold: float | None,
    max_errors: int,
    total_rows: int,
) -> dict[str, Any]:
    metric: dict[str, Any] = {
        "type": "noul",
        "total": total_rows,
        "evaluated": len(pairs),
        "coverage": len(pairs) / total_rows if total_rows else 0.0,
        "brier": (
            sum((probability - label) ** 2 for _item_id, label, probability in pairs) / len(pairs)
            if pairs else None
        ),
        "ece": _ece(pairs),
        "auc": _binary_auc(pairs),
    }
    if dataset_role == "tuning":
        # A fixed grid keeps reports and runtime bounded even for very large sets.
        candidates = [round(step / 20, 6) for step in range(21)]
        sweep = [_threshold_metrics(pairs, candidate) for candidate in candidates]
        metric["threshold_sweep"] = sweep
        if sweep:
            best = max(
                sweep,
                key=lambda row: (
                    row["f1"],
                    row["accuracy"] if row["accuracy"] is not None else -1,
                    -abs(row["threshold"] - 0.5),
                    -row["threshold"],
                ),
            )
            selected = float(best["threshold"])
        else:
            selected = 0.5
        metric["recommended_threshold"] = selected
    else:
        if threshold is None:  # Checked at the public boundary; keeps this helper total.
            raise BatchError("holdout Noul evaluation requires an explicit --threshold")
        selected = threshold
    metric["threshold_evaluation"] = _threshold_metrics(pairs, selected)
    metric["worst_misses"] = _worst_noul_misses(pairs, selected, max_errors)
    return metric


def _choice_metrics(
    values: list[tuple[str, str, str, float]],
    *,
    options: list[str],
    max_errors: int,
    total_rows: int,
) -> dict[str, Any]:
    confusion = {actual: {predicted: 0 for predicted in options} for actual in options}
    for _item_id, actual, predicted, _confidence in values:
        confusion[actual][predicted] += 1
    correct = sum(actual == predicted for _item_id, actual, predicted, _confidence in values)
    class_metrics: dict[str, dict[str, Any]] = {}
    f1_values: list[float] = []
    for option in options:
        tp = confusion[option][option]
        fp = sum(confusion[actual][option] for actual in options if actual != option)
        fn = sum(confusion[option][predicted] for predicted in options if predicted != option)
        precision = tp / (tp + fp) if tp + fp else 0.0
        recall = tp / (tp + fn) if tp + fn else 0.0
        f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
        f1_values.append(f1)
        class_metrics[option] = {
            "support": sum(confusion[option].values()),
            "precision": precision,
            "recall": recall,
            "f1": f1,
        }
    coverage = []
    for minimum in CONFIDENCE_LEVELS:
        kept = [value for value in values if value[3] >= minimum]
        kept_correct = sum(actual == predicted for _item_id, actual, predicted, _confidence in kept)
        coverage.append({
            "min_confidence": minimum,
            "covered": len(kept),
            "coverage": len(kept) / len(values) if values else 0.0,
            "accuracy": kept_correct / len(kept) if kept else None,
        })
    misses = [
        {
            "id": item_id,
            "label": actual,
            "predicted": predicted,
            "confidence": confidence,
        }
        for item_id, actual, predicted, confidence in values if actual != predicted
    ]
    misses.sort(key=lambda row: (-row["confidence"], row["id"]))
    return {
        "type": "choice",
        "total": total_rows,
        "evaluated": len(values),
        "prediction_coverage": len(values) / total_rows if total_rows else 0.0,
        "accuracy": correct / len(values) if values else None,
        "confusion": confusion,
        "per_class": class_metrics,
        "macro_f1": sum(f1_values) / len(f1_values) if f1_values else None,
        "coverage": coverage,
        "worst_misses": misses[:max_errors],
    }


def _score_metrics(
    values: list[tuple[str, float, float, float]],
    *,
    max_errors: int,
    total_rows: int,
) -> dict[str, Any]:
    errors = [abs(predicted - actual) for _item_id, actual, predicted, _confidence in values]
    coverage = []
    for minimum in CONFIDENCE_LEVELS:
        kept = [value for value in values if value[3] >= minimum]
        kept_errors = [abs(predicted - actual) for _item_id, actual, predicted, _confidence in kept]
        coverage.append({
            "min_confidence": minimum,
            "covered": len(kept),
            "coverage": len(kept) / len(values) if values else 0.0,
            "mae": sum(kept_errors) / len(kept_errors) if kept_errors else None,
            "rmse": (
                math.sqrt(sum(error * error for error in kept_errors) / len(kept_errors))
                if kept_errors else None
            ),
        })
    misses = [
        {
            "id": item_id,
            "label": actual,
            "predicted": predicted,
            "confidence": confidence,
            "absolute_error": abs(predicted - actual),
        }
        for item_id, actual, predicted, confidence in values
        if abs(predicted - actual) > 0
    ]
    misses.sort(key=lambda row: (-row["absolute_error"], row["id"]))
    return {
        "type": "score",
        "total": total_rows,
        "evaluated": len(values),
        "prediction_coverage": len(values) / total_rows if total_rows else 0.0,
        "mae": sum(errors) / len(errors) if errors else None,
        "rmse": math.sqrt(sum(error * error for error in errors) / len(errors)) if errors else None,
        "within_one": sum(error <= 1.0 for error in errors) / len(errors) if errors else None,
        "coverage": coverage,
        "worst_misses": misses[:max_errors],
    }


def evaluate_rows(
    rows: Any,
    variants: dict[str, dict[str, Any]],
    *,
    dataset_role: str,
    threshold: float | None = None,
    thresholds: dict[str, float] | None = None,
    max_errors: int = DEFAULT_MAX_ERRORS,
) -> dict[str, Any]:
    """Compute typed evaluation metrics without allowing holdout threshold fitting."""
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise BatchError("evaluation rows must be a list of objects")
    try:
        jev_judge.validate_request({"state": {}, "questions": variants})
    except (jev_judge.JevError, TypeError, ValueError) as exc:
        raise BatchError(f"invalid variants: {exc}") from None
    frozen_thresholds = _validate_eval_options(
        variants,
        dataset_role=dataset_role,
        threshold=threshold,
        thresholds=thresholds,
        max_errors=max_errors,
    )

    metrics: dict[str, Any] = {}
    variant_count = len(variants)
    for name, question in variants.items():
        kind = question["type"]
        if kind == "noul":
            pairs: list[tuple[str, int, float]] = []
            for row in rows:
                label = _binary_label(
                    _label_for(row, name, variant_count),
                    f"evaluation row {row.get('id', '<unknown>')!r} label for {name!r}",
                )
                if row.get("status") != "ok":
                    continue
                answer = row.get("answers", {}).get(name)
                if not isinstance(answer, dict) or answer.get("type") != "noul":
                    raise BatchError(f"evaluation row {row.get('id', '<unknown>')!r} has invalid answer {name!r}")
                probability = answer.get("noul")
                if (
                    not isinstance(probability, (int, float)) or isinstance(probability, bool)
                    or not math.isfinite(probability) or not 0 <= probability <= 1
                ):
                    raise BatchError(f"evaluation row {row.get('id', '<unknown>')!r} has invalid Noul probability")
                pairs.append((str(row.get("id", "<unknown>")), label, float(probability)))
            metrics[name] = _noul_metrics(
                pairs,
                dataset_role=dataset_role,
                threshold=frozen_thresholds.get(name),
                max_errors=max_errors,
                total_rows=len(rows),
            )
        elif kind == "choice":
            options = list(question["criteria"])
            values: list[tuple[str, str, str, float]] = []
            for row in rows:
                actual = _choice_label(
                    _label_for(row, name, variant_count), options,
                    f"evaluation row {row.get('id', '<unknown>')!r} label for {name!r}",
                )
                if row.get("status") != "ok":
                    continue
                answer = row.get("answers", {}).get(name)
                if not isinstance(answer, dict):
                    raise BatchError(f"evaluation row {row.get('id', '<unknown>')!r} has invalid answer {name!r}")
                predicted = _choice_label(answer.get("choice"), options, "choice prediction")
                confidence = answer.get("confidence")
                if (
                    not isinstance(confidence, (int, float)) or isinstance(confidence, bool)
                    or not math.isfinite(confidence) or not 0 <= confidence <= 1
                ):
                    raise BatchError("choice confidence must be a finite probability")
                values.append((str(row.get("id", "<unknown>")), actual, predicted, float(confidence)))
            metrics[name] = _choice_metrics(
                values, options=options, max_errors=max_errors, total_rows=len(rows)
            )
        else:
            criteria = question["criteria"]
            score_values: list[tuple[str, float, float, float]] = []
            for row in rows:
                actual = _score_label(
                    _label_for(row, name, variant_count), criteria,
                    f"evaluation row {row.get('id', '<unknown>')!r} label for {name!r}",
                )
                if row.get("status") != "ok":
                    continue
                answer = row.get("answers", {}).get(name)
                if not isinstance(answer, dict):
                    raise BatchError(f"evaluation row {row.get('id', '<unknown>')!r} has invalid answer {name!r}")
                predicted = answer.get("score")
                confidence = answer.get("confidence")
                if (
                    not isinstance(predicted, (int, float)) or isinstance(predicted, bool)
                    or not math.isfinite(predicted) or not 0 <= predicted <= len(criteria) - 1
                ):
                    raise BatchError("score prediction is outside its rubric")
                if (
                    not isinstance(confidence, (int, float)) or isinstance(confidence, bool)
                    or not math.isfinite(confidence) or not 0 <= confidence <= 1
                ):
                    raise BatchError("score confidence must be a finite probability")
                score_values.append((
                    str(row.get("id", "<unknown>")), actual, float(predicted), float(confidence)
                ))
            metrics[name] = _score_metrics(
                score_values, max_errors=max_errors, total_rows=len(rows)
            )
    return {"dataset_role": dataset_role, "variants": metrics}


def _compact_answer(answer: Any) -> Any:
    if not isinstance(answer, dict):
        return None
    kind = answer.get("type")
    if kind == "noul":
        return {"type": kind, "noul": answer.get("noul")}
    if kind == "choice":
        return {"type": kind, "choice": answer.get("choice"), "confidence": answer.get("confidence")}
    if kind == "score":
        return {"type": kind, "score": answer.get("score"), "confidence": answer.get("confidence")}
    return None


def _compact_metrics(report: Any) -> Any:
    if not isinstance(report, dict) or not isinstance(report.get("variants"), dict):
        return report
    compact_variants: dict[str, Any] = {}
    for name, metric in report["variants"].items():
        if not isinstance(metric, dict):
            continue
        kind = metric.get("type")
        common = {
            key: metric[key] for key in ("type", "total", "evaluated", "coverage", "prediction_coverage")
            if key in metric
        }
        if kind == "noul":
            for key in ("brier", "ece", "auc", "recommended_threshold", "threshold_evaluation"):
                if key in metric:
                    common[key] = metric[key]
        elif kind == "choice":
            for key in ("accuracy", "macro_f1"):
                if key in metric:
                    common[key] = metric[key]
        elif kind == "score":
            for key in ("mae", "rmse", "within_one"):
                if key in metric:
                    common[key] = metric[key]
        common["worst_miss_ids"] = [
            miss.get("id") for miss in metric.get("worst_misses", [])[:5] if isinstance(miss, dict)
        ]
        compact_variants[name] = common
    return {"dataset_role": report.get("dataset_role"), "variants": compact_variants}


def compact_view(result: dict[str, Any], max_rows: int = DEFAULT_MAX_ROWS) -> dict[str, Any]:
    """Build the bounded, state-free representation intended for stdout."""
    if isinstance(max_rows, bool) or not isinstance(max_rows, int) or max_rows < 0:
        raise BatchError("max_rows must be a non-negative integer")
    row_cap = min(max_rows, MAX_STDOUT_ROWS)
    rows = result.get("rows", [])
    if not isinstance(rows, list):
        raise BatchError("batch result rows must be a list")
    shown_rows = []
    for row in rows[:row_cap]:
        if not isinstance(row, dict):
            continue
        shown: dict[str, Any] = {"id": row.get("id"), "status": row.get("status")}
        if row.get("status") == "ok" and isinstance(row.get("answers"), dict):
            shown["answers"] = {
                name: _compact_answer(answer) for name, answer in row["answers"].items()
            }
        elif row.get("status") == "preview":
            shown["request_hash"] = row.get("request_hash")
            shown["redactions"] = row.get("redactions", 0)
        elif row.get("status") == "error":
            shown["error"] = row.get("error")
        shown_rows.append(shown)

    succeeded = result.get("succeeded")
    if not isinstance(succeeded, int):
        succeeded = sum(
            isinstance(row, dict) and row.get("status") in {"ok", "preview"} for row in rows
        )
    view: dict[str, Any] = {
        "mode": result.get("mode", "map"),
        "network": bool(result.get("network", False)),
        "model": result.get("model"),
        "total": len(rows),
        "succeeded": succeeded,
        "failed": len(rows) - succeeded,
        "rows": shown_rows,
        "omitted_rows": max(len(rows) - len(shown_rows), 0),
    }
    if max_rows > MAX_STDOUT_ROWS:
        view["row_cap"] = MAX_STDOUT_ROWS
    if "evaluation" in result:
        view["evaluation"] = _compact_metrics(result["evaluation"])
    if "output" in result:
        view["output"] = result["output"]
    return view


def write_private_json(path: str | os.PathLike[str], payload: Any) -> None:
    """Atomically write JSON with mode 0600 and never follow an output symlink."""
    destination = Path(path)
    try:
        metadata = os.lstat(destination)
    except FileNotFoundError:
        metadata = None
    except OSError as exc:
        raise BatchError(f"cannot inspect output {str(destination)!r}: {exc}") from None
    if metadata is not None:
        if stat.S_ISLNK(metadata.st_mode):
            raise BatchError("output path must not be a symbolic link")
        if not stat.S_ISREG(metadata.st_mode):
            raise BatchError("output path must be a regular file")
    try:
        encoded = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False
        ) + "\n"
    except (TypeError, ValueError) as exc:
        raise BatchError(f"result is not strict JSON: {exc}") from None

    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent
        )
    except OSError as exc:
        raise BatchError(f"cannot create private output {str(destination)!r}: {exc}") from None
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            descriptor = -1
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        if destination.is_symlink():
            raise BatchError("output path became a symbolic link; refusing replacement")
        os.replace(temporary, destination)
        os.chmod(destination, 0o600, follow_symlinks=False)
    except BatchError:
        raise
    except OSError as exc:
        raise BatchError(f"cannot write private output {str(destination)!r}: {exc}") from None
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _add_common_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("dataset", help="JSON/JSONL dataset with stable id and state fields")
    parser.add_argument("contract", help="typed questions/variants JSON")
    parser.add_argument("--send", action="store_true", help="perform JEV calls; default is local preview")
    parser.add_argument(
        "--model",
        default=os.environ.get("JEV_MODEL", jev_judge.DEFAULT_MODEL),
        help="concrete pinned JEV model version",
    )
    parser.add_argument("--concurrency", type=int, default=DEFAULT_CONCURRENCY)
    parser.add_argument("--max-rows", type=int, default=DEFAULT_MAX_ROWS)
    parser.add_argument("--output", type=Path, help="atomic mode-0600 path for the full redacted result")
    parser.add_argument("--endpoint", default=os.environ.get("JEV_ENDPOINT", jev_judge.DEFAULT_ENDPOINT))
    parser.add_argument("--timeout", type=float, default=15.0)
    parser.add_argument("--retries", type=int, default=4)
    parser.add_argument("--cache-dir", type=Path)
    parser.add_argument("--cache-ttl-seconds", type=float, default=jev_judge.DEFAULT_CACHE_TTL_SECONDS)
    parser.add_argument("--audit", type=Path)
    parser.add_argument("--max-string", type=int, default=6000)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    map_parser = commands.add_parser("map", help="preview or run a typed contract over a dataset")
    _add_common_arguments(map_parser)
    eval_parser = commands.add_parser("eval", help="preview or run and evaluate labeled variants")
    _add_common_arguments(eval_parser)
    eval_parser.add_argument("--dataset-role", choices=("tuning", "holdout"), required=True)
    eval_parser.add_argument(
        "--threshold",
        type=float,
        help="frozen holdout threshold when the contract has exactly one Noul variant",
    )
    eval_parser.add_argument(
        "--thresholds",
        dest="thresholds_json",
        metavar="JSON",
        help="frozen holdout JSON object mapping every Noul variant to its threshold",
    )
    eval_parser.add_argument("--max-errors", type=int, default=DEFAULT_MAX_ERRORS)
    return parser


def _parse_thresholds_json(value: str | None) -> dict[str, float] | None:
    if value is None:
        return None
    try:
        parsed = json.loads(
            value,
            parse_constant=_reject_json_constant,
            object_pairs_hook=_unique_json_object,
        )
    except (json.JSONDecodeError, ValueError) as exc:
        raise BatchError(f"--thresholds must be a strict JSON object: {exc}") from None
    if not isinstance(parsed, dict):
        raise BatchError("--thresholds must be a JSON object keyed by Noul variant")
    return parsed


def _validate_cli_options(args: argparse.Namespace) -> None:
    validate_model(args.model)
    _validate_runtime_options(args.concurrency, args.max_string)
    _validate_call_options(
        timeout=args.timeout,
        retries=args.retries,
        cache_ttl_seconds=args.cache_ttl_seconds,
        send=args.send,
    )
    if args.max_rows < 0:
        raise BatchError("max_rows must be non-negative")


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        _validate_cli_options(args)
        items = load_items(args.dataset)
        wrapper_name = "questions" if args.command == "map" else "variants"
        questions = load_questions(args.contract, wrapper_name)
        thresholds: dict[str, float] | None = None

        if args.command == "eval":
            thresholds = _parse_thresholds_json(args.thresholds_json)
            _validate_eval_labels(items, questions)
            _validate_eval_options(
                questions,
                dataset_role=args.dataset_role,
                threshold=args.threshold,
                thresholds=thresholds,
                max_errors=args.max_errors,
            )

        result = run_batch(
            items,
            questions,
            send=args.send,
            model=args.model,
            concurrency=args.concurrency,
            endpoint=args.endpoint,
            timeout=args.timeout,
            retries=args.retries,
            cache_dir=args.cache_dir,
            audit=args.audit,
            cache_ttl_seconds=args.cache_ttl_seconds,
            max_string=args.max_string,
        )
        result["mode"] = args.command
        if args.command == "eval":
            result["dataset_role"] = args.dataset_role
            if args.send:
                result["evaluation"] = evaluate_rows(
                    result["rows"],
                    questions,
                    dataset_role=args.dataset_role,
                    threshold=args.threshold,
                    thresholds=thresholds,
                    max_errors=args.max_errors,
                )
            else:
                result["evaluation"] = {
                    "dataset_role": args.dataset_role,
                    "status": "preview_only",
                    "variants": {},
                }
        if args.output is not None:
            write_private_json(args.output, result)
            result["output"] = str(args.output)
        print(json.dumps(compact_view(result, args.max_rows), ensure_ascii=False, indent=2, allow_nan=False))
        return 0
    except (BatchError, jev_judge.JevError, OSError, ValueError, TypeError) as exc:
        clean_message, _ = jev_judge.redact(str(exc), max_string=6000)
        print(json.dumps({"ok": False, "error": clean_message}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

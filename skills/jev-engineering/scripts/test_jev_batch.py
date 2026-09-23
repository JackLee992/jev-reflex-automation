#!/usr/bin/env python3
"""Offline contracts for the stdlib-only JEV batch mapper and evaluator."""
from __future__ import annotations

import contextlib
import io
import json
import math
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest import mock

import jev_batch as batch


NOUL = {
    "signal": {
        "type": "noul",
        "instructions": "Is this item actionable based only on the supplied state?",
    }
}

MULTI_NOUL = {
    "relevant": {
        "type": "noul",
        "instructions": "Is this item relevant?",
    },
    "urgent": {
        "type": "noul",
        "instructions": "Is this item urgent?",
    },
}

CHOICE = {
    "owner": {
        "type": "choice",
        "instructions": "Which team owns this item?",
        "criteria": {"a": "Team A", "b": "Team B"},
    }
}

SCORE = {
    "severity": {
        "type": "score",
        "instructions": "How severe is this item?",
        "criteria": ["low", "medium", "high"],
    }
}


def response(model: str, answers: dict[str, object]) -> dict[str, object]:
    return {
        "response": {
            "model": model,
            "answers": answers,
            "usage": {"input_tokens": 10, "output_tokens": 0},
        },
        "meta": {"cached": False},
    }


class DatasetTests(unittest.TestCase):
    def test_parser_honors_pinned_model_environment_default(self):
        with mock.patch.dict(os.environ, {"JEV_MODEL": "jev-9.8.7"}):
            args = batch.build_parser().parse_args(["map", "items.json", "questions.json"])
        self.assertEqual(args.model, "jev-9.8.7")

    def test_loads_json_and_jsonl_with_unique_stable_ids(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            json_path = root / "items.json"
            json_path.write_text(
                json.dumps({"items": [{"id": "a-1", "state": {"text": "one"}}]}),
                encoding="utf-8",
            )
            jsonl_path = root / "items.jsonl"
            jsonl_path.write_text(
                '\n'.join(
                    json.dumps(item)
                    for item in (
                        {"id": "b-1", "state": {"text": "two"}},
                        {"id": "b-2", "state": {"text": "three"}, "label": True},
                    )
                ),
                encoding="utf-8",
            )

            self.assertEqual([item["id"] for item in batch.load_items(str(json_path))], ["a-1"])
            self.assertEqual(
                [item["id"] for item in batch.load_items(str(jsonl_path))],
                ["b-1", "b-2"],
            )

    def test_rejects_missing_duplicate_or_free_text_ids(self):
        invalid = (
            [{"state": {}}],
            [{"id": "same", "state": {}}, {"id": "same", "state": {}}],
            [{"id": "contains a secret-looking sentence", "state": {}}],
        )
        for items in invalid:
            with self.subTest(items=items), self.assertRaises(batch.BatchError):
                batch.validate_items(items)

    def test_questions_accept_direct_or_named_wrapper_objects(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            direct = root / "direct.json"
            wrapped = root / "wrapped.json"
            direct.write_text(json.dumps(NOUL), encoding="utf-8")
            wrapped.write_text(json.dumps({"variants": NOUL}), encoding="utf-8")
            self.assertEqual(batch.load_questions(str(direct), "questions"), NOUL)
            self.assertEqual(batch.load_questions(str(wrapped), "variants"), NOUL)

    def test_loader_rejects_duplicate_json_keys(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "items.json"
            path.write_text('[{"id":"first","id":"second","state":{}}]', encoding="utf-8")
            with self.assertRaises(batch.BatchError):
                batch.load_items(str(path))

    def test_direct_contract_can_use_the_same_name_as_the_wrapper(self):
        direct = {
            "questions": {
                "type": "noul",
                "instructions": "Is this a direct question named questions?",
            }
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "questions.json"
            path.write_text(json.dumps(direct), encoding="utf-8")
            self.assertEqual(batch.load_questions(str(path), "questions"), direct)


class BatchExecutionTests(unittest.TestCase):
    def test_preview_redacts_and_never_calls_the_network_runner(self):
        items = [{"id": "one", "state": {"authorization": "Bearer top-secret", "ok": "yes"}}]

        def forbidden(*_args, **_kwargs):
            raise AssertionError("preview called the network runner")

        result = batch.run_batch(
            items,
            NOUL,
            send=False,
            model="jev-1.13.0",
            runner=forbidden,
        )

        self.assertEqual(result["rows"][0]["status"], "preview")
        self.assertEqual(
            result["rows"][0]["request"]["state"]["authorization"],
            "<redacted-sensitive-field>",
        )
        self.assertEqual(result["rows"][0]["request"]["model"], "jev-1.13.0")

    def test_send_limits_concurrency_preserves_order_and_isolates_failures(self):
        lock = threading.Lock()
        active = 0
        maximum = 0

        def runner(payload, *, model, **_kwargs):
            nonlocal active, maximum
            item_id = payload["state"]["id"]
            with lock:
                active += 1
                maximum = max(maximum, active)
            try:
                time.sleep(0.02)
                if item_id == "i2":
                    raise RuntimeError("fixture failure")
                return response(model, {"signal": {"type": "noul", "noul": int(item_id[-1]) / 10}})
            finally:
                with lock:
                    active -= 1

        items = [{"id": f"i{i}", "state": {"id": f"i{i}"}} for i in range(6)]
        result = batch.run_batch(
            items,
            NOUL,
            send=True,
            model="jev-1.13.0",
            concurrency=2,
            runner=runner,
        )

        self.assertEqual([row["id"] for row in result["rows"]], [f"i{i}" for i in range(6)])
        self.assertEqual(result["rows"][2]["status"], "error")
        self.assertEqual(sum(row["status"] == "ok" for row in result["rows"]), 5)
        self.assertGreaterEqual(maximum, 2)
        self.assertLessEqual(maximum, 2)

    def test_send_revalidates_an_injected_runner_response(self):
        def malformed(_payload, *, model, **_kwargs):
            return response(model, {"signal": {"type": "noul", "noul": 7}})

        result = batch.run_batch(
            [{"id": "bad", "state": {"value": 1}}],
            NOUL,
            send=True,
            model="jev-1.13.0",
            runner=malformed,
        )
        self.assertEqual(result["rows"][0]["status"], "error")
        self.assertNotIn("answers", result["rows"][0])

    def test_model_must_be_a_concrete_version(self):
        for model in ("jev-latest", "latest", "jev"):
            with self.subTest(model=model), self.assertRaises(batch.BatchError):
                batch.validate_model(model)

    def test_compact_view_caps_rows_and_never_includes_state(self):
        result = {
            "mode": "map",
            "network": True,
            "model": "jev-1.13.0",
            "rows": [
                {
                    "id": f"i{i}",
                    "status": "ok",
                    "state": {"private": "must stay out"},
                    "answers": {"signal": {"type": "noul", "noul": i / 10}},
                }
                for i in range(5)
            ],
        }
        view = batch.compact_view(result, max_rows=2)
        encoded = json.dumps(view)
        self.assertEqual(len(view["rows"]), 2)
        self.assertEqual(view["omitted_rows"], 3)
        self.assertNotIn("private", encoded)


class MetricsTests(unittest.TestCase):
    def test_noul_tuning_reports_sweep_calibration_auc_and_worst_misses(self):
        rows = [
            {"id": "a", "status": "ok", "label": False, "answers": {"signal": {"type": "noul", "noul": 0.1}}},
            {"id": "b", "status": "ok", "label": False, "answers": {"signal": {"type": "noul", "noul": 0.2}}},
            {"id": "c", "status": "ok", "label": True, "answers": {"signal": {"type": "noul", "noul": 0.8}}},
            {"id": "d", "status": "ok", "label": True, "answers": {"signal": {"type": "noul", "noul": 0.9}}},
            {"id": "miss", "status": "ok", "label": False, "answers": {"signal": {"type": "noul", "noul": 0.95}}},
        ]
        report = batch.evaluate_rows(rows, NOUL, dataset_role="tuning", threshold=None, max_errors=3)
        metric = report["variants"]["signal"]
        self.assertEqual(metric["type"], "noul")
        self.assertIn("threshold_sweep", metric)
        self.assertIn("recommended_threshold", metric)
        self.assertIn("brier", metric)
        self.assertIn("ece", metric)
        self.assertIn("auc", metric)
        self.assertEqual(metric["worst_misses"][0]["id"], "miss")
        self.assertTrue(all(key in metric["threshold_sweep"][0] for key in ("precision", "recall", "f1", "accuracy")))

    def test_noul_holdout_requires_and_only_evaluates_the_supplied_threshold(self):
        rows = [
            {"id": "a", "status": "ok", "label": False, "answers": {"signal": {"type": "noul", "noul": 0.4}}},
            {"id": "b", "status": "ok", "label": True, "answers": {"signal": {"type": "noul", "noul": 0.8}}},
        ]
        with self.assertRaises(batch.BatchError):
            batch.evaluate_rows(rows, NOUL, dataset_role="holdout", threshold=None)

        report = batch.evaluate_rows(rows, NOUL, dataset_role="holdout", threshold=0.7)
        metric = report["variants"]["signal"]
        self.assertEqual(metric["threshold_evaluation"]["threshold"], 0.7)
        self.assertNotIn("threshold_sweep", metric)
        self.assertNotIn("recommended_threshold", metric)

    def test_multi_noul_holdout_requires_exact_per_variant_thresholds(self):
        rows = [{
            "id": "a",
            "status": "ok",
            "label": {"relevant": True, "urgent": False},
            "answers": {
                "relevant": {"type": "noul", "noul": 0.6},
                "urgent": {"type": "noul", "noul": 0.6},
            },
        }]

        with self.assertRaisesRegex(batch.BatchError, "multiple Noul"):
            batch.evaluate_rows(
                rows,
                MULTI_NOUL,
                dataset_role="holdout",
                threshold=0.5,
            )
        for thresholds in (
            {"relevant": 0.5},
            {"relevant": 0.5, "urgent": 0.7, "extra": 0.2},
        ):
            with self.subTest(thresholds=thresholds), self.assertRaises(batch.BatchError):
                batch.evaluate_rows(
                    rows,
                    MULTI_NOUL,
                    dataset_role="holdout",
                    thresholds=thresholds,
                )

        report = batch.evaluate_rows(
            rows,
            MULTI_NOUL,
            dataset_role="holdout",
            thresholds={"relevant": 0.5, "urgent": 0.7},
        )
        self.assertEqual(
            report["variants"]["relevant"]["threshold_evaluation"]["threshold"],
            0.5,
        )
        self.assertEqual(
            report["variants"]["urgent"]["threshold_evaluation"]["threshold"],
            0.7,
        )

    def test_frozen_threshold_contract_rejects_ambiguity_and_invalid_values(self):
        rows = [{
            "id": "a",
            "status": "ok",
            "label": True,
            "answers": {"signal": {"type": "noul", "noul": 0.8}},
        }]
        invalid_calls = (
            {"dataset_role": "tuning", "thresholds": {"signal": 0.5}},
            {"dataset_role": "holdout", "threshold": 0.5,
             "thresholds": {"signal": 0.5}},
            {"dataset_role": "holdout", "thresholds": {"signal": True}},
            {"dataset_role": "holdout", "thresholds": {"signal": math.nan}},
            {"dataset_role": "holdout", "thresholds": {"signal": 1.1}},
        )
        for kwargs in invalid_calls:
            with self.subTest(kwargs=kwargs), self.assertRaises(batch.BatchError):
                batch.evaluate_rows(rows, NOUL, **kwargs)

        choice_rows = [{
            "id": "c",
            "status": "ok",
            "label": "a",
            "answers": {"owner": {
                "type": "choice", "choice": "a", "confidence": 1.0,
                "probabilities": {"a": 1.0, "b": 0.0},
            }},
        }]
        for kwargs in (
            {"threshold": 0.5},
            {"thresholds": {}},
        ):
            with self.subTest(no_noul=kwargs), self.assertRaises(batch.BatchError):
                batch.evaluate_rows(choice_rows, CHOICE, dataset_role="holdout", **kwargs)

    def test_single_noul_accepts_scalar_or_exact_threshold_mapping(self):
        rows = [{
            "id": "a",
            "status": "ok",
            "label": True,
            "answers": {"signal": {"type": "noul", "noul": 0.8}},
        }]
        scalar = batch.evaluate_rows(
            rows, NOUL, dataset_role="holdout", threshold=0.7
        )
        mapped = batch.evaluate_rows(
            rows,
            NOUL,
            dataset_role="holdout",
            thresholds={"signal": 0.6},
        )
        self.assertEqual(
            scalar["variants"]["signal"]["threshold_evaluation"]["threshold"],
            0.7,
        )
        self.assertEqual(
            mapped["variants"]["signal"]["threshold_evaluation"]["threshold"],
            0.6,
        )

    def test_choice_reports_accuracy_confusion_macro_f1_coverage_and_misses(self):
        answers = [
            ("x", "a", 0.9, "a"),
            ("y", "a", 0.7, "b"),
            ("z", "b", 0.8, "b"),
        ]
        rows = []
        for item_id, choice, confidence, label in answers:
            probabilities = {"a": confidence if choice == "a" else 1 - confidence,
                             "b": confidence if choice == "b" else 1 - confidence}
            rows.append({
                "id": item_id,
                "status": "ok",
                "label": label,
                "answers": {"owner": {"type": "choice", "choice": choice,
                                         "confidence": confidence, "probabilities": probabilities}},
            })
        metric = batch.evaluate_rows(rows, CHOICE, dataset_role="holdout")["variants"]["owner"]
        self.assertAlmostEqual(metric["accuracy"], 2 / 3)
        self.assertEqual(metric["confusion"]["b"]["a"], 1)
        self.assertAlmostEqual(metric["macro_f1"], 2 / 3)
        high = next(row for row in metric["coverage"] if row["min_confidence"] == 0.8)
        self.assertEqual(high["accuracy"], 1.0)
        self.assertEqual(metric["worst_misses"][0]["id"], "y")

    def test_score_reports_mae_rmse_within_one_coverage_and_misses(self):
        rows = [
            {"id": "x", "status": "ok", "label": 0, "answers": {"severity": {
                "type": "score", "score": 0.2, "confidence": 0.9,
                "legend": {"0": "low", "1": "medium", "2": "high"},
                "probabilities": {"0": 0.8, "1": 0.2, "2": 0.0}}}},
            {"id": "y", "status": "ok", "label": "high", "answers": {"severity": {
                "type": "score", "score": 1.4, "confidence": 0.7,
                "legend": {"0": "low", "1": "medium", "2": "high"},
                "probabilities": {"0": 0.0, "1": 0.6, "2": 0.4}}}},
        ]
        metric = batch.evaluate_rows(rows, SCORE, dataset_role="holdout")["variants"]["severity"]
        self.assertAlmostEqual(metric["mae"], 0.4)
        self.assertAlmostEqual(metric["rmse"], math.sqrt(0.2))
        self.assertEqual(metric["within_one"], 1.0)
        high = next(row for row in metric["coverage"] if row["min_confidence"] == 0.8)
        self.assertEqual(high["coverage"], 0.5)
        self.assertEqual(metric["worst_misses"][0]["id"], "y")


class OutputAndCliTests(unittest.TestCase):
    def test_private_json_write_is_atomic_mode_600_and_rejects_symlinks(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output = root / "result.json"
            batch.write_private_json(output, {"ok": True})
            self.assertEqual(json.loads(output.read_text()), {"ok": True})
            self.assertEqual(output.stat().st_mode & 0o777, 0o600)

            target = root / "target.json"
            target.write_text("unchanged", encoding="utf-8")
            link = root / "linked.json"
            link.symlink_to(target)
            with self.assertRaises(batch.BatchError):
                batch.write_private_json(link, {"ok": False})
            self.assertEqual(target.read_text(encoding="utf-8"), "unchanged")

    def test_cli_defaults_to_preview_and_emits_capped_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data = root / "items.json"
            questions = root / "questions.json"
            data.write_text(json.dumps([
                {"id": "a", "state": {"text": "one"}},
                {"id": "b", "state": {"text": "two"}},
            ]), encoding="utf-8")
            questions.write_text(json.dumps(NOUL), encoding="utf-8")
            stdout = io.StringIO()
            with mock.patch.object(batch.jev_judge, "run_request", side_effect=AssertionError("network")), \
                 contextlib.redirect_stdout(stdout):
                code = batch.main(["map", str(data), str(questions), "--max-rows", "1"])
            shown = json.loads(stdout.getvalue())
            self.assertEqual(code, 0)
            self.assertFalse(shown["network"])
            self.assertEqual(len(shown["rows"]), 1)
            self.assertEqual(shown["omitted_rows"], 1)

    def test_cli_holdout_with_noul_requires_threshold_before_network(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data = root / "items.json"
            variants = root / "variants.json"
            data.write_text(json.dumps([{"id": "a", "state": {}, "label": True}]), encoding="utf-8")
            variants.write_text(json.dumps(NOUL), encoding="utf-8")
            stderr = io.StringIO()
            with mock.patch.object(batch.jev_judge, "run_request", side_effect=AssertionError("network")), \
                 contextlib.redirect_stderr(stderr):
                code = batch.main([
                    "eval", str(data), str(variants), "--dataset-role", "holdout", "--send"
                ])
            self.assertEqual(code, 2)
            self.assertIn("--threshold", stderr.getvalue())

    def test_cli_multi_noul_refuses_scalar_threshold_before_network(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data = root / "items.json"
            variants = root / "variants.json"
            data.write_text(json.dumps([{
                "id": "a",
                "state": {},
                "label": {"relevant": True, "urgent": False},
            }]), encoding="utf-8")
            variants.write_text(json.dumps(MULTI_NOUL), encoding="utf-8")
            stderr = io.StringIO()
            with mock.patch.object(batch.jev_judge, "run_request") as runner, \
                 contextlib.redirect_stderr(stderr):
                code = batch.main([
                    "eval", str(data), str(variants),
                    "--dataset-role", "holdout", "--threshold", "0.5", "--send",
                ])
            self.assertEqual(code, 2)
            runner.assert_not_called()
            self.assertIn("--thresholds", stderr.getvalue())

    def test_cli_thresholds_json_applies_each_frozen_holdout_threshold(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data = root / "items.json"
            variants = root / "variants.json"
            data.write_text(json.dumps([{
                "id": "a",
                "state": {"case": "a"},
                "label": {"relevant": True, "urgent": False},
            }]), encoding="utf-8")
            variants.write_text(json.dumps(MULTI_NOUL), encoding="utf-8")
            stdout = io.StringIO()
            answer = response("jev-1.13.0", {
                "relevant": {"type": "noul", "noul": 0.6},
                "urgent": {"type": "noul", "noul": 0.6},
            })
            with mock.patch.object(batch.jev_judge, "run_request", return_value=answer), \
                 contextlib.redirect_stdout(stdout):
                code = batch.main([
                    "eval", str(data), str(variants), "--dataset-role", "holdout",
                    "--thresholds", '{"relevant":0.5,"urgent":0.7}', "--send",
                ])
            shown = json.loads(stdout.getvalue())
            self.assertEqual(code, 0)
            metrics = shown["evaluation"]["variants"]
            self.assertEqual(metrics["relevant"]["threshold_evaluation"]["threshold"], 0.5)
            self.assertEqual(metrics["urgent"]["threshold_evaluation"]["threshold"], 0.7)

    def test_eval_help_documents_threshold_mapping(self):
        help_text = batch.build_parser()._subparsers._group_actions[0].choices["eval"].format_help()
        self.assertIn("--thresholds", help_text)
        self.assertIn("variant", help_text.lower())


if __name__ == "__main__":
    unittest.main()

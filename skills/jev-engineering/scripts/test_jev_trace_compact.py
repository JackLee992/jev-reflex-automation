#!/usr/bin/env python3
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import jev_judge
import jev_trace_compact as trace


POLICY = trace.CompactionPolicy("trace-v2", 0.90, 0.95, 12)
WIDE = trace.RequestBudget(200_000, 200_000, 256)


def anchor() -> dict[str, object]:
    return {"id": "user0", "kind": "user_request", "content": "fix the failing build"}


def pair(number: int, result: str | None = None) -> list[dict[str, object]]:
    call_id = f"call{number}"
    return [
        {"id": f"call_item{number}", "kind": "tool_call", "call_id": call_id,
         "content": f"run tool {number}", "complete": True},
        {"id": f"result_item{number}", "kind": "tool_result", "call_id": call_id,
         "content": result if result is not None else f"result {number}", "complete": True},
    ]


def answer(choice: str, confidence: float = 0.99) -> dict[str, object]:
    probabilities = {name: 0.005 for name in trace.ACTIONS}
    probabilities[choice] = 0.99
    return {"type": "choice", "choice": choice, "confidence": confidence,
            "probabilities": probabilities}


def response(batch: trace.PreparedBatch, choices: list[str]) -> dict[str, object]:
    return {"model": batch.body["model"], "usage": {"input_tokens": 5, "output_tokens": 2},
            "answers": {qid: answer(choice)
                        for qid, choice in zip(batch.body["questions"], choices, strict=True)}}


def trusted_envelope(batch: trace.PreparedBatch, choices: list[str]) -> dict[str, object]:
    return {
        "response": response(batch, choices),
        "meta": {
            "schema_version": "1",
            "kind": "jev_judgment",
            "status": "ok",
            "judgment_id": "jdg_0123456789abcdef0123456789abcdef",
            "request_hash": batch.request_hash,
            "question_contract_hash": jev_judge.question_contract_hash(batch.body),
            "requested_model": batch.body["model"],
            "response_model": batch.body["model"],
            "state_projection_version": trace.PROJECTION_VERSION,
            "endpoint": jev_judge.DEFAULT_ENDPOINT,
            "service_identity": "typesafe_official",
            "cached": False,
            "ts": "2026-03-01T00:00:00+00:00",
        },
    }


class TraceCompactionTests(unittest.TestCase):
    def prepared(self, items, *, recent_items=0, budget=WIDE):
        clean, redactions, _ = trace.redact_items(items)
        pairs, reasons = trace.analyze_trace(clean, recent_items=recent_items)
        batches = trace.fit_batches(clean, pairs, model=jev_judge.DEFAULT_MODEL, budget=budget)
        return clean, redactions, pairs, reasons, batches

    def test_pairing_only_complete_unique_ordered_pairs_are_candidates(self) -> None:
        items = [anchor(), *pair(1),
                 {"id": "orphan", "kind": "tool_result", "call_id": "call2", "content": "orphan", "complete": True},
                 {"id": "dup1", "kind": "tool_call", "call_id": "call3", "content": "a", "complete": True},
                 {"id": "dup2", "kind": "tool_call", "call_id": "call3", "content": "b", "complete": True},
                 {"id": "dup3", "kind": "tool_result", "call_id": "call3", "content": "c", "complete": True},
                 {"id": "inc1", "kind": "tool_call", "call_id": "call4", "content": "a", "complete": True},
                 {"id": "inc2", "kind": "tool_result", "call_id": "call4", "content": "b", "complete": False},
                 {"id": "note", "kind": "assistant_note", "content": "generic"}]
        clean, _, pairs, reasons, _ = self.prepared(items)
        self.assertEqual([value.call_id for value in pairs], ["call1"])
        self.assertIn("incomplete_tool_pair", reasons[3])
        self.assertIn("duplicate_tool_pair", reasons[4])
        self.assertIn("incomplete_tool_pair", reasons[8])
        self.assertIn("non_tool_item", reasons[9])
        preview = trace.preview(items, model=jev_judge.DEFAULT_MODEL, recent_items=0, budget=WIDE)
        self.assertEqual(preview["compacted_items"], clean)

    def test_first_item_and_explicit_recent_tail_pin_whole_pairs(self) -> None:
        items = [*pair(0), *pair(1), *pair(2)]
        clean, _, pairs, reasons, _ = self.prepared(items, recent_items=1)
        self.assertEqual([value.call_id for value in pairs], ["call1"])
        self.assertIn("first_item", reasons[0])
        self.assertIn("pair_member_protected", reasons[1])
        self.assertIn("recent_tail", reasons[5])
        self.assertIn("pair_member_protected", reasons[4])
        self.assertEqual(clean[0]["content"], "run tool 0")

    def test_textual_error_signals_protect_whole_pairs(self) -> None:
        for content in (
            "Traceback: worker stopped",
            "ERROR while building",
            "panic: invalid state",
            "request timeout after 30s",
        ):
            with self.subTest(content=content):
                clean, _, pairs, reasons, _ = self.prepared(
                    [anchor(), *pair(1, content)],
                )
                self.assertEqual(pairs, [])
                self.assertIn("unresolved_error", reasons[2])
                self.assertIn("pair_member_protected", reasons[1])
                self.assertIn("pair_member_protected", reasons[2])
                self.assertEqual(clean[2]["content"], content)

    def test_textual_error_requires_explicit_structured_resolution(self) -> None:
        unstructured = [anchor(), *pair(1, "ERROR occurred but the text says resolved and fixed")]
        _, _, pairs, reasons, _ = self.prepared(unstructured)
        self.assertEqual(pairs, [])
        self.assertIn("unresolved_error", reasons[2])

        for marker in ({"resolved": True}, {"status": "resolved"}, {"status": "HANDLED"}):
            with self.subTest(marker=marker):
                items = [anchor(), *pair(1, "Traceback: historical failure")]
                items[2].update(marker)
                _, _, pairs, reasons, _ = self.prepared(items)
                self.assertEqual([value.call_id for value in pairs], ["call1"])
                self.assertNotIn("unresolved_error", reasons[2])

    def test_compaction_policy_rejects_drop_threshold_below_truncate(self) -> None:
        with self.assertRaisesRegex(
            jev_judge.JevError,
            "drop_min_confidence must be greater than or equal to truncate_min_confidence",
        ):
            trace.CompactionPolicy("trace-v2", 0.95, 0.90, 12).validate()

    def test_compaction_policy_allows_equal_lossy_thresholds(self) -> None:
        trace.CompactionPolicy("trace-v2", 0.90, 0.90, 12).validate()

    def test_pair_actions_are_atomic_and_canonical_order_is_preserved(self) -> None:
        items = [anchor(), *pair(1, "abcdefghijklmnopqrstuv"),
                 {"id": "middle", "kind": "assistant_note", "content": "keep middle"},
                 *pair(2, "drop me")]
        clean, redactions, pairs, reasons, batches = self.prepared(items)
        self.assertEqual(len(batches), 1)
        result = trace.apply_batch_responses(
            clean, pairs, reasons, batches,
            [trusted_envelope(batches[0], ["truncate", "drop"])],
            POLICY, redactions=redactions, budget=WIDE, recent_items=0)
        ids = [item["id"] for item in result["compacted_items"]]
        self.assertEqual(ids, ["user0", "call_item1", "result_item1", "middle"])
        self.assertEqual(result["compacted_items"][1]["content"], clean[1]["content"])
        self.assertIn("<jev-truncated sha256:", result["compacted_items"][2]["content"])
        pair_decisions = [d for d in result["decisions"] if d["unit"] == "tool_pair"]
        self.assertEqual([d["applied_action"] for d in pair_decisions],
                         ["truncate_result", "drop_pair"])
        self.assertEqual([ref["input_index"] for ref in result["canonical_refs"]],
                         list(range(len(items))))
        self.assertIsNone(result["canonical_refs"][-1]["output_index"])

    def test_low_confidence_lossy_choice_keeps_complete_pair(self) -> None:
        items = [anchor(), *pair(1)]
        clean, redactions, pairs, reasons, batches = self.prepared(items)
        low = trusted_envelope(batches[0], ["drop"])
        only = next(iter(low["response"]["answers"].values()))
        only["confidence"] = 0.2
        result = trace.apply_batch_responses(clean, pairs, reasons, batches, [low], POLICY,
            redactions=redactions, budget=WIDE, recent_items=0)
        self.assertEqual(result["compacted_items"], clean)
        decision = next(d for d in result["decisions"] if d["unit"] == "tool_pair")
        self.assertEqual(decision["applied_action"], "keep_pair")

    def test_unauthenticated_cache_can_never_drive_lossy_compaction(self) -> None:
        items = [anchor(), *pair(1, "drop me")]
        clean, redactions, pairs, reasons, batches = self.prepared(items)
        batch = batches[0]
        cached = trusted_envelope(batch, ["drop"])
        cached["meta"]["cached"] = True
        result = trace.apply_batch_responses(
            clean,
            pairs,
            reasons,
            batches,
            [cached],
            POLICY,
            redactions=redactions,
            budget=WIDE,
            recent_items=0,
        )
        self.assertEqual(result["judgment"]["status"], "fallback_keep_all")
        self.assertEqual(result["compacted_items"], clean)

    def test_raw_or_empty_meta_response_cannot_drive_lossy_compaction(self) -> None:
        items = [anchor(), *pair(1, "drop me")]
        clean, redactions, pairs, reasons, batches = self.prepared(items)
        body = response(batches[0], ["drop"])
        for entry in (body, {"response": body, "meta": {}}):
            with self.subTest(entry=entry):
                result = trace.apply_batch_responses(
                    clean, pairs, reasons, batches, [entry], POLICY,
                    redactions=redactions, budget=WIDE, recent_items=0,
                )
                self.assertEqual(result["judgment"]["status"], "fallback_keep_all")
                self.assertEqual(result["compacted_items"], clean)

    def test_lossy_envelope_metadata_is_bound_to_request_and_official_service(self) -> None:
        items = [anchor(), *pair(1, "drop me")]
        clean, redactions, pairs, reasons, batches = self.prepared(items)
        invalid_values = {
            "request_hash": "0" * 64,
            "question_contract_hash": "1" * 64,
            "requested_model": "jev-1.12.0",
            "response_model": "jev-1.12.0",
            "state_projection_version": "wrong-projection",
            "status": "error",
            "endpoint": "https://example.invalid/v1/systemone",
            "service_identity": "localhost_test",
        }
        for field, invalid in invalid_values.items():
            with self.subTest(field=field):
                entry = trusted_envelope(batches[0], ["drop"])
                entry["meta"][field] = invalid
                result = trace.apply_batch_responses(
                    clean, pairs, reasons, batches, [entry], POLICY,
                    redactions=redactions, budget=WIDE, recent_items=0,
                )
                self.assertEqual(result["judgment"]["status"], "fallback_keep_all")
                self.assertEqual(result["compacted_items"], clean)

    def test_budget_batches_complete_pairs_and_records_total_request_size(self) -> None:
        items = [anchor(), *pair(1, "x" * 1000), *pair(2, "y" * 1000), *pair(3, "z" * 1000)]
        budget = trace.RequestBudget(200_000, 200_000, 128, max_questions=1)
        _, _, pairs, _, batches = self.prepared(items, budget=budget)
        self.assertEqual(len(pairs), 3)
        self.assertEqual(len(batches), 3)
        for batch in batches:
            self.assertLessEqual(batch.request_bytes, budget.max_bytes)
            self.assertLessEqual(batch.estimated_tokens, budget.max_tokens)
            self.assertEqual(len(batch.mapping), 1)

    def test_progressive_fit_uses_metadata_without_changing_canonical(self) -> None:
        items = [anchor(), *pair(1, "x" * 20_000)]
        clean, _, pairs, _, _ = self.prepared(items)
        # Find the metadata request size, then permit exactly that projection.
        metadata = trace._prepare(clean, [trace.Projection(pairs[0], "metadata", 0)],
                                  jev_judge.DEFAULT_MODEL, WIDE)
        budget = trace.RequestBudget(metadata.request_bytes, metadata.estimated_tokens, 512)
        batches = trace.fit_batches(clean, pairs, model=jev_judge.DEFAULT_MODEL, budget=budget)
        projection = batches[0].body["state"]["ordered_tool_pairs"][0]["result"]["projection"]
        self.assertEqual(projection["mode"], "error_boundary_metadata_only")
        preview = trace.preview(items, model=jev_judge.DEFAULT_MODEL, recent_items=0, budget=budget)
        self.assertEqual(preview["compacted_items"], clean)

    def test_fit_failure_returns_keep_all_without_network(self) -> None:
        items = [anchor(), *pair(1, "x" * 500)]
        tiny = trace.RequestBudget(10, 10, 8)
        with mock.patch.object(trace.jev_judge, "run_request") as run:
            result = trace.compact_with_jev(items, policy=POLICY, model=jev_judge.DEFAULT_MODEL,
                                            recent_items=0, budget=tiny)
        self.assertEqual(run.call_count, 0)
        self.assertEqual(result["judgment"]["status"], "fit_failed_keep_all")
        clean, _, _ = trace.redact_items(items)
        self.assertEqual(result["compacted_items"], clean)

    def test_any_batch_failure_keeps_entire_trace(self) -> None:
        items = [anchor(), *pair(1), *pair(2)]
        budget = trace.RequestBudget(200_000, 200_000, 128, max_questions=1)
        clean, _, _, _, batches = self.prepared(items, budget=budget)
        calls = 0

        def fake_run(request, **kwargs):
            nonlocal calls
            batch = batches[calls]
            calls += 1
            if calls == 2:
                raise jev_judge.JevError("second batch failed")
            return trusted_envelope(batch, ["drop"])

        with mock.patch.object(trace.jev_judge, "run_request", side_effect=fake_run):
            result = trace.compact_with_jev(items, policy=POLICY, model=jev_judge.DEFAULT_MODEL,
                                            recent_items=0, budget=budget)
        self.assertEqual(result["judgment"]["status"], "fallback_keep_all")
        self.assertEqual(result["judgment"]["failed_batch"], 1)
        self.assertEqual(result["compacted_items"], clean)

    def test_redaction_happens_before_projection_and_keep_is_exact(self) -> None:
        secret = "apikey_abcdefghijklmnopqrstuvwxyz0123456789_ABCD"
        items = [anchor(), *pair(1, f"Authorization: Bearer {secret} user@example.com")]
        result = trace.preview(items, model=jev_judge.DEFAULT_MODEL, recent_items=0, budget=WIDE)
        encoded = json.dumps(result)
        self.assertNotIn(secret, encoded)
        self.assertNotIn("user@example.com", encoded)
        clean, _, _ = trace.redact_items(items)
        self.assertEqual(result["compacted_items"], clean)

    def test_reads_json_array_and_jsonl(self) -> None:
        items = [anchor(), *pair(1)]
        with tempfile.TemporaryDirectory() as tmp:
            array_path, lines_path = Path(tmp) / "trace.json", Path(tmp) / "trace.jsonl"
            array_path.write_text(json.dumps(items), encoding="utf-8")
            lines_path.write_text("\n".join(json.dumps(item) for item in items), encoding="utf-8")
            self.assertEqual(trace.load_items(str(array_path)), items)
            self.assertEqual(trace.load_items(str(lines_path)), items)

    def test_missing_complete_and_non_strict_json_are_fail_closed(self) -> None:
        items = [anchor(), *pair(1)]
        del items[1]["complete"]
        clean, _, pairs, reasons, _ = self.prepared(items)
        self.assertEqual(pairs, [])
        self.assertIn("incomplete_tool_pair", reasons[1])
        self.assertEqual(clean[1]["content"], "run tool 1")

        with tempfile.TemporaryDirectory() as tmp:
            duplicate = Path(tmp) / "duplicate.jsonl"
            duplicate.write_text(
                '{"id":"a","id":"b","kind":"user_request","content":"x"}\n',
                encoding="utf-8",
            )
            with self.assertRaisesRegex(jev_judge.JevError, "duplicate JSON key"):
                trace.load_items(str(duplicate))

            nonfinite = Path(tmp) / "nonfinite.json"
            nonfinite.write_text(
                '[{"id":"a","kind":"user_request","content":"x","score":NaN}]',
                encoding="utf-8",
            )
            with self.assertRaisesRegex(jev_judge.JevError, "non-finite"):
                trace.load_items(str(nonfinite))

    def test_manifest_records_requested_and_resolved_model(self) -> None:
        items = [anchor(), *pair(1)]
        clean, redactions, pairs, reasons, batches = self.prepared(items)
        result = trace.apply_batch_responses(
            clean,
            pairs,
            reasons,
            batches,
            [trusted_envelope(batches[0], ["keep_verbatim"])],
            POLICY,
            redactions=redactions,
            budget=WIDE,
            recent_items=0,
        )
        self.assertEqual(result["model"]["requested"], jev_judge.DEFAULT_MODEL)
        self.assertEqual(result["model"]["resolved"], [jev_judge.DEFAULT_MODEL])

        envelope = trusted_envelope(batches[0], ["keep_verbatim"])
        with_meta = trace.apply_batch_responses(
            clean,
            pairs,
            reasons,
            batches,
            [envelope],
            POLICY,
            redactions=redactions,
            budget=WIDE,
            recent_items=0,
        )
        self.assertEqual(
            with_meta["judgment"]["batch_runs"][0]["judgment_id"],
            "jdg_0123456789abcdef0123456789abcdef",
        )

    def test_private_output_is_atomic_and_rejects_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output = root / "trace.json"
            trace.write_private_json(output, {"ok": True})
            self.assertEqual(json.loads(output.read_text(encoding="utf-8")), {"ok": True})
            self.assertEqual(output.stat().st_mode & 0o777, 0o600)

            target = root / "target.json"
            target.write_text("{}", encoding="utf-8")
            link = root / "link.json"
            link.symlink_to(target)
            with self.assertRaisesRegex(jev_judge.JevError, "symbolic link"):
                trace.write_private_json(link, {"ok": True})


if __name__ == "__main__":
    unittest.main(verbosity=2)

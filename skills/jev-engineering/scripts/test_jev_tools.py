#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import jev_judge
import jev_log_triage


class JevJudgeTests(unittest.TestCase):
    def test_redacts_sensitive_keys_and_inline_secrets(self) -> None:
        value = {
            "password": "hunter2",
            "accessToken": "camel-access-token",
            "sessionId": "camel-session-id",
            "clientSecret": "camel-client-secret",
            "message": (
                "Authorization: Basic YWxpY2U6cGFzc3dvcmQ=\n"
                "jwt=eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.signature12345\n"
                "key=apikey_abcdefghijklmnopqrstuvwxyz0123456789_ABCD user@example.com"
            ),
            "safe": "kept",
        }
        clean, count = jev_judge.redact(value)
        self.assertGreaterEqual(count, 7)
        self.assertEqual(clean["password"], "<redacted-sensitive-field>")
        self.assertEqual(clean["accessToken"], "<redacted-sensitive-field>")
        self.assertEqual(clean["sessionId"], "<redacted-sensitive-field>")
        self.assertEqual(clean["clientSecret"], "<redacted-sensitive-field>")
        self.assertNotIn("YWxpY2U6cGFzc3dvcmQ", clean["message"])
        self.assertNotIn("eyJhbGci", clean["message"])
        self.assertNotIn("apikey_", clean["message"])
        self.assertNotIn("user@example.com", clean["message"])
        self.assertEqual(clean["safe"], "kept")
        payload = {
            "state": value,
            "questions": {"x": {"type": "noul", "instructions": "is this relevant?"}},
        }
        body, prepared_count, _ = jev_judge.prepare_request(payload, jev_judge.DEFAULT_MODEL)
        encoded = json.dumps(body)
        self.assertGreaterEqual(prepared_count, 7)
        self.assertNotIn("camel-client-secret", encoded)
        self.assertNotIn("apikey_", encoded)

    def test_validates_choice_cardinality(self) -> None:
        payload = {
            "state": {},
            "questions": {"x": {"type": "choice", "instructions": "pick", "criteria": {"only": "one"}}},
        }
        with self.assertRaises(jev_judge.JevError):
            jev_judge.validate_request(payload)

    def test_prepare_is_stable_and_pins_model(self) -> None:
        payload = {
            "state": {"goal": "route"},
            "questions": {"x": {"type": "noul", "instructions": "is this relevant?"}},
        }
        first, _, h1 = jev_judge.prepare_request(payload, "jev-1.13.0")
        second, _, h2 = jev_judge.prepare_request(payload, "jev-1.13.0")
        self.assertEqual(h1, h2)
        self.assertEqual(first, second)
        self.assertEqual(first["model"], "jev-1.13.0")

    def test_log_triage_parser_respects_environment_defaults(self) -> None:
        with mock.patch.dict(
            os.environ,
            {
                "JEV_MODEL": "jev-test-version",
                "JEV_ENDPOINT": "https://example.test/v1/systemone",
            },
        ):
            args = jev_log_triage.build_parser().parse_args(
                ["app.log", "--goal", "find the cause"]
            )
        self.assertEqual(args.model, "jev-test-version")
        self.assertEqual(args.endpoint, "https://example.test/v1/systemone")

    def test_rejects_insecure_remote_endpoint(self) -> None:
        with self.assertRaises(jev_judge.JevError):
            jev_judge.call_jev(
                {"model": "jev-1.13.0", "state": {}, "questions": {}},
                endpoint="http://example.com/systemone",
                timeout=1,
                retries=1,
                api_key="secret",
            )

    def test_rejects_choice_outside_candidates(self) -> None:
        body = {
            "model": jev_judge.DEFAULT_MODEL,
            "questions": {
                "route": {
                    "type": "choice",
                    "instructions": "pick one",
                    "criteria": {"inspect": "inspect", "handoff": "handoff"},
                }
            }
        }
        with self.assertRaises(jev_judge.JevError):
            jev_judge.validate_response(
                body,
                {
                    "model": jev_judge.DEFAULT_MODEL,
                    "usage": {"input_tokens": 1, "output_tokens": 1},
                    "answers": {
                        "route": {
                            "type": "choice",
                            "choice": "delete",
                            "confidence": 0.99,
                            "probabilities": {"inspect": 0.01, "handoff": 0.99},
                        }
                    },
                },
            )

    def test_rejects_unexpected_resolved_model(self) -> None:
        body = {
            "model": jev_judge.DEFAULT_MODEL,
            "questions": {"x": {"type": "noul", "instructions": "judge"}},
        }
        with self.assertRaises(jev_judge.JevError):
            jev_judge.validate_response(
                body,
                {
                    "model": "jev-other",
                    "usage": {"input_tokens": 1, "output_tokens": 1},
                    "answers": {"x": {"type": "noul", "noul": 0.5}},
                },
            )

    def test_cache_avoids_network(self) -> None:
        payload = {
            "state": {"goal": "route"},
            "questions": {"x": {"type": "noul", "instructions": "is this relevant?"}},
        }
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp)
            _, _, request_hash = jev_judge.prepare_request(payload, jev_judge.DEFAULT_MODEL)
            jev_judge._write_cache(
                cache,
                request_hash,
                jev_judge.DEFAULT_ENDPOINT,
                {
                    "model": jev_judge.DEFAULT_MODEL,
                    "answers": {"x": {"type": "noul", "noul": 0.9}},
                    "usage": {"input_tokens": 10, "output_tokens": 2},
                },
                jev_judge.DEFAULT_CACHE_TTL_SECONDS,
            )
            out = jev_judge.run_request(payload, cache_dir=cache, api_key="not-used")
            self.assertTrue(out["meta"]["cached"])
            self.assertEqual(out["response"]["answers"]["x"]["noul"], 0.9)
            self.assertIsNone(
                jev_judge._read_cache(
                    cache,
                    request_hash,
                    "https://different.example/v1/systemone",
                    jev_judge.DEFAULT_CACHE_TTL_SECONDS,
                )
            )
            with self.assertRaises(jev_judge.JevError):
                jev_judge.run_request(
                    payload,
                    cache_dir=cache,
                    endpoint="http://not-local.invalid/systemone",
                    api_key="not-used",
                )

    def test_live_response_is_redacted_before_cache(self) -> None:
        payload = {
            "state": {"goal": "route"},
            "questions": {"x": {"type": "noul", "instructions": "is this relevant?"}},
        }
        original = jev_judge.call_jev
        jev_judge.call_jev = lambda *args, **kwargs: (
            {
                "model": jev_judge.DEFAULT_MODEL,
                "answers": {"x": {"type": "noul", "noul": 0.8}},
                "usage": {"input_tokens": 10, "output_tokens": 2},
                "debug": "Authorization: Bearer abcdefghijklmnopqrstuvwxyz",
            },
            1.0,
        )
        try:
            with tempfile.TemporaryDirectory() as tmp:
                cache = Path(tmp)
                audit = cache / "events.jsonl"
                audit.write_text("", encoding="utf-8")
                audit.chmod(0o644)
                jev_judge.run_request(payload, cache_dir=cache, audit=audit, api_key="not-used")
                cached_text = next(cache.rglob("*.json")).read_text(encoding="utf-8")
                self.assertNotIn("abcdefghijklmnopqrstuvwxyz", cached_text)
                self.assertIn("<redacted>", cached_text)
                event = json.loads(audit.read_text(encoding="utf-8"))
                self.assertEqual(event["request"]["state"], payload["state"])
                self.assertEqual(event["status"], "ok")
                self.assertEqual(audit.stat().st_mode & 0o777, 0o600)
        finally:
            jev_judge.call_jev = original

    def test_failed_call_is_audited_without_raw_secret(self) -> None:
        payload = {
            "state": {"message": "Authorization: Bearer abcdefghijklmnopqrstuvwxyz"},
            "questions": {"x": {"type": "noul", "instructions": "is this relevant?"}},
        }
        original = jev_judge.call_jev
        jev_judge.call_jev = lambda *args, **kwargs: (_ for _ in ()).throw(jev_judge.JevError("boom"))
        try:
            with tempfile.TemporaryDirectory() as tmp:
                audit = Path(tmp) / "events.jsonl"
                with self.assertRaises(jev_judge.JevError):
                    jev_judge.run_request(payload, audit=audit, api_key="not-used")
                text = audit.read_text(encoding="utf-8")
                event = json.loads(text)
                self.assertEqual(event["status"], "error")
                self.assertNotIn("abcdefghijklmnopqrstuvwxyz", text)
        finally:
            jev_judge.call_jev = original


class LogTriageTests(unittest.TestCase):
    def test_groups_error_context_and_maps_lines(self) -> None:
        lines = [
            "start\n",
            "connecting\n",
            "ERROR connection refused\n",
            "retry\n",
            "Traceback: later cascade\n",
            "done\n",
        ]
        request, refs = jev_log_triage.build_request(lines, goal="restore service", source="app.log", context=1)
        self.assertIn("w0", refs)
        self.assertIn("none", request["questions"]["first_actionable_window"]["criteria"])
        self.assertEqual(request["state"]["source_slice"]["source_total_lines"], 6)
        self.assertEqual(request["state"]["evidence_windows"][0]["ref"], "w0")
        self.assertIn("next_read_only_diagnostic", request["questions"])

    def test_preview_redacts_log_secrets(self) -> None:
        lines = ["ERROR api_key=supersecretvalue user@example.com\n"]
        request, _ = jev_log_triage.build_request(lines, goal="diagnose", source="app.log")
        body, count, _ = jev_judge.prepare_request(request, jev_judge.DEFAULT_MODEL)
        encoded = json.dumps(body)
        self.assertGreaterEqual(count, 2)
        self.assertNotIn("supersecretvalue", encoded)
        self.assertNotIn("user@example.com", encoded)

    def test_large_log_reads_only_bounded_tail(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "large.log"
            path.write_text("old secret\n" + "x" * 100 + "\nERROR tail\n", encoding="utf-8")
            lines, metadata, byte_offsets = jev_log_triage._read_log(path, 32)
            text = "".join(lines)
            self.assertIn("ERROR tail", text)
            self.assertNotIn("old secret", text)
            self.assertTrue(metadata["truncated"])
            self.assertFalse(metadata["earliest_file_evidence_observed"])
            self.assertGreater(metadata["included_start_line"], 1)
            request, refs = jev_log_triage.build_request(
                lines,
                goal="diagnose",
                source=path.name,
                line_offset=metadata["included_start_line"] - 1,
                byte_offsets=byte_offsets,
                source_slice=metadata,
            )
            self.assertGreaterEqual(refs["w0"]["start_line"], metadata["included_start_line"])
            self.assertIn("start_byte", refs["w0"])
            self.assertTrue(request["state"]["source_slice"]["truncated"])

            bounded_lines, bounded_meta, bounded_offsets = jev_log_triage._read_log(
                path,
                32,
                max_prefix_scan_bytes=0,
            )
            self.assertFalse(bounded_meta["line_numbers_exact"])
            self.assertIsNone(bounded_meta["included_start_line"])
            bounded_request, bounded_refs = jev_log_triage.build_request(
                bounded_lines,
                goal="diagnose",
                source=path.name,
                line_offset=None,
                byte_offsets=bounded_offsets,
                source_slice=bounded_meta,
            )
            self.assertIn("slice_start_line", bounded_refs["w0"])
            self.assertIn("start_byte", bounded_refs["w0"])
            self.assertFalse(bounded_request["state"]["source_slice"]["line_numbers_exact"])

    def test_cr_only_log_keeps_original_line_numbers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cr.log"
            path.write_bytes(b"line1\rline2\rERROR third")
            lines, metadata, byte_offsets = jev_log_triage._read_log(path, 16)
            self.assertEqual(metadata["included_start_line"], 3)
            self.assertEqual(metadata["source_total_lines"], 3)
            request, refs = jev_log_triage.build_request(
                lines,
                goal="diagnose",
                source=path.name,
                line_offset=metadata["included_start_line"] - 1,
                byte_offsets=byte_offsets,
                source_slice=metadata,
            )
            self.assertEqual(refs["w0"]["start_line"], 3)
            self.assertEqual(request["state"]["source_slice"]["source_total_lines"], 3)

    def test_refuses_partial_oversized_line(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "one-line.log"
            path.write_bytes(b"A" * 100 + b"ERROR secret tail")
            with self.assertRaises(jev_judge.JevError):
                jev_log_triage._read_log(path, 32)

    def test_window_limit_keeps_earliest_and_marks_omissions(self) -> None:
        lines: list[str] = []
        for index in range(50):
            lines.extend([f"ERROR failure {index}\n", "quiet\n"])
        request, refs = jev_log_triage.build_request(
            lines,
            goal="find the first actionable cause",
            source="many.log",
            context=0,
            max_windows=10,
        )
        self.assertEqual(refs["w0"]["start_line"], 1)
        self.assertEqual(request["state"]["source_slice"]["candidate_groups_total"], 50)
        self.assertTrue(request["state"]["source_slice"]["candidate_windows_truncated"])


if __name__ == "__main__":
    unittest.main()

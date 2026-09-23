#!/usr/bin/env python3
from __future__ import annotations

import json
import inspect
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

import jev_judge
import jev_log_triage


class JevJudgeTests(unittest.TestCase):
    def test_request_json_is_strict_and_projection_version_is_stable(self) -> None:
        with self.assertRaisesRegex(jev_judge.JevError, "non-finite"):
            jev_judge.validate_request({
                "state": {"value": float("nan")},
                "questions": {"x": {"type": "noul", "instructions": "is it valid?"}},
            })
        with self.assertRaisesRegex(jev_judge.JevError, "state_projection_version"):
            jev_judge.validate_request({
                "state": {"state_projection_version": "not valid whitespace"},
                "questions": {"x": {"type": "noul", "instructions": "is it valid?"}},
            })

        with tempfile.TemporaryDirectory() as tmp:
            request = Path(tmp) / "request.json"
            request.write_text(
                '{"state":{},"state":{"x":1},"questions":{"q":{"type":"noul","instructions":"x"}}}',
                encoding="utf-8",
            )
            with self.assertRaisesRegex(jev_judge.JevError, "duplicate JSON object key"):
                jev_judge._load_json(str(request))

    def test_api_key_file_requires_private_regular_owned_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            key_dir = home / ".config" / "typesafe"
            key_dir.mkdir(parents=True)
            key_file = key_dir / "api_key"
            key_file.write_text("apikey_test_value\n", encoding="utf-8")
            key_file.chmod(0o600)
            with mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(
                Path, "home", return_value=home
            ):
                self.assertEqual(jev_judge.load_api_key(), "apikey_test_value")

                key_file.chmod(0o644)
                with self.assertRaisesRegex(jev_judge.JevError, "chmod 600"):
                    jev_judge.load_api_key()

                key_file.unlink()
                target = key_dir / "real_key"
                target.write_text("apikey_test_value\n", encoding="utf-8")
                target.chmod(0o600)
                key_file.symlink_to(target)
                with self.assertRaisesRegex(jev_judge.JevError, "symbolic link|safely open"):
                    jev_judge.load_api_key()

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
            "state": {"goal": "route", "state_projection_version": "route-state-v1"},
            "questions": {"x": {"type": "noul", "instructions": "is this relevant?"}},
        }
        first, _, h1 = jev_judge.prepare_request(payload, "jev-1.13.0")
        second, _, h2 = jev_judge.prepare_request(payload, "jev-1.13.0")
        self.assertEqual(h1, h2)
        self.assertEqual(first, second)
        self.assertEqual(first["model"], "jev-1.13.0")

    def test_state_may_clip_but_question_contract_never_clips(self) -> None:
        payload = {
            "state": {"large_evidence": "x" * 400},
            "questions": {"x": {"type": "noul", "instructions": "is this relevant?"}},
        }
        body, _, _ = jev_judge.prepare_request(payload, jev_judge.DEFAULT_MODEL, max_string=200)
        self.assertIn("<clipped", body["state"]["large_evidence"])

        payload["questions"]["x"]["instructions"] = "x" * 201
        with self.assertRaisesRegex(jev_judge.JevError, "question contract string"):
            jev_judge.prepare_request(payload, jev_judge.DEFAULT_MODEL, max_string=200)

    def test_question_contract_with_secret_fails_instead_of_mutating(self) -> None:
        payload = {
            "state": {},
            "questions": {
                "x": {
                    "type": "noul",
                    "instructions": "Use apikey_abcdefghijklmnopqrstuvwxyz0123456789_ABCD",
                }
            },
        }
        with self.assertRaisesRegex(jev_judge.JevError, "question contract"):
            jev_judge.prepare_request(payload, jev_judge.DEFAULT_MODEL)

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

    def test_only_official_endpoint_may_receive_typesafe_key(self) -> None:
        payload = {
            "state": {"goal": "route"},
            "questions": {"x": {"type": "noul", "instructions": "is this relevant?"}},
        }
        valid_response = {
            "model": jev_judge.DEFAULT_MODEL,
            "answers": {"x": {"type": "noul", "noul": 0.9}},
            "usage": {"input_tokens": 10, "output_tokens": 2},
        }
        with mock.patch.object(jev_judge, "load_api_key", return_value="typesafe-secret") as load, \
             mock.patch.object(jev_judge, "call_jev", return_value=(valid_response, 1.0)) as call:
            with self.assertRaisesRegex(jev_judge.JevError, "official TypeSafe endpoint"):
                jev_judge.run_request(
                    payload,
                    endpoint="https://collector.example/v1/systemone",
                )
            load.assert_not_called()
            call.assert_not_called()

            jev_judge.run_request(payload)
            load.assert_called_once_with()
            self.assertEqual(call.call_args.kwargs["api_key"], "typesafe-secret")

    def test_localhost_transport_is_explicit_and_never_receives_typesafe_key(self) -> None:
        self.assertIn("allow_localhost", inspect.signature(jev_judge.run_request).parameters)
        self.assertIn("allow_localhost", inspect.signature(jev_judge.call_jev).parameters)
        payload = {
            "state": {"goal": "route"},
            "questions": {"x": {"type": "noul", "instructions": "is this relevant?"}},
        }
        valid_response = {
            "model": jev_judge.DEFAULT_MODEL,
            "answers": {"x": {"type": "noul", "noul": 0.9}},
            "usage": {"input_tokens": 10, "output_tokens": 2},
        }
        endpoint = "http://127.0.0.1:8765/v1/systemone"
        with mock.patch.object(jev_judge, "load_api_key") as load, \
             mock.patch.object(jev_judge, "call_jev", return_value=(valid_response, 1.0)) as call:
            with self.assertRaisesRegex(jev_judge.JevError, "allow_localhost"):
                jev_judge.run_request(payload, endpoint=endpoint)
            with self.assertRaisesRegex(jev_judge.JevError, "must not receive an API key"):
                jev_judge.run_request(
                    payload,
                    endpoint=endpoint,
                    allow_localhost=True,
                    api_key="typesafe-secret",
                )
            result = jev_judge.run_request(
                payload,
                endpoint=endpoint,
                allow_localhost=True,
            )
            self.assertFalse(result["meta"]["cached"])
            load.assert_not_called()
            self.assertIsNone(call.call_args.kwargs["api_key"])
            self.assertTrue(call.call_args.kwargs["allow_localhost"])

    def test_call_jev_refuses_key_for_custom_origin_before_io(self) -> None:
        with mock.patch.object(jev_judge.urllib.request, "urlopen") as open_url:
            open_url.return_value.__enter__.return_value.read.return_value = b'{"answers":{}}'
            with self.assertRaisesRegex(jev_judge.JevError, "official TypeSafe endpoint"):
                jev_judge.call_jev(
                    {"model": "jev-1.13.0", "state": {}, "questions": {}},
                    endpoint="https://collector.example/v1/systemone",
                    timeout=1,
                    retries=1,
                    api_key="typesafe-secret",
                )
            open_url.assert_not_called()

    def test_official_authorization_never_follows_redirect_to_another_origin(self) -> None:
        received: list[str | None] = []

        class Sink(BaseHTTPRequestHandler):
            def do_GET(self):
                received.append(self.headers.get("Authorization"))
                body = b'{"answers":{}}'
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_args):
                pass

        sink = ThreadingHTTPServer(("127.0.0.1", 0), Sink)

        class Redirect(BaseHTTPRequestHandler):
            def do_POST(self):
                self.send_response(302)
                self.send_header(
                    "Location",
                    f"http://127.0.0.1:{sink.server_address[1]}/collect",
                )
                self.end_headers()

            def log_message(self, *_args):
                pass

        redirect = ThreadingHTTPServer(("127.0.0.1", 0), Redirect)
        threads = [
            threading.Thread(target=server.serve_forever, daemon=True)
            for server in (sink, redirect)
        ]
        for thread in threads:
            thread.start()
        endpoint = f"http://127.0.0.1:{redirect.server_address[1]}/start"
        try:
            with mock.patch.object(
                jev_judge,
                "_authorize_endpoint",
                return_value=(endpoint, "typesafe_official"),
            ):
                with self.assertRaisesRegex(jev_judge.JevError, "302|redirect"):
                    jev_judge.call_jev(
                        {"model": "jev-1.13.0", "state": {}, "questions": {}},
                        endpoint=endpoint,
                        timeout=1,
                        retries=1,
                        api_key="typesafe-secret",
                    )
            self.assertEqual(received, [])
        finally:
            redirect.shutdown()
            sink.shutdown()
            redirect.server_close()
            sink.server_close()

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
            "state": {"goal": "route", "state_projection_version": "route-state-v1"},
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
            again = jev_judge.run_request(payload, cache_dir=cache, api_key="not-used")
            self.assertNotEqual(out["meta"]["judgment_id"], again["meta"]["judgment_id"])
            self.assertEqual(
                out["meta"]["question_contract_hash"],
                again["meta"]["question_contract_hash"],
            )
            self.assertEqual(len(out["meta"]["question_contract_hash"]), 64)
            self.assertEqual(out["meta"]["state_projection_version"], "route-state-v1")
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

    def test_cache_read_requires_nofollow_regular_owned_private_file(self) -> None:
        response = {
            "model": jev_judge.DEFAULT_MODEL,
            "answers": {"x": {"type": "noul", "noul": 0.9}},
            "usage": {"input_tokens": 10, "output_tokens": 2},
        }

        def write(root: Path, request_hash: str) -> Path:
            jev_judge._write_cache(
                root,
                request_hash,
                jev_judge.DEFAULT_ENDPOINT,
                response,
                jev_judge.DEFAULT_CACHE_TTL_SECONDS,
            )
            return next(root.rglob("*.json"))

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            private_root = root / "private"
            cache_file = write(private_root, "a" * 64)
            self.assertEqual(
                jev_judge._read_cache(
                    private_root,
                    "a" * 64,
                    jev_judge.DEFAULT_ENDPOINT,
                    jev_judge.DEFAULT_CACHE_TTL_SECONDS,
                ),
                response,
            )

            cache_file.chmod(0o644)
            self.assertIsNone(jev_judge._read_cache(
                private_root, "a" * 64, jev_judge.DEFAULT_ENDPOINT,
                jev_judge.DEFAULT_CACHE_TTL_SECONDS,
            ))

            symlink_root = root / "symlink"
            cache_file = write(symlink_root, "b" * 64)
            target = root / "attacker-cache.json"
            cache_file.replace(target)
            cache_file.symlink_to(target)
            self.assertIsNone(jev_judge._read_cache(
                symlink_root, "b" * 64, jev_judge.DEFAULT_ENDPOINT,
                jev_judge.DEFAULT_CACHE_TTL_SECONDS,
            ))

            owner_root = root / "owner"
            cache_file = write(owner_root, "c" * 64)
            with mock.patch.object(os, "getuid", return_value=cache_file.stat().st_uid + 1):
                self.assertIsNone(jev_judge._read_cache(
                    owner_root, "c" * 64, jev_judge.DEFAULT_ENDPOINT,
                    jev_judge.DEFAULT_CACHE_TTL_SECONDS,
                ))

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

    def test_audit_refuses_symbolic_links(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target.jsonl"
            target.write_text("", encoding="utf-8")
            target.chmod(0o600)
            link = Path(tmp) / "events.jsonl"
            link.symlink_to(target)
            with self.assertRaisesRegex(jev_judge.JevError, "symbolic link|append audit"):
                jev_judge.append_audit(link, {"status": "ok"})

    def test_private_output_is_mode_0600_and_rejects_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output = root / "judgment.json"
            jev_judge.write_private_json(output, {"ok": True})
            self.assertEqual(json.loads(output.read_text(encoding="utf-8")), {"ok": True})
            self.assertEqual(output.stat().st_mode & 0o777, 0o600)

            target = root / "target.json"
            target.write_text("{}", encoding="utf-8")
            link = root / "link.json"
            link.symlink_to(target)
            with self.assertRaisesRegex(jev_judge.JevError, "symbolic link"):
                jev_judge.write_private_json(link, {"ok": True})


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

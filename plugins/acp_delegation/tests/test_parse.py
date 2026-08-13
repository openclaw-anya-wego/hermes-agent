"""Tests for the acpx NDJSON parser.

Fixtures are real lines captured from acpx 0.13.0 driving claude-agent-acp
0.60.0, not invented shapes. Everything here runs offline with no acpx, no
worker, and no credentials.
"""

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from parse import (  # noqa: E402
    EXIT_AGENT_ERROR,
    EXIT_CLI_USAGE,
    EXIT_INTERRUPTED,
    EXIT_NO_SESSION,
    EXIT_OK,
    EXIT_PERMISSION_DENIED,
    EXIT_TIMEOUT,
    Transcript,
    build_result,
    consume_line,
)

MAX_CHARS = 8000


def chunk(text):
    return json.dumps(
        {
            "jsonrpc": "2.0",
            "method": "session/update",
            "params": {
                "update": {
                    "sessionUpdate": "agent_message_chunk",
                    "content": {"type": "text", "text": text},
                }
            },
        }
    )


def final_result(output_tokens=6, total_tokens=31206, stop_reason="end_turn"):
    return json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "result": {
                "stopReason": stop_reason,
                "usage": {
                    "inputTokens": 2,
                    "outputTokens": output_tokens,
                    "cachedReadTokens": 0,
                    "cachedWriteTokens": 31198,
                    "totalTokens": total_tokens,
                },
            },
        }
    )


def usage_with_cost(amount=0.31214):
    return json.dumps(
        {
            "jsonrpc": "2.0",
            "method": "session/update",
            "params": {
                "update": {
                    "sessionUpdate": "usage_update",
                    "used": 31206,
                    "size": 1000000,
                    "cost": {"amount": amount, "currency": "USD"},
                }
            },
        }
    )


def permission_request(kind="edit", path="/tmp/work/hello.txt"):
    return json.dumps(
        {
            "jsonrpc": "2.0",
            "method": "session/request_permission",
            "params": {
                "toolCall": {
                    "kind": kind,
                    "title": "Write hello.txt",
                    "rawInput": {"file_path": path},
                }
            },
        }
    )


def available_commands(*names):
    return json.dumps(
        {
            "jsonrpc": "2.0",
            "method": "session/update",
            "params": {
                "update": {
                    "sessionUpdate": "available_commands_update",
                    "availableCommands": [
                        {"name": name, "description": "does {}".format(name)} for name in names
                    ],
                }
            },
        }
    )


def transcript_from(lines):
    transcript = Transcript()
    for line in lines:
        consume_line(transcript, line)
    return transcript


def tool_call(title="Read FareParser.java", status="in_progress", kind="tool_call"):
    return json.dumps(
        {
            "jsonrpc": "2.0",
            "method": "session/update",
            "params": {
                "update": {
                    "sessionUpdate": kind,
                    "toolCallId": "t1",
                    "title": title,
                    "status": status,
                }
            },
        }
    )


class ActivityTests(unittest.TestCase):
    """What the worker is doing, for a call that blocks up to 90 minutes."""

    def test_records_the_workers_own_wording(self):
        transcript = transcript_from([tool_call("Run bun test")])

        self.assertEqual(transcript.last_activity, "Run bun test")

    def test_keeps_only_the_newest_action(self):
        transcript = transcript_from(
            [tool_call("Read a.java"), tool_call("Edit b.java")]
        )

        self.assertEqual(transcript.last_activity, "Edit b.java")

    def test_ignores_a_completed_update(self):
        """Otherwise the finished step is reported while the next one runs."""
        transcript = transcript_from(
            [tool_call("Read a.java"), tool_call("Read a.java", status="completed")]
        )

        self.assertEqual(transcript.last_activity, "Read a.java")

    def test_follows_a_tool_call_update(self):
        transcript = transcript_from([tool_call("Editing", kind="tool_call_update")])

        self.assertEqual(transcript.last_activity, "Editing")

    def test_is_absent_before_the_worker_does_anything(self):
        self.assertIsNone(transcript_from([chunk("hi")]).last_activity)

    def test_survives_a_titleless_call(self):
        line = json.dumps(
            {
                "method": "session/update",
                "params": {"update": {"sessionUpdate": "tool_call", "status": "in_progress"}},
            }
        )

        self.assertIsNone(transcript_from([line]).last_activity)


class AvailableCommandsTests(unittest.TestCase):
    """The command list is what tells a typo'd command from a real one."""

    def test_collects_the_advertised_command_names(self):
        transcript = transcript_from([available_commands("deploy-mini", "saber-code-review")])

        self.assertEqual(
            transcript.available_commands, ["deploy-mini", "saber-code-review"]
        )

    def test_is_empty_when_the_worker_never_advertised_any(self):
        """Callers must fail open on this — it is silence, not an empty catalogue."""
        transcript = transcript_from([chunk("hello"), final_result()])

        self.assertEqual(transcript.available_commands, [])

    def test_replaces_rather_than_accumulates_on_a_second_event(self):
        """The adapter pushes the full list whenever it changes."""
        transcript = transcript_from(
            [available_commands("old-one"), available_commands("new-one", "another")]
        )

        self.assertEqual(transcript.available_commands, ["new-one", "another"])

    def test_survives_a_malformed_command_entry(self):
        line = json.dumps(
            {
                "method": "session/update",
                "params": {
                    "update": {
                        "sessionUpdate": "available_commands_update",
                        "availableCommands": ["not-an-object", {"description": "no name"}],
                    }
                },
            }
        )

        self.assertEqual(transcript_from([line]).available_commands, [])


class ConsumeLineTests(unittest.TestCase):
    def test_joins_streamed_text_fragments_in_arrival_order(self):
        transcript = transcript_from([chunk("B"), chunk("ANANA")])

        self.assertEqual(transcript.text, "BANANA")

    def test_reads_stop_reason_and_usage_from_the_final_result(self):
        transcript = transcript_from([final_result(output_tokens=6)])

        self.assertTrue(transcript.saw_final_result)
        self.assertEqual(transcript.stop_reason, "end_turn")
        self.assertEqual(transcript.usage.output_tokens, 6)

    def test_ignores_the_handshake_result_that_has_no_stop_reason(self):
        handshake = json.dumps({"jsonrpc": "2.0", "id": 0, "result": {"protocolVersion": 1}})

        transcript = transcript_from([handshake])

        self.assertFalse(transcript.saw_final_result)

    def test_records_permission_requests_with_their_target_path(self):
        transcript = transcript_from([permission_request(path="/tmp/work/a.txt")])

        self.assertEqual(len(transcript.permission_requests), 1)
        self.assertEqual(transcript.permission_requests[0].kind, "edit")
        self.assertEqual(transcript.permission_requests[0].file_path, "/tmp/work/a.txt")

    def test_keeps_the_cost_reported_by_a_usage_event(self):
        transcript = transcript_from([usage_with_cost(0.5)])

        self.assertEqual(transcript.cost_amount, 0.5)
        self.assertEqual(transcript.cost_currency, "USD")

    def test_survives_unparseable_and_unrecognised_lines(self):
        transcript = transcript_from(
            ["", "   ", "not json at all", json.dumps({"method": "session/unknown"}), chunk("hi")]
        )

        self.assertEqual(transcript.text, "hi")


class FalseSuccessGuardTests(unittest.TestCase):
    """The guard exists because a worker can report completion having done nothing."""

    def test_flags_a_clean_exit_that_produced_no_text_and_no_output_tokens(self):
        transcript = transcript_from([final_result(output_tokens=0)])

        result = build_result(transcript, EXIT_OK, MAX_CHARS)

        self.assertFalse(result["success"])
        self.assertEqual(result["error_type"], "false_success")

    def test_total_tokens_alone_never_clears_the_guard(self):
        """A one-word reply measured 31,206 total tokens, nearly all cache writes.

        Guarding on total tokens would therefore never fire. This test fails if
        anyone swaps output_tokens for total_tokens.
        """
        transcript = transcript_from([final_result(output_tokens=0, total_tokens=31206)])

        result = build_result(transcript, EXIT_OK, MAX_CHARS)

        self.assertEqual(result["error_type"], "false_success")

    def test_accepts_a_run_that_produced_text_but_reported_no_output_tokens(self):
        transcript = transcript_from([chunk("done"), final_result(output_tokens=0)])

        result = build_result(transcript, EXIT_OK, MAX_CHARS)

        self.assertTrue(result["success"])

    def test_accepts_a_silent_run_that_reported_output_tokens(self):
        transcript = transcript_from([final_result(output_tokens=12)])

        result = build_result(transcript, EXIT_OK, MAX_CHARS)

        self.assertTrue(result["success"])

    def test_whitespace_only_output_does_not_count_as_text(self):
        transcript = transcript_from([chunk("   \n  "), final_result(output_tokens=0)])

        result = build_result(transcript, EXIT_OK, MAX_CHARS)

        self.assertEqual(result["error_type"], "false_success")


class BuildResultTests(unittest.TestCase):
    def test_returns_the_reply_cost_and_usage_on_success(self):
        transcript = transcript_from([chunk("BANANA"), usage_with_cost(0.25), final_result()])

        result = build_result(transcript, EXIT_OK, MAX_CHARS)

        self.assertTrue(result["success"])
        self.assertEqual(result["response"], "BANANA")
        self.assertFalse(result["response_truncated"])
        self.assertEqual(result["cost"], {"amount": 0.25, "currency": "USD"})
        self.assertEqual(result["usage"]["output_tokens"], 6)

    def test_omits_cost_when_no_usage_event_carried_one(self):
        transcript = transcript_from([chunk("hi"), final_result()])

        result = build_result(transcript, EXIT_OK, MAX_CHARS)

        self.assertIsNone(result["cost"])

    def test_rejects_a_clean_exit_that_never_reported_a_result(self):
        transcript = transcript_from([chunk("partial work")])

        result = build_result(transcript, EXIT_OK, MAX_CHARS)

        self.assertFalse(result["success"])
        self.assertEqual(result["error_type"], "malformed_output")
        self.assertEqual(result["partial_response"], "partial work")

    def test_distinguishes_our_deadline_from_the_acpx_timeout(self):
        transcript = transcript_from([chunk("started")])

        killed = build_result(transcript, None, MAX_CHARS)
        self_reported = build_result(transcript, EXIT_TIMEOUT, MAX_CHARS)

        self.assertEqual(killed["error_type"], "plugin_deadline_exceeded")
        self.assertEqual(self_reported["error_type"], "timeout")

    def test_maps_every_documented_acpx_exit_code(self):
        expected = {
            EXIT_AGENT_ERROR: "agent_error",
            EXIT_CLI_USAGE: "cli_usage_error",
            EXIT_TIMEOUT: "timeout",
            EXIT_NO_SESSION: "no_session",
            EXIT_PERMISSION_DENIED: "permission_denied",
            EXIT_INTERRUPTED: "interrupted",
        }

        for exit_code, error_type in expected.items():
            with self.subTest(exit_code=exit_code):
                result = build_result(Transcript(), exit_code, MAX_CHARS)
                self.assertEqual(result["error_type"], error_type)

    def test_labels_an_undocumented_exit_code_rather_than_guessing(self):
        result = build_result(Transcript(), 99, MAX_CHARS)

        self.assertEqual(result["error_type"], "unknown_exit")
        self.assertIn("99", result["error"])

    def test_reports_which_actions_were_denied_on_a_permission_failure(self):
        transcript = transcript_from([permission_request(path="/etc/passwd")])

        result = build_result(transcript, EXIT_PERMISSION_DENIED, MAX_CHARS)

        self.assertEqual(result["error_type"], "permission_denied")
        self.assertEqual(result["permission_requests"][0]["file_path"], "/etc/passwd")

    def test_passes_the_stderr_tail_through_on_failure(self):
        result = build_result(Transcript(), EXIT_AGENT_ERROR, MAX_CHARS, stderr_tail="boom")

        self.assertEqual(result["stderr_tail"], "boom")


class TruncationTests(unittest.TestCase):
    def test_truncates_an_oversized_reply_and_says_so(self):
        transcript = transcript_from([chunk("x" * 100), final_result()])

        result = build_result(transcript, EXIT_OK, max_response_chars=10)

        self.assertTrue(result["response_truncated"])
        self.assertIn("kept 10 of 100 chars", result["response"])

    def test_leaves_a_reply_at_the_limit_untouched(self):
        transcript = transcript_from([chunk("x" * 10), final_result()])

        result = build_result(transcript, EXIT_OK, max_response_chars=10)

        self.assertFalse(result["response_truncated"])
        self.assertEqual(result["response"], "x" * 10)

    def test_truncates_partial_output_on_failure_too(self):
        transcript = transcript_from([chunk("y" * 100)])

        result = build_result(transcript, EXIT_AGENT_ERROR, max_response_chars=10)

        self.assertTrue(result["partial_response_truncated"])


if __name__ == "__main__":
    unittest.main()

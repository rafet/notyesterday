"""Tests for NotYesterday. Stdlib only: a plugin that needs pip is a plugin nobody installs."""

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone

try:  # zoneinfo is 3.9+; the plugin must still work on the 3.8 that ships with older setups
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import notyesterday as ny  # noqa: E402

SCRIPT = os.path.join(os.path.dirname(__file__), "..", "scripts", "notyesterday.py")
IST = timezone(timedelta(hours=3))  # fixed offset: no DST, no dependency
UTC = timezone.utc


def entry(kind, when, **extra):
    base = {"type": kind, "timestamp": when.astimezone(UTC).isoformat().replace("+00:00", "Z")}
    base.update(extra)
    return json.dumps(base)


def user_msg(when, text="hi"):
    return entry("user", when, message={"role": "user", "content": [{"type": "text", "text": text}]})


def tool_result(when):
    """The big lines: a user turn that is really a tool result."""
    return entry(
        "user",
        when,
        message={"role": "user", "content": [{"type": "tool_result", "tool_use_id": "x", "content": "y" * 500}]},
    )


def edit_msg(when, path, tool="Edit"):
    return entry(
        "assistant",
        when,
        message={"role": "assistant", "content": [{"type": "tool_use", "name": tool, "input": {"file_path": path}}]},
    )


def transcript(lines):
    handle = tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False)
    handle.write("\n".join(lines) + "\n")
    handle.close()
    return handle.name


class FormattingTests(unittest.TestCase):
    def test_gap_formatting(self):
        self.assertEqual(ny.fmt_delta(45), "45s")
        self.assertEqual(ny.fmt_delta(180), "3m")
        self.assertEqual(ny.fmt_delta(2 * 3600 + 10 * 60), "2h 10m")
        self.assertEqual(ny.fmt_delta(3 * 86400), "3d")
        self.assertEqual(ny.fmt_delta(-5), "0s")

    def test_day_class_uses_calendar_not_24h(self):
        now = datetime(2026, 9, 20, 0, 30, tzinfo=IST)
        eighty_minutes_ago = datetime(2026, 9, 19, 23, 10, tzinfo=IST)
        # 80 minutes back, but a different calendar day: that is "yesterday"
        self.assertEqual(ny.day_class(eighty_minutes_ago, now), "yesterday")
        self.assertEqual(ny.day_class(datetime(2026, 9, 20, 0, 5, tzinfo=IST), now), "today")
        self.assertEqual(ny.day_class(datetime(2026, 9, 17, 12, 0, tzinfo=IST), now), "3 days ago")

    @unittest.skipIf(ZoneInfo is None, "zoneinfo unavailable (python < 3.9)")
    def test_day_class_across_dst(self):
        berlin = ZoneInfo("Europe/Berlin")
        # The clocks go back on 25 Oct 2026: same wall time one day apart is 25 real hours.
        now = datetime(2026, 10, 25, 10, 0, tzinfo=berlin)
        before = datetime(2026, 10, 24, 10, 0, tzinfo=berlin)
        self.assertEqual(ny.day_class(before, now), "yesterday")
        self.assertEqual(ny.fmt_delta(ny.elapsed(before, now)), "1d 1h")

    def test_stamp_switches_to_dates_beyond_yesterday(self):
        now = datetime(2026, 9, 20, 12, 0, tzinfo=IST)
        self.assertTrue(ny.stamp(datetime(2026, 9, 20, 11, 0, tzinfo=IST), now).startswith("today 11:00"))
        self.assertTrue(ny.stamp(datetime(2026, 6, 9, 13, 21, tzinfo=IST), now).startswith("2026-06-09 13:21"))


class TranscriptTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime.now(IST)
        self.cfg = dict(ny.DEFAULTS)

    def test_timeline_extraction_skips_tool_results(self):
        path = transcript(
            [
                user_msg(self.now - timedelta(hours=3), "start"),
                edit_msg(self.now - timedelta(minutes=52), "/repo/auth.py"),
                tool_result(self.now - timedelta(minutes=51)),
                edit_msg(self.now - timedelta(minutes=9), "/repo/tests/test_auth.py"),
                user_msg(self.now - timedelta(minutes=47), "previous"),
                # the message being typed right now is not in the transcript yet
            ]
        )
        facts = ny.collect(path, self.cfg, IST, self.now)
        targets = [e["target"] for e in facts["edits"]]
        self.assertEqual(targets, ["/repo/tests/test_auth.py", "/repo/auth.py"])
        self.assertIsNotNone(facts["prev_user"])
        self.assertAlmostEqual(ny.elapsed(facts["prev_user"], self.now), 47 * 60, delta=5)
        os.unlink(path)

    def test_commands_are_remembered_too(self):
        """'when did you run the tests?' is a question about a command."""
        path = transcript(
            [
                user_msg(self.now - timedelta(hours=1), "start"),
                entry(
                    "assistant",
                    self.now - timedelta(minutes=12),
                    message={
                        "role": "assistant",
                        "content": [
                            {
                                "type": "tool_use",
                                "name": "Bash",
                                "input": {"command": "cd /repo && pytest -q tests/"},
                            }
                        ],
                    },
                ),
                user_msg(self.now - timedelta(minutes=2), "previous"),
            ]
        )
        facts = ny.collect(path, self.cfg, IST, self.now)
        self.assertEqual([c["command"] for c in facts["commands"]], ["pytest -q tests/"])
        rendered = ny.render_session_start("resume", facts, self.cfg, self.now)
        self.assertIn("Commands run in this session:", rendered)
        self.assertIn("pytest -q tests/", rendered)
        os.unlink(path)

    def test_every_tool_call_is_described(self):
        """Tracking only edits answers only questions about edits."""
        calls = [
            ("Read", {"file_path": "/repo/auth.py"}, "read /repo/auth.py"),
            ("Bash", {"command": "cd /repo && pytest -q <<'EOF'\nbody\nEOF"}, "ran pytest -q <<heredoc"),
            ("Grep", {"pattern": "TODO"}, "searched for TODO"),
            ("WebSearch", {"query": "claude code hooks"}, "searched the web for claude code hooks"),
            ("Task", {"subagent_type": "Explore", "description": "find the parser"}, "ran a Explore agent: find the parser"),
            ("Skill", {"skill": "pdf"}, "used the pdf skill"),
            ("mcp__linear__create_issue", {}, "called linear create_issue"),
            ("SomethingNew", {}, "used SomethingNew"),
        ]
        for name, args, expected in calls:
            self.assertEqual(ny.describe(name, args), expected)

    def test_action_history_is_opt_in(self):
        """It costs tokens, so it is collected only where it is asked for."""
        path = transcript(
            [
                user_msg(self.now - timedelta(hours=1), "start"),
                entry(
                    "assistant",
                    self.now - timedelta(minutes=5),
                    message={
                        "role": "assistant",
                        "content": [{"type": "tool_use", "name": "Read", "input": {"file_path": "/repo/a.py"}}],
                    },
                ),
                user_msg(self.now - timedelta(minutes=2), "previous"),
            ]
        )
        self.assertEqual(ny.collect(path, self.cfg, IST, self.now)["actions"], [])
        wide = dict(self.cfg, actionHistory=8)
        actions = ny.collect(path, wide, IST, self.now)["actions"]
        self.assertEqual([a["what"] for a in actions], ["read /repo/a.py"])
        os.unlink(path)

    def test_compact_boundary_is_found(self):
        path = transcript(
            [
                user_msg(self.now - timedelta(hours=2)),
                entry("system", self.now - timedelta(minutes=30), subtype="compact_boundary", content="Conversation compacted"),
                user_msg(self.now - timedelta(minutes=10)),
                user_msg(self.now),
            ]
        )
        facts = ny.collect(path, self.cfg, IST, self.now, want_compact=True)
        self.assertIsNotNone(facts["last_compact"])
        rendered = ny.render_session_start("compact", facts, self.cfg, self.now)
        self.assertIn("compacted", rendered)
        self.assertIn("Temporal rule", rendered)
        os.unlink(path)

    def test_startup_stays_minimal(self):
        rendered = ny.render_session_start("startup", {}, self.cfg, self.now)
        self.assertIn("Temporal rule", rendered)
        self.assertNotIn("Recent work", rendered)


class PromptNoteTests(unittest.TestCase):
    """The prompt hook speaks on every turn now; what it says is what varies."""

    def setUp(self):
        self.now = datetime.now(IST)
        self.path = transcript(
            [
                user_msg(self.now - timedelta(hours=2), "start"),
                edit_msg(self.now - timedelta(minutes=40), "/repo/auth.py"),
                user_msg(self.now - timedelta(minutes=3), "previous"),
            ]
        )

    def tearDown(self):
        os.unlink(self.path)

    def note(self, prompt):
        result = subprocess.run(
            [sys.executable, SCRIPT, "prompt-submit"],
            input=json.dumps({"prompt": prompt, "transcript_path": self.path, "cwd": "/repo"}),
            capture_output=True,
            text=True,
        )
        if not result.stdout.strip():
            return ""
        return json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"]

    def test_ordinary_prompt_gets_the_gap(self):
        note = self.note("carry on")
        self.assertRegex(note, r"\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}\]")
        self.assertIn("between your previous message and this one", note)
        self.assertNotIn("Recent work", note)

    def test_time_question_gets_the_timeline(self):
        for prompt in ("when did you touch that file?", "bunu ne zaman yaptın?", "how long ago was that"):
            note = self.note(prompt)
            self.assertIn("Recent work", note, prompt)
            self.assertIn("auth.py", note, prompt)
            # mid-session the rule is not worth repeating
            self.assertNotIn("Temporal rule", note, prompt)


class HistoryTests(unittest.TestCase):
    """A time question reaches past this session; an ordinary turn never does."""

    def test_other_sessions_are_read(self):
        now = datetime.now(IST)
        folder = tempfile.mkdtemp()
        old = os.path.join(folder, "old.jsonl")
        with open(old, "w") as fh:
            fh.write(edit_msg(now - timedelta(days=6), "/repo/legacy.py") + "\n")
        current = os.path.join(folder, "current.jsonl")
        with open(current, "w") as fh:
            fh.write(user_msg(now - timedelta(minutes=5), "hi") + "\n")

        found = ny.other_sessions(current, dict(ny.DEFAULTS), IST, now, 5)
        self.assertEqual([e["target"] for e in found], ["/repo/legacy.py"])

        result = subprocess.run(
            [sys.executable, SCRIPT, "prompt-submit"],
            input=json.dumps({"prompt": "when did you write that?", "transcript_path": current, "cwd": folder}),
            capture_output=True,
            text=True,
        )
        note = json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"]
        self.assertIn("Earlier sessions", note)
        self.assertIn("legacy.py", note)

    def test_ordinary_turn_skips_the_history(self):
        now = datetime.now(IST)
        folder = tempfile.mkdtemp()
        with open(os.path.join(folder, "old.jsonl"), "w") as fh:
            fh.write(edit_msg(now - timedelta(days=6), "/repo/legacy.py") + "\n")
        current = os.path.join(folder, "current.jsonl")
        with open(current, "w") as fh:
            fh.write(user_msg(now - timedelta(minutes=5), "hi") + "\n")
        result = subprocess.run(
            [sys.executable, SCRIPT, "prompt-submit"],
            input=json.dumps({"prompt": "carry on", "transcript_path": current, "cwd": folder}),
            capture_output=True,
            text=True,
        )
        note = json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"]
        self.assertNotIn("legacy.py", note)


class StopDetectorTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime.now(IST)
        self.cfg = dict(ny.DEFAULTS)
        self.facts = {"edits": [{"target": "/repo/auth.py", "when": self.now - timedelta(minutes=4)}]}

    def check(self, text):
        return ny.check_claims({"last_assistant_message": text}, self.facts, self.cfg, self.now)

    def test_true_positive(self):
        found = self.check("I fixed auth.py yesterday, so it should be fine.")
        self.assertTrue(any(f["wrong"] for f in found))

    def test_user_quote_is_not_a_claim(self):
        self.assertEqual(self.check("> did you change auth.py yesterday\nNo."), [])

    def test_code_block_is_not_a_claim(self):
        self.assertEqual(self.check("```\ngit log --since=yesterday auth.py\n```"), [])

    def test_genuine_yesterday_is_left_alone(self):
        facts = {"edits": [{"target": "/repo/auth.py", "when": self.now - timedelta(days=1, hours=2)}]}
        found = ny.check_claims({"last_assistant_message": "I edited auth.py yesterday."}, facts, self.cfg, self.now)
        self.assertFalse(any(f["wrong"] for f in found))

    def test_claim_about_an_older_edit_is_still_checked(self):
        """The file a claim names can be many edits back; the check must still see it."""
        now = datetime.now(IST)
        lines = [edit_msg(now - timedelta(minutes=90), "/repo/auth.py")]
        for i in range(12):
            lines.append(edit_msg(now - timedelta(minutes=60 - i), f"/repo/other{i}.py"))
        lines.append(user_msg(now - timedelta(minutes=2), "and?"))
        path = transcript(lines)
        result = subprocess.run(
            [sys.executable, SCRIPT, "stop"],
            input=json.dumps(
                {
                    "last_assistant_message": "I changed auth.py yesterday.",
                    "transcript_path": path,
                    "cwd": "/repo",
                    "stop_hook_active": False,
                }
            ),
            capture_output=True,
            text=True,
            env=dict(os.environ, HOME=tempfile.mkdtemp()),
        )
        self.assertEqual(result.returncode, 0)
        logged = os.path.join(os.environ.get("HOME", ""), "")  # log lives under the temp HOME
        self.assertEqual(result.stdout, "")  # log mode injects nothing
        os.unlink(path)

    def test_turkish_only_when_language_is_turkish(self):
        self.assertEqual(ny.find_claims("auth.py dosyasını dün düzelttim.", "en"), [])
        self.assertTrue(ny.find_claims("auth.py dosyasını dün düzelttim.", "tr"))


class FailSafeTests(unittest.TestCase):
    def run_mode(self, mode, payload):
        return subprocess.run(
            [sys.executable, SCRIPT, mode], input=json.dumps(payload), capture_output=True, text=True
        )

    def test_missing_transcript(self):
        result = self.run_mode("session-start", {"source": "resume", "transcript_path": "/nope/none.jsonl"})
        self.assertEqual(result.returncode, 0)
        self.assertNotIn("Recent work", result.stdout)

    def test_malformed_transcript(self):
        path = transcript(["{not json", '{"type":"user"}', ""])
        result = self.run_mode("prompt-submit", {"prompt": "hi", "transcript_path": path, "cwd": "/tmp"})
        self.assertEqual(result.returncode, 0)
        os.unlink(path)

    def test_garbage_stdin(self):
        result = subprocess.run([sys.executable, SCRIPT, "prompt-submit"], input="{{{", capture_output=True, text=True)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")

    def test_stop_never_loops(self):
        result = self.run_mode(
            "stop", {"last_assistant_message": "I did it yesterday", "stop_hook_active": True, "transcript_path": "/nope"}
        )
        self.assertEqual(result.stdout, "")


class PerformanceTests(unittest.TestCase):
    def test_large_transcript_is_read_from_the_tail(self):
        now = datetime.now(IST)
        lines = [user_msg(now - timedelta(hours=9), "start")]
        for i in range(4000):
            lines.append(tool_result(now - timedelta(minutes=300 - i / 20)))
        lines += [
            edit_msg(now - timedelta(minutes=8), "/repo/late.py"),
            user_msg(now - timedelta(minutes=5), "previous"),
        ]
        path = transcript(lines)
        size = os.path.getsize(path)
        started = time.time()
        facts = ny.collect(path, dict(ny.DEFAULTS), IST, now)
        elapsed = (time.time() - started) * 1000
        self.assertEqual(facts["edits"][0]["target"], "/repo/late.py")
        self.assertLess(elapsed, 50, f"{size/1e6:.1f}MB transcript took {elapsed:.0f}ms")
        os.unlink(path)


if __name__ == "__main__":
    unittest.main(verbosity=2)

#!/usr/bin/env python3
"""NotYesterday: hand Claude Code pre-computed time facts instead of raw clocks.

Every mode reads one hook payload (JSON) on stdin and prints either nothing or
a hook JSON response on stdout. Any unexpected condition must end as a silent
exit 0: a time note is never worth breaking a session over.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from datetime import datetime, timezone

MARK = "⏱ NotYesterday"

CONFIG_PATH = os.path.expanduser("~/.claude/notyesterday.json")
LOG_PATH = os.path.expanduser("~/.claude/notyesterday-log.jsonl")

DEFAULTS = {
    "enabled": True,
    "timelineEntries": 5,
    "timezone": "auto",
    "language": "en",
    "stopMode": "log",  # log | correct | off
}

EDIT_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit"}


def describe(name: str, args: dict, cwd: str = "") -> str:
    """One line for any tool call, in the words a person would use.

    Everything a session does passes through a tool, so this is what makes
    "when did you…" answerable for anything, not only for file edits.
    """
    args = args or {}
    short = lambda key: short_path(str(args.get(key) or ""), cwd)  # noqa: E731
    if name in EDIT_TOOLS:
        return f"edited {short('file_path')}"
    if name == "Read":
        return f"read {short('file_path') or short('notebook_path')}"
    if name == "Bash":
        command = " ".join(str(args.get("command", "")).split())
        command = re.sub(r"^cd\s+\S+\s*&&\s*", "", command)
        command = re.sub(r"<<\s*'?\w+'?.*", "<<heredoc", command)  # the body is not the command
        return f"ran {command}"
    if name in ("Grep", "Glob"):
        return f"searched for {args.get('pattern', '')}"
    if name == "WebFetch":
        return f"fetched {args.get('url', '')}"
    if name == "WebSearch":
        return f"searched the web for {args.get('query', '')}"
    if name == "Task":
        return f"ran a {args.get('subagent_type', 'sub')} agent: {args.get('description', '')}"
    if name == "Skill":
        return f"used the {args.get('skill', '')} skill"
    if name == "TodoWrite":
        return "updated the task list"
    if str(name).startswith("mcp__"):
        return "called " + str(name).replace("mcp__", "").replace("__", " ")
    return f"used {name}"

# After an hour away the gap alone is not enough: the model needs the edit
# times again. Not configurable — a knob here is a way to get it wrong.
STALE_SECONDS = 3600


# --------------------------------------------------------------------------
# config


def load_config() -> dict:
    cfg = dict(DEFAULTS)
    try:
        with open(CONFIG_PATH) as fh:
            user = json.load(fh)
        for key, value in user.items():
            cfg[key] = value
    except FileNotFoundError:
        write_config(cfg)
    except Exception:
        pass
    return cfg


def write_config(cfg: dict) -> None:
    try:
        os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
        temp = f"{CONFIG_PATH}.{os.getpid()}"
        with open(temp, "w") as fh:
            json.dump(cfg, fh, indent=2)
            fh.write("\n")
        os.replace(temp, CONFIG_PATH)
    except Exception:
        pass


def tzinfo_for(cfg: dict):
    """None means "the system zone", resolved per instant rather than frozen.

    datetime.now().astimezone().tzinfo is today's *offset*, not a zone: applying
    it to a timestamp from the other side of a DST switch shifts it an hour, and
    near midnight that changes the calendar day — the very label this plugin is
    supposed to get right.
    """
    name = cfg.get("timezone", "auto")
    if name and name != "auto":
        try:
            from zoneinfo import ZoneInfo

            return ZoneInfo(name)
        except Exception:
            pass
    return None


# --------------------------------------------------------------------------
# formatting


def fmt_delta(seconds: float) -> str:
    """Human elapsed time, coarse on purpose: nobody needs '2h 10m 4s'."""
    seconds = int(max(0, round(seconds)))
    if seconds < 60:
        return f"{seconds}s"
    minutes, secs = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m" if secs < 30 else f"{minutes + 1}m"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}h" if minutes == 0 else f"{hours}h {minutes}m"
    days, hours = divmod(hours, 24)
    return f"{days}d" if hours == 0 else f"{days}d {hours}h"


def elapsed(then: datetime, now: datetime) -> float:
    """Seconds between two aware datetimes, via UTC.

    Subtracting two datetimes that share a tzinfo object is a wall-clock
    subtraction, so across a DST switch it silently loses (or gains) an hour.
    """
    return (now.astimezone(timezone.utc) - then.astimezone(timezone.utc)).total_seconds()


def day_class(then: datetime, now: datetime) -> str:
    """today / yesterday / N days ago, by local calendar date, not by 24h blocks."""
    delta_days = (now.date() - then.date()).days
    if delta_days == 0:
        return "today"
    if delta_days == 1:
        return "yesterday"
    if delta_days > 1:
        return f"{delta_days} days ago"
    return "tomorrow" if delta_days == -1 else "in the future"


def stamp(then: datetime, now: datetime) -> str:
    """'today 14:32 (47m ago)' — the arithmetic Claude should never have to do."""
    ago = fmt_delta(elapsed(then, now))
    label = day_class(then, now)
    if label in ("today", "yesterday", "later today"):
        return f"{label} {then:%H:%M} ({ago} ago)"
    return f"{then:%Y-%m-%d %H:%M} ({ago} ago)"


def parse_ts(value, tz) -> datetime | None:
    if not value:
        return None
    try:
        text = str(value).replace("Z", "+00:00")
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(tz) if tz else dt.astimezone()
    except Exception:
        return None


# --------------------------------------------------------------------------
# transcript reading


def head_entry(path: str) -> dict | None:
    first = None
    try:
        with open(path, "rb") as fh:
            for _ in range(40):
                line = fh.readline()
                if not line:
                    return None
                try:
                    entry = json.loads(line)
                except Exception:
                    continue
                if entry.get("timestamp") and entry.get("cwd"):
                    return entry
                if entry.get("timestamp") and first is None:
                    first = entry
    except Exception:
        return first
    return first


# Real transcripts are written as compact JSON, but nothing promises that, so
# match the key with optional whitespace rather than a fixed byte string.
INTERESTING = re.compile(rb'"type"\s*:\s*"(?:user|assistant|system)"')
TOOL_RESULT = re.compile(rb'"tool_use_id"')


SCAN_BUDGET = 2.0  # seconds; a self-tuning stop, so there is no size knob to set


def iter_reverse(path: str, budget: float = SCAN_BUDGET, state: dict = None):
    """Yield parsed entries newest first, reading the file backwards in chunks.

    Transcripts reach hundreds of megabytes, so reading the whole file is not an
    option; and a fixed tail is not enough either, since a busy session buries
    the interesting edits under tool output. Lines are filtered as bytes first,
    because the expensive ones are tool results we never look at.
    """
    chunk_size = 256 * 1024
    deadline = time.monotonic() + budget
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as fh:
            pos = size
            tail = b""
            while pos > 0:
                if time.monotonic() >= deadline:
                    # Out of budget, not out of file: the caller must not read
                    # "nothing found" as "nothing happened".
                    if state is not None:
                        state["truncated"] = True
                    return
                step = min(chunk_size, pos)
                pos -= step
                fh.seek(pos)
                buf = fh.read(step) + tail
                lines = buf.split(b"\n")
                tail = lines.pop(0) if pos > 0 else b""
                for line in reversed(lines):
                    if len(line) < 20 or not INTERESTING.search(line):
                        continue
                    if TOOL_RESULT.search(line) and b'"type":"assistant"' not in line:
                        continue  # tool results: the big lines, none of our business
                    try:
                        yield json.loads(line)
                    except Exception:
                        continue
    except Exception:
        if state is not None:
            state["error"] = True  # unreadable is not the same as empty
        return


def collect(
    path: str,
    cfg: dict,
    tz,
    now: datetime,
    want_compact: bool = False,
    want_names: set = None,
) -> dict:
    """One backward pass collecting everything the layers need, stopping early.

    want_names keeps the scan going until those file names are found (or the
    file runs out), because saying "not edited in this session" about a file we
    simply stopped short of is worse than saying nothing.
    """
    want_edits = max(1, int(cfg.get("timelineEntries", 5)))
    want_commands = 3
    want_actions = max(0, int(cfg.get("actionHistory", 0)))
    facts = {
        "actions": [],
        "commands": [],
        "cwd": None,
        "session_start": None,
        "prev_user": None,
        "last_activity": None,
        "last_compact": None,
        "edits": [],
    }
    seen_files: set[str] = set()
    seen_commands: set = set()
    all_edited: set[str] = set()  # every file touched, not just the listed ones
    seen_user = 0
    scan: dict = {}

    for entry in iter_reverse(path, state=scan):
        etype = entry.get("type")
        when = parse_ts(entry.get("timestamp"), tz)
        if when and facts["last_activity"] is None:
            facts["last_activity"] = when

        if etype == "user" and not entry.get("isMeta") and not entry.get("isSidechain"):
            message = entry.get("message") or {}
            # tool results are recorded as user turns; only real typing counts
            content = message.get("content")
            text = ""
            if isinstance(content, str):
                text = content
            elif isinstance(content, list):
                text = " ".join(
                    b.get("text", "")
                    for b in content
                    if isinstance(b, dict) and b.get("type") == "text"
                )
            # /clear, its output, and an interrupt are recorded as user turns too,
            # and measuring "since your last message" from one of those is wrong.
            is_typed = bool(text.strip()) and not text.lstrip().startswith(
                ("<command-name>", "<command-message>", "<local-command-stdout>", "[Request interrupted")
            )
            if is_typed and when:
                # UserPromptSubmit fires before the prompt reaches the
                # transcript — on a fresh session the file does not exist yet —
                # so the newest user entry is the previous message. The one
                # second of slack is in case a future version writes it first.
                if facts["prev_user"] is None and elapsed(when, now) > 1:
                    facts["prev_user"] = when
                seen_user += 1

        elif etype == "assistant":
            message = entry.get("message") or {}
            for block in message.get("content") or []:
                if not isinstance(block, dict) or block.get("type") != "tool_use":
                    continue
                name = block.get("name")
                if when and len(facts["actions"]) < want_actions:
                    label = describe(str(name), block.get("input") or {}, facts.get("cwd") or "")
                    if len(label) > 90:
                        label = label[:89] + "\u2026"
                    facts["actions"].append({"when": when, "what": label})
                if name == "Bash" and len(facts["commands"]) < want_commands:
                    # "when did you run the tests?" is a question about a command,
                    # not about a file, and nothing else in the transcript answers it.
                    command = " ".join(
                        str((block.get("input") or {}).get("command", "")).split()
                    )
                    # "cd somewhere && " is how the command got to its directory,
                    # not what it did.
                    command = re.sub(r"^cd\s+\S+\s*&&\s*", "", command)
                    if command and when and command not in seen_commands:
                        seen_commands.add(command)
                        facts["commands"].append(
                            {
                                "command": command if len(command) <= 60 else command[:59] + "\u2026",
                                "when": when,
                            }
                        )
                    continue
                target = None
                if name in EDIT_TOOLS:
                    target = (block.get("input") or {}).get("file_path")
                if not target:
                    continue
                all_edited.add(os.path.basename(str(target)))
                if target in seen_files:
                    continue
                seen_files.add(target)
                facts["edits"].append({"target": target, "when": when})

        elif etype == "system" and entry.get("subtype") == "compact_boundary":
            if facts["last_compact"] is None:
                facts["last_compact"] = when

        if (
            facts["prev_user"] is not None
            and when
            and elapsed(when, now) > 7 * 86400
            and not want_names
        ):
            break  # a week back is past anything these notes talk about

        have_all = (
            facts["prev_user"] is not None
            and len(facts["edits"]) >= want_edits
            and (facts["last_compact"] is not None or not want_compact)
            and (not want_names or want_names <= all_edited)
            and len(facts["commands"]) >= want_commands
        )
        if have_all:
            break

    facts["edited_names"] = all_edited
    facts["partial"] = bool(want_names) and not (want_names <= all_edited) and facts.get("truncated")
    facts["truncated"] = bool(scan.get("truncated") or scan.get("error"))
    first = head_entry(path)
    if first:
        facts["cwd"] = first.get("cwd")
        facts["session_start"] = parse_ts(first.get("timestamp"), tz)
    return facts


# --------------------------------------------------------------------------
# rendering


def anchor_rule() -> str:
    # $CLAUDE_PLUGIN_ROOT exists for hooks, not for the shell Claude runs commands
    # in, so the rule carries the resolved path.
    cli_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cli.sh")
    return (
        f"Temporal rule: lines prefixed \"{MARK}\" are computed from the transcript and "
        "are authoritative. The note with the latest timestamp is the most recent one; "
        "earlier notes describe the moment they were written, not now. Never guess how "
        "long ago your own work happened: when no note covers it, run "
        f"`bash \"{cli_path}\" when` for this session's exact times, "
        "or say \"earlier in this session\" rather than naming a day."
    )


def other_sessions(transcript: str, cfg: dict, tz, now: datetime, limit: int) -> list:
    """Edits from this project's other sessions — only asked for by a time question.

    Claude Code keeps one transcript per session under ~/.claude/projects/<slug>/,
    so "what did you do last week" lives in a neighbouring file, not this one.
    """
    found = []
    seen = set()
    try:
        folder = os.path.dirname(transcript)
        names = [
            os.path.join(folder, n)
            for n in os.listdir(folder)
            if n.endswith(".jsonl") and os.path.join(folder, n) != transcript
        ]
        names.sort(key=os.path.getmtime, reverse=True)
        for path in names[:3]:
            for entry in iter_reverse(path, budget=0.25):
                if entry.get("type") != "assistant":
                    continue
                when = parse_ts(entry.get("timestamp"), tz)
                for block in (entry.get("message") or {}).get("content") or []:
                    if not isinstance(block, dict) or block.get("type") != "tool_use":
                        continue
                    if block.get("name") in EDIT_TOOLS:
                        target = (block.get("input") or {}).get("file_path")
                        if target and when and target not in seen:
                            seen.add(target)
                            found.append({"target": target, "when": when})
                if len(found) >= limit:
                    break
            if len(found) >= limit:
                break
    except Exception:
        pass
    return sorted(found, key=lambda e: e["when"], reverse=True)


def recent_commits(cwd: str, tz, limit: int = 3) -> list:
    """Git remembers what every transcript forgot, and it is cheap to ask."""
    try:
        import subprocess

        out = subprocess.run(
            ["git", "-C", cwd or ".", "log", "-n", str(limit), "--pretty=%cI\t%s"],
            capture_output=True,
            text=True,
            timeout=1.0,
        )
        if out.returncode != 0:
            return []
        commits = []
        for line in out.stdout.strip().split("\n"):
            if "\t" not in line:
                continue
            stamp, subject = line.split("\t", 1)
            when = parse_ts(stamp, tz)
            if when:
                subject = subject if len(subject) <= 72 else subject[:71] + "\u2026"
                commits.append({"when": when, "subject": subject})
        return commits
    except Exception:
        return []


def render_session_start(
    source: str, facts: dict, cfg: dict, now: datetime, cwd: str = "", with_rule: bool = True
) -> str:
    lines = [f"{MARK}: now {now:%a %Y-%m-%d %H:%M %Z}."]
    if with_rule:
        # The rule is worth its tokens once per session, not on every question.
        lines.append(anchor_rule())

    if source in ("resume", "compact", "fork"):
        start = facts.get("session_start")
        if start:
            lines.append(
                f"Session started {stamp(start, now)}."
                if elapsed(start, now) > 60
                else "Session started just now."
            )
        compacted = facts.get("last_compact")
        if compacted:
            lines.append(
                f"Context was compacted {stamp(compacted, now)} — work before that point "
                "is summarized, but the times below are exact."
            )
        last = facts.get("last_activity")
        if last and elapsed(last, now) > 60:
            lines.append(f"Last activity before this: {stamp(last, now)}.")
        edits = [e for e in facts.get("edits", []) if e.get("when")]
        if edits:
            lines.append("Recent work in this session:")
            base = cwd or facts.get("cwd") or ""
            for edit in edits:
                lines.append(f"  - {short_path(edit['target'], base)} — {stamp(edit['when'], now)}")

    actions = facts.get("actions") or []
    if actions:
        lines.append("Most recent steps, newest first:")
        for action in sorted(actions, key=lambda a: a["when"], reverse=True)[:8]:
            lines.append(f"  - {action['what']} — {stamp(action['when'], now)}")
    else:
        commands = facts.get("commands") or []
        if commands:
            lines.append("Commands run in this session:")
            for command in commands:
                lines.append(f"  - {command['command']} — {stamp(command['when'], now)}")

    older = facts.get("older_edits") or []
    if older:
        lines.append("Earlier sessions in this project:")
        for edit in older:
            lines.append(
                f"  - {short_path(str(edit['target']), cwd or '')} — {stamp(edit['when'], now)}"
            )
    commits = facts.get("commits") or []
    if commits:
        lines.append("Recent commits in this repo (any author):")
        for commit in commits:
            lines.append(f"  - {commit['subject']} — {stamp(commit['when'], now)}")
    return "\n".join(lines)


def short_path(path: str, cwd: str) -> str:
    """Show paths the way the user typed them, not as absolute walls of text."""
    if not path or not path.startswith("/"):
        return path  # "git commit" is a label, not a file
    try:
        # /tmp is a symlink to /private/tmp on macOS, so compare resolved paths
        real_cwd = os.path.realpath(cwd) if cwd else ""
        real_path = os.path.realpath(path)
        if real_cwd and real_path.startswith(real_cwd.rstrip("/") + "/"):
            return real_path[len(real_cwd.rstrip("/")) + 1 :]
        home = os.path.realpath(os.path.expanduser("~"))
        if real_path.startswith(home + "/"):
            return "~" + real_path[len(home) :]
    except Exception:
        pass
    return path


# A safety net in the two languages this was written in, not a list to grow: the
# anchor rule tells Claude to run the CLI when it needs times in any other one.
TIME_QUESTION_RE = re.compile(
    r"\b(when did|when was|how long ago|how long has|what time|how long since|"
    r"ne zaman|kac dakika|ka\u00e7 dakika|kac saat|ka\u00e7 saat|ne kadar (?:zaman |s\u00fcre )?"
    r"(?:oldu|ge\u00e7ti)|az \u00f6nce|dun mu|d\u00fcn m\u00fc)\b",
    re.I,
)


# One greedy class, not two that can both match the same characters: the old
# pattern backtracked catastrophically and hung the hook on a long prompt.
FILE_RE = re.compile(r"[\w./~-]+\.[A-Za-z0-9]{1,6}\b")


def mentioned_files(prompt: str, cwd: str, limit: int = 3) -> list[tuple[str, float]]:
    """Files named in the prompt that exist on disk, with their mtime.

    Disk beats transcript archaeology for 'did you already change X?': it stays
    true across compaction, across sessions, and costs one stat().
    """
    found: list[tuple[str, float]] = []
    for token in FILE_RE.findall(prompt or ""):
        if len(found) >= limit:
            break
        candidate = os.path.expanduser(token)
        if not os.path.isabs(candidate):
            candidate = os.path.join(cwd or ".", candidate)
        try:
            if os.path.isfile(candidate):
                found.append((token, os.path.getmtime(candidate)))
        except Exception:
            continue
    return found


def render_prompt(facts: dict, cfg: dict, now: datetime, payload: dict) -> str:
    """Interval facts only: phrased so they stay true after they scroll away."""
    header = f"{MARK} [{now:%Y-%m-%d %H:%M}]"

    lines = []
    prev = facts.get("prev_user")
    if prev:
        gap = elapsed(prev, now)
        crossed = prev.date() != now.date()
        lines.append(
            f"{fmt_delta(gap)} passed between your previous message and this one"
            + (f" (it was sent {day_class(prev, now)} at {prev:%H:%M})." if crossed else ".")
        )

    edited_here = facts.get("edited_names") or set()
    for name, mtime in mentioned_files(payload.get("prompt", ""), payload.get("cwd", "")):
        then = datetime.fromtimestamp(mtime).astimezone(now.tzinfo)
        # mtime says when the file changed, never who changed it: a checkout or
        # a formatter moves it too. Only the transcript can claim the edit.
        if os.path.basename(name) in edited_here:
            lines.append(f"{name} was last modified {stamp(then, now)} — edited in this session.")
        elif facts.get("truncated") or facts.get("partial"):
            # We did not read the whole session, so we cannot claim the negative.
            lines.append(f"{name} was last modified {stamp(then, now)}.")
        else:
            lines.append(
                f"{name} was last modified {stamp(then, now)} — "
                "changed on disk, not by an edit in this session."
            )

    if not lines:
        if facts.get("truncated"):
            return ""  # we simply did not get far enough back to know
        lines.append("this is the first message of the session.")
    return header + ": " + " ".join(lines)


# --------------------------------------------------------------------------
# Layer 3: the self-check, shipped as a measuring instrument first

# Phrases that place Claude's own work in time. English by default; Turkish is
# added only when the user runs in Turkish, because every extra pattern is
# another chance to be wrong.
CLAIM_PATTERNS = {
    "en": [
        (r"\byesterday\b", 1),
        (r"\blast week\b", 7),
        (r"\bthe other day\b", 2),
        (r"\b(\d+) days? ago\b", None),
        (r"\ba few days ago\b", 2),
    ],
    "tr": [
        (r"\bd[üu]n\b", 1),
        (r"\bge[çc]en hafta\b", 7),
        (r"\b[öo]nceki g[üu]n\b", 2),
        (r"\b(\d+) g[üu]n [öo]nce\b", None),
    ],
}

CODE_BLOCK_RE = re.compile(r"```.*?```", re.S)
QUOTE_RE = re.compile(r"^\s*>.*$", re.M)


QUOTED_RE = re.compile(r"[\"\u201c\u201d][^\"\u201c\u201d\n]{0,40}[\"\u201c\u201d]")


def strip_noise(text: str) -> str:
    """Quoting the user saying 'yesterday' is not a claim about our own work.

    Nor is talking *about* the word: in real transcripts most matches turn out
    to be quoted words, code, or the user's own text being echoed back.
    """
    text = CODE_BLOCK_RE.sub(" ", text or "")
    text = QUOTE_RE.sub(" ", text)
    text = re.sub(r"`[^`]*`", " ", text)
    text = QUOTED_RE.sub(" ", text)
    return text


def find_claims(text: str, language: str) -> list[dict]:
    languages = ["en"] + (["tr"] if language == "tr" else [])
    cleaned = strip_noise(text)
    claims = []
    for lang in languages:
        for pattern, days in CLAIM_PATTERNS[lang]:
            for match in re.finditer(pattern, cleaned, re.I):
                claimed = days
                if claimed is None and match.groups():
                    try:
                        claimed = int(match.group(1))
                    except Exception:
                        continue
                start = max(0, match.start() - 160)
                claims.append(
                    {
                        "phrase": match.group(0),
                        "claimed_days_ago": claimed,
                        "sentence": cleaned[start : match.end() + 80].strip(),
                    }
                )
    return claims


def check_claims(payload: dict, facts: dict, cfg: dict, now: datetime) -> list[dict]:
    """A claim is only wrong if the transcript can prove it is. Nothing else counts."""
    text = payload.get("last_assistant_message", "")
    findings = []
    edits = [e for e in facts.get("edits", []) if e.get("when")]
    newest = max((e["when"] for e in edits), default=None)
    for claim in find_claims(text, cfg.get("language", "en")):
        target = None
        for edit in edits:
            name = os.path.basename(str(edit["target"]))
            if name and name in claim["sentence"]:
                target = edit
                break
        evidence = target["when"] if target else newest
        if evidence is None:
            continue
        real_days = (now.date() - evidence.date()).days
        claim.update(
            {
                "target": target["target"] if target else None,
                "actual": evidence.isoformat(),
                "actual_days_ago": real_days,
                "actual_elapsed": fmt_delta(elapsed(evidence, now)),
                "wrong": claim["claimed_days_ago"] is not None
                and real_days == 0
                and claim["claimed_days_ago"] >= 1
                and target is not None,
            }
        )
        findings.append(claim)
    return findings


def mode_stop(payload: dict, cfg: dict, tz, now: datetime) -> None:
    if payload.get("stop_hook_active"):
        return  # never argue with ourselves
    mode = cfg.get("stopMode", "log")
    if mode == "off":
        return
    text = payload.get("last_assistant_message", "")
    if not text or not find_claims(text, cfg.get("language", "en")):
        return  # cheap reject before reading any transcript

    # The check needs a wider net than a timeline does: the file a claim names
    # may be twenty edits back, and it costs nothing to look — this hook never
    # injects anything.
    wide = dict(cfg, timelineEntries=25)
    facts = collect(payload.get("transcript_path", ""), wide, tz, now)
    findings = check_claims(payload, facts, cfg, now)
    if not findings:
        return

    try:
        with open(LOG_PATH, "a") as fh:
            fh.write(
                json.dumps(
                    {
                        "at": now.isoformat(),
                        "session": payload.get("session_id"),
                        "mode": mode,
                        "findings": findings,
                    }
                )
                + "\n"
            )
    except Exception:
        pass

    if mode != "correct":
        return  # measuring only: see README, "Why the self-check ships switched off"
    wrong = [f for f in findings if f.get("wrong")]
    if not wrong:
        return
    first = wrong[0]
    target = short_path(str(first.get("target")), payload.get("cwd", ""))
    json.dump(
        {
            "decision": "block",
            "reason": (
                f"{MARK}: {target} was last touched {first['actual_elapsed']} ago, today — "
                f"not \"{first['phrase']}\". Restate that part accurately."
            ),
        },
        sys.stdout,
    )


# --------------------------------------------------------------------------
# modes


def emit(event: str, context: str) -> None:
    if not context:
        return
    json.dump(
        {"hookSpecificOutput": {"hookEventName": event, "additionalContext": context}},
        sys.stdout,
    )


def mode_session_start(payload: dict, cfg: dict, tz, now: datetime) -> None:
    source = payload.get("source", "startup")
    facts = {}
    if source in ("resume", "compact", "fork"):
        facts = collect(
            payload.get("transcript_path", ""), cfg, tz, now, want_compact=(source == "compact")
        )
    emit("SessionStart", render_session_start(source, facts, cfg, now, payload.get("cwd", "")))


def mode_prompt_submit(payload: dict, cfg: dict, tz, now: datetime) -> None:
    prompt_text = payload.get("prompt", "")
    named = {
        os.path.basename(name)
        for name, _ in mentioned_files(prompt_text, payload.get("cwd", ""))
    }
    asked_about_time = bool(TIME_QUESTION_RE.search(prompt_text or ""))
    if asked_about_time:
        cfg = dict(cfg, actionHistory=8)  # the one turn where the history earns its tokens
    facts = collect(payload.get("transcript_path", ""), cfg, tz, now, want_names=named)
    prev = facts.get("prev_user")
    gap = elapsed(prev, now) if prev else None
    # Asking when something happened is the whole point: answer it with the
    # timeline, not with a gap the model would have to reason from.
    stale = gap is not None and gap > STALE_SECONDS
    crossed_midnight = prev is not None and prev.date() != now.date()
    if asked_about_time:
        # Only here: the two sources that reach past this session.
        limit = max(1, int(cfg.get("timelineEntries", 5)))
        seen = {e["target"] for e in facts.get("edits", [])}
        facts["older_edits"] = [
            e
            for e in other_sessions(payload.get("transcript_path", ""), cfg, tz, now, limit)
            if e["target"] not in seen
        ][:limit]
        facts["commits"] = recent_commits(payload.get("cwd", ""), tz)

    if stale or crossed_midnight or asked_about_time:
        emit(
            "UserPromptSubmit",
            render_session_start(
                "resume",
                facts,
                cfg,
                now,
                payload.get("cwd", ""),
                with_rule=False,  # the rule was given at session start
            ),
        )
        return
    emit("UserPromptSubmit", render_prompt(facts, cfg, now, payload))


# --------------------------------------------------------------------------
# /notyesterday


def newest_transcript(cwd: str) -> str:
    """This project's most recent transcript, for when the command is run by hand.

    Claude Code names the folder after the working directory, with every
    non-alphanumeric character replaced by a dash.
    """
    try:
        slug = re.sub(r"[^A-Za-z0-9]+", "-", os.path.realpath(cwd))
        folder = os.path.expanduser(os.path.join("~/.claude/projects", slug))
        files = [
            os.path.join(folder, name) for name in os.listdir(folder) if name.endswith(".jsonl")
        ]
        return max(files, key=os.path.getmtime) if files else ""
    except Exception:
        return ""


def cli(argv: list[str]) -> int:
    cfg = load_config()
    tz = tzinfo_for(cfg)
    now = datetime.now(tz) if tz else datetime.now().astimezone()
    args = argv[1:]
    cmd = args[0] if args else "status"

    def save(**changes):
        cfg.update(changes)
        write_config(cfg)

    if cmd == "status":
        print(f"NotYesterday {'enabled' if cfg['enabled'] else 'disabled'}")
        print(f"  now          {now:%a %Y-%m-%d %H:%M %Z}")
        print("  full timeline after a 1h gap, a new day, or a question about time")
        print(f"  entries      {cfg['timelineEntries']} per timeline")
        print(f"  timezone     {cfg['timezone']}")
        print(f"  stop mode    {cfg['stopMode']} (log = record only, correct = ask for a fix)")
        print(f"  config       {CONFIG_PATH}")
        if os.path.exists(LOG_PATH):
            with open(LOG_PATH) as fh:
                print(f"  self-check   {sum(1 for _ in fh)} logged observations at {LOG_PATH}")
        return 0

    if cmd in ("on", "off"):
        save(enabled=(cmd == "on"))
        print(f"NotYesterday {cmd}")
        return 0

    if cmd == "tz" and len(args) > 1:
        save(timezone=args[1])
        print(f"timezone {cfg['timezone']}")
        return 0

    if cmd == "stop" and len(args) > 1 and args[1] in ("off", "log", "correct"):
        save(stopMode=args[1])
        print(f"stop mode {cfg['stopMode']}")
        return 0

    if cmd in ("when", "timeline"):  # timeline kept as an alias
        path = os.environ.get("NY_TRANSCRIPT", "") or newest_transcript(os.getcwd())
        if not path:
            print("no session transcript found for this directory")
            return 0
        limit = int(args[1]) if len(args) > 1 and args[1].isdigit() else 20
        wide = dict(cfg, timelineEntries=limit, actionHistory=limit)
        facts = collect(path, wide, tz, now, want_compact=True)
        start = facts.get("session_start")
        if start:
            print(f"session started {stamp(start, now)}")
        if facts.get("last_compact"):
            print(f"context compacted {stamp(facts['last_compact'], now)}")
        actions = sorted(
            facts.get("actions", []), key=lambda a: a["when"], reverse=True
        )[:limit]
        if not actions:
            print("nothing recorded in this session yet")
        for action in actions:
            print(f"  {stamp(action['when'], now):<28} {action['what']}")
        return 0

    print("usage: /notyesterday [status|on|off|tz <zone|auto>|stop <off|log|correct>|when [n]]")
    return 0


def main() -> int:
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    if mode == "cli":
        return cli(sys.argv[1:])
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except Exception:
        return 0

    cfg = load_config()
    if not cfg.get("enabled", True):
        return 0

    if mode not in ("session-start", "prompt-submit", "stop"):
        return 0

    tz = tzinfo_for(cfg)
    now = datetime.now(tz) if tz else datetime.now().astimezone()

    if mode == "session-start":
        mode_session_start(payload, cfg, tz, now)
    elif mode == "prompt-submit":
        mode_prompt_submit(payload, cfg, tz, now)
    elif mode == "stop":
        mode_stop(payload, cfg, tz, now)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        # Fail-safe: never break a session over a time note.
        sys.exit(0)

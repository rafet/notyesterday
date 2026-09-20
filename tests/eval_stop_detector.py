"""Offline evaluation of the Stop detector against your own real sessions.

    python3 tests/eval_stop_detector.py

It reads ~/.claude/projects/*/*.jsonl, never writes anything, and prints how
often the detector would have fired and on what. Run it before turning
`/notyesterday stop correct` on.

Original note: offline evaluation: how often would the Stop detector fire on real sessions,
and how often would it be right? No blocking, no plugin — just the same code."""
import glob, json, os, sys
from datetime import datetime

sys.path.insert(0, os.path.expanduser("~/Projects/notyesterday/scripts"))
import notyesterday as ny

tz = ny.tzinfo_for({})
files = sorted(glob.glob(os.path.expanduser("~/.claude/projects/*/*.jsonl")), key=os.path.getsize, reverse=True)
assistant_msgs = claims = wrong = 0
samples = []

for path in files:
    edits = []  # (when, basename) seen so far in this transcript
    for line in open(path, errors="ignore"):
        if '"type":"assistant"' not in line:
            continue
        try:
            entry = json.loads(line)
        except Exception:
            continue
        when = ny.parse_ts(entry.get("timestamp"), tz)
        blocks = (entry.get("message") or {}).get("content") or []
        text = " ".join(b.get("text", "") for b in blocks if isinstance(b, dict) and b.get("type") == "text")
        for b in blocks:
            if isinstance(b, dict) and b.get("type") == "tool_use" and b.get("name") in ny.EDIT_TOOLS:
                target = (b.get("input") or {}).get("file_path")
                if target and when:
                    edits.append({"target": target, "when": when})
        if not text.strip():
            continue
        assistant_msgs += 1
        found = ny.find_claims(text, "tr")  # both languages, the widest net
        if not found:
            continue
        claims += 1
        facts = {"edits": list(reversed(edits[-8:]))}
        verdicts = ny.check_claims({"last_assistant_message": text}, facts, {"language": "tr"}, when or datetime.now(tz))
        hits = [v for v in verdicts if v.get("wrong")]
        if hits:
            wrong += 1
            if len(samples) < 12:
                samples.append((os.path.basename(path)[:8], hits[0]["phrase"], hits[0]["actual_elapsed"], hits[0]["sentence"][-150:]))

print(f"assistant messages with text : {assistant_msgs}")
print(f"  containing a time claim    : {claims} ({100*claims/max(assistant_msgs,1):.2f}%)")
print(f"  flagged as wrong           : {wrong} ({100*wrong/max(assistant_msgs,1):.3f}% of messages)")
print()
for name, phrase, real, sentence in samples:
    print(f"[{name}] claimed {phrase!r} but the edit was {real} ago")
    print(f"    …{sentence.strip()}")

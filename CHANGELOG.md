# Changelog

## 0.1.0 — unreleased

- SessionStart anchor: current local time plus the rule that NotYesterday lines
  outrank guesses; on resume/compact/fork also a timeline recovered from the
  transcript (session start, compaction boundary, last activity, recent edits).
- UserPromptSubmit: an interval line on every turn, plus mtime for files named
  in the prompt. A question about when something happened ("when did you…",
  "ne zaman…") answers with the session timeline instead.
- Stop self-check in `log` mode: records temporal claims and their real elapsed
  time to ~/.claude/notyesterday-log.jsonl without injecting anything. Correction
  mode is opt-in.
- A time question also pulls edits from this project's other session transcripts
  and the last few git commits, so "last week" is answerable.
- No scan-size setting: the transcript reader walks backwards until it has what
  it needs, under a 2s budget.
- A file named in the prompt is reported with who changed it: an edit this
  session made, or a change on disk that it did not.
- `/notyesterday` for status and settings, `/notyesterday when` for the
  session timeline on demand.
- `tests/eval_stop_detector.py` measures the self-check against your own
  transcripts: 4252 assistant messages here produced 19 time claims and zero
  corrections, which is why correction stays opt-in.
- Elapsed time is computed through UTC, so DST switches do not shift results.
- A note becomes a full timeline after an hour, or as soon as the day changes.

---
description: Show or change NotYesterday settings, or print the session timeline
allowed-tools: Bash(bash:*)
argument-hint: status | on | off | stop log|correct|off | tz <zone> | when [n]
---

Run the NotYesterday CLI and report its output verbatim. Do not add your own
estimates of elapsed time — the point of this plugin is that the script does the
arithmetic, not you.

Arguments given by the user: `$ARGUMENTS` (empty means `status`).

Run:

```
bash "$CLAUDE_PLUGIN_ROOT/scripts/cli.sh" $ARGUMENTS
```

Accepted arguments:

- `status` — current settings, config path and how many self-check observations are logged
- `on` / `off` — enable or disable every hook at once
- `tz <IANA zone|auto>` — timezone used for day boundaries
- `stop <off|log|correct>` — the self-check: record only (default), or ask for a correction
- `when [n]` — print the session's edits and compaction points, with elapsed times
  (`timeline` still works as the old name)

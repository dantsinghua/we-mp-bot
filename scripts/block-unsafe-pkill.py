#!/usr/bin/env python3
"""PreToolUse(Bash) guard: block raw `pkill -f` / `pkill --full` (self-kill risk).

The recurring bug: `pkill -f <pattern>` matches the executing shell's own
command line (the pattern is literally in it), killing the shell — exit 144,
half-done operations. Memory notes didn't stop it recurring, so this is a
machine-enforced guard: any Bash command using full-cmdline pkill is blocked
with instructions to use `safe-pkill`, which excludes the shell + ancestors.

Exit 0 = allow. Exit 2 = block (stderr shown to the model).
Fails open (exit 0) on any parse error — never wedge the session.
"""
import json
import re
import sys

try:
    data = json.load(sys.stdin)
except Exception:
    sys.exit(0)

if data.get("tool_name") != "Bash":
    sys.exit(0)

cmd = (data.get("tool_input") or {}).get("command", "") or ""

# Two self-kill routes to block (both have bitten us):
#   1. `pkill -f` / `pkill --full`  — full-cmdline match kills this shell.
#   2. `pgrep -f <pat>` feeding `kill` (pipe/xargs/loop) — pgrep -f matches this
#      shell too, so the subsequent kill terminates it. (exit 144)
danger = False
reason = ""

for m in re.finditer(r"(?<![\w-])pkill\b([^\n;|&]*)", cmd):
    if re.search(r"(?:^|\s)-\w*f\b|\s--full\b", m.group(1)):
        danger, reason = True, "pkill -f"
        break

if not danger and re.search(r"(?<![\w-])pgrep\b[^\n]*\s-\w*f\b", cmd) \
        and re.search(r"(?<![\w-])kill\b", cmd):
    # pgrep -f ... together with a kill somewhere in the same command
    danger, reason = True, "pgrep -f | kill"

if danger:
    sys.stderr.write(
        f"BLOCKED: `{reason}` can kill THIS shell — a full-cmdline match "
        "(-f/--full) also matches the shell's own command line, causing "
        "exit-144 self-kills (has happened many times).\n"
        "Use `safe-pkill <pattern> [signal]` instead — a drop-in that excludes "
        "the calling shell and all ancestors.\n"
        "  e.g.  safe-pkill wemprss_ratelimit_watch\n"
        "        safe-pkill wechat_group_to_email KILL\n"
        "(Name-based `pkill <name>` without -f matches process names not full "
        "cmdlines and is allowed.)\n")
    sys.exit(2)

sys.exit(0)

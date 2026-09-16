#!/usr/bin/env python3
"""Records that Codex spawned the transport program, then becomes it.

Codex keeps the transport's stderr only at debug log level, so for the
`unreachable` scenario the real `ccnm internal exec-transport` runs behind
this one-line wrapper: it appends argv and pid to <log>, points stderr at
<log>.stderr, and execs the command unchanged.

argv: exec_logged.py <log> <program> [args...]
"""
import json
import os
import sys
import time

log, program, args = sys.argv[1], sys.argv[2], sys.argv[3:]
with open(log, "a") as f:
    f.write(json.dumps({"t": time.time(), "pid": os.getpid(), "ppid": os.getppid(), "program": program,
                        "args": args, "codex_home": os.environ.get("CODEX_HOME")}) + "\n")
fd = os.open(log + ".stderr", os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
os.dup2(fd, 2)
os.execv(program, [program] + args)

#!/usr/bin/env python3
"""ccnm P24 zero-quota gate, over the real ssh to hpsrv. No model is involved.

One exec-server session: the P21/P22 rule table's allow and refuse cases against
the real Linux executor (with bubblewrap), side effects checked on hpsrv's disk
rather than in the replies; P12.2's identity checks, both as the account itself
and from inside the executor's sandbox; then the guard and leftover checks after
the session ends.

usage: gate.py <out json>
"""

import json
import sys
import time

import native_client as nc

results = []


def check(name, ok, detail=""):
    results.append({"check": name, "ok": bool(ok), "detail": detail})
    print(("PASS " if ok else "FAIL ") + name + ("" if ok else f"  -- {detail}"))


def exists(path):
    return nc.remote(f"test -e {path} && echo yes || echo no").strip() == "yes"


IDENTITY_SCRIPT = r"""
echo "user=$(id -un)"; echo "groups=$(id -Gn)"
if sudo -n true 2>/dev/null; then echo sudo=yes; else echo sudo=no; fi
if [ -S /var/run/docker.sock ] && [ -w /var/run/docker.sock ]; then echo docker=writable; else echo docker=not-writable; fi
others=""; for f in "$HOME"/.ssh/*; do case "$(basename "$f")" in authorized_keys|known_hosts|known_hosts.old|'*') ;; *) others="$others $(basename "$f")";; esac; done
echo "ssh_other=${others:-none}"
echo "ssh_auth_sock=${SSH_AUTH_SOCK:-unset}"
if ls /home/bing >/dev/null 2>&1; then echo other_home=readable; else echo other_home=unreadable; fi
echo "cred_env=$(env | cut -d= -f1 | grep -iE 'OPENAI|ANTHROPIC|TOKEN|SECRET|API_KEY|SSH_AUTH' | tr '\n' ' ')"
echo "codex_home=${CODEX_HOME:-unset}"
if [ -n "${CODEX_HOME:-}" ]; then echo "codex_home_entries=$(ls -A "$CODEX_HOME" 2>/dev/null | tr '\n' ' ')"; fi
echo "marker=${CCNM_EXEC_SESSION:+present}"
"""


def parse(text):
    return dict(line.split("=", 1) for line in text.splitlines() if "=" in line)


def identity_ok(values, where):
    check(f"{where}: runs as ccrun", values.get("user") == "ccrun", values)
    check(f"{where}: only its own group", values.get("groups") == "ccrun", values)
    check(f"{where}: no sudo", values.get("sudo") == "no", values)
    check(f"{where}: docker socket not writable", values.get("docker") == "not-writable", values)
    check(f"{where}: no private key candidates in ~/.ssh", values.get("ssh_other") == "none", values)
    check(f"{where}: no SSH agent", values.get("ssh_auth_sock") == "unset", values)
    check(f"{where}: another account's home unreadable", values.get("other_home") == "unreadable", values)
    check(f"{where}: no credential-shaped environment names", values.get("cred_env", "").strip() == "", values)


def main():
    out = sys.argv[1]
    nc.remote(f"rm -f {nc.ROOT}/gate-*.txt {nc.OUTSIDE}/*")

    account = nc.remote(IDENTITY_SCRIPT)
    identity_ok(parse(account), "account")
    results.append({"check": "account: raw identity output", "ok": True, "detail": account})

    s = nc.Session("exec-serve")
    init = s.handshake()
    info = (init or {}).get("result", {}).get("environmentInfo", {})
    check("handshake through exec-serve", bool(info), init)
    environment = {k: info.get(k) for k in ("platformOs", "executorVersion", "providerId", "shell")}

    held = nc.guard_state()
    check("guard held by this session while it is open", held.startswith(f"held {s.session} p24"), held)
    procs = nc.remote("ps -u ccrun -o pid=,args= | grep -E 'internal exec-serve|exec-server --listen' | grep -v grep")
    check("supervisor and executor run as ccrun", "internal exec-serve" in procs and "exec-server --listen stdio" in procs, procs)

    reply, code, text = s.run(1, "id -un; pwd; echo gate > gate-inside.txt; ls -a")
    check("sandboxed command runs as ccrun in the workspace", code == 0 and "ccrun" in text and nc.ROOT in text, {"exit": code, "output": text})
    check("its write landed in the workspace", exists(f"{nc.ROOT}/gate-inside.txt"))

    reply, code, text = s.run(2, f"echo x > {nc.OUTSIDE}/sandbox-escape.txt; echo rc=$?")
    check("bubblewrap keeps the sandboxed command out of other directories",
          "rc=0" not in text and not exists(f"{nc.OUTSIDE}/sandbox-escape.txt"), {"exit": code, "output": text})

    reply = s.call(nc.start(3, f"echo escalated > {nc.OUTSIDE}/escalated.txt", sandbox=False))
    time.sleep(1)
    check("escalated command (sandbox null) refused -32600", nc.error_code(reply) == -32600, reply)
    check("and it did not run", not exists(f"{nc.OUTSIDE}/escalated.txt"))

    reply = s.call(nc.write_file(4, f"{nc.ROOT}/gate-patched.txt", "hello\n"))
    check("sandboxed fs/writeFile inside the workspace", reply and "result" in reply, reply)
    check("with the bytes on disk", nc.remote(f"cat {nc.ROOT}/gate-patched.txt 2>/dev/null") == "hello\n")

    reply = s.call(nc.fixture("fs-write-file-approved-outside.json", 5))
    check("approved out-of-workspace write refused -32600", nc.error_code(reply) == -32600, reply)
    check("and nothing written", not exists(f"{nc.OUTSIDE}/patched-outside.txt"))

    reply = s.call({"id": 6, "method": "http/request", "params": {"method": "GET", "url": "https://example.com/"}})
    check("http/request refused -32600", nc.error_code(reply) == -32600, reply)

    reply = s.call({"id": 7, "method": "fs/getMetadata", "params": {"path": "file:///home/ccrun/.git", "sandbox": None}})
    check("ancestor .git answered as not found -32004", nc.error_code(reply) == -32004, reply)

    reply = s.call({"id": 8, "method": "fs/readFile", "params": {"path": "file:///etc/hostname", "sandbox": None}})
    check("read outside the workspace refused -32600", nc.error_code(reply) == -32600, reply)

    reply, code, text = s.run(9, IDENTITY_SCRIPT)
    values = parse(text)
    identity_ok(values, "inside the executor")
    results.append({"check": "inside the executor: raw identity output", "ok": True, "detail": text})
    check("inside the executor: CODEX_HOME holds no credential file",
          "auth.json" not in values.get("codex_home_entries", "") and values.get("codex_home", "unset") != "unset", values)
    check("inside the executor: session marker inherited", values.get("marker") == "present", values)

    rc, stderr = s.close()
    check("session closes cleanly (exit 0)", rc == 0, stderr[-400:])
    time.sleep(1)
    after = nc.guard_state()
    check("guard released after close", after == "released", after)
    left = nc.marked_processes()
    check("no process with a session marker left", not left, left)
    homes = nc.remote("ls -A ~/.local/state/ccnm/exec-server 2>/dev/null | wc -l").strip()
    check("the session's generated CODEX_HOME removed", homes == "0", homes)

    nc.remote(f"rm -f {nc.ROOT}/gate-*.txt; git -C {nc.ROOT} status --short")
    summary = {"environment": environment, "session": s.session, "passed": sum(r["ok"] for r in results),
               "failed": sum(not r["ok"] for r in results), "checks": results}
    open(out, "w").write(json.dumps(summary, indent=2, ensure_ascii=False, default=str))
    print(json.dumps({k: summary[k] for k in ("environment", "passed", "failed")}, ensure_ascii=False))
    sys.exit(1 if summary["failed"] else 0)


if __name__ == "__main__":
    main()

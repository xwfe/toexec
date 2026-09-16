#!/usr/bin/env python3
"""ccnm P23.4: real Codex 0.154.0 + real `ccnm internal exec-serve` + real
`codex exec-server`, end to end, with zero model spend.

Topology (one machine):

    Codex (TUI, no-egress Seatbelt, fresh HOME, per-session CODEX_HOME)
      └─ spawns `program` from CODEX_HOME/environments.toml
           = relay_client.py ── TCP 127.0.0.1 ──> harness (outside the sandbox)
                                                   └─ ccnm internal exec-serve --payload <protocol 6>
                                                        └─ codex exec-server --listen stdio

The TCP hop stands in for ssh: there is no second machine here, no local
sshd key, and an exec-server started inside Codex's Seatbelt cannot apply its
own sandbox (measured in the spike). Everything else is the product path:
the environments.toml shape ccnm writes, the Codex flags ccnm passes, the
Runtime's exec-serve with its rule table, the real executor.

usage: harness.py <scenario> <new out dir>
scenarios: basic | refused | drop | unreachable
env: P23_CCNM (default ../../../../ccnm/target/debug/ccnm), P23_CODEX
"""
import base64
import json
import os
import shlex
import socket
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "native-surface"))
from probe import CCNM_DISABLED, NATIVE_KEEP, SANDBOX_PROFILE, Log, MockModel  # noqa: E402

CODEX = os.environ.get("P23_CODEX", "/opt/homebrew/bin/codex")
CCNM = os.environ.get("P23_CCNM", str((HERE / "../../../../ccnm/target/debug/ccnm").resolve()))
PYTHON = os.path.realpath(sys.executable)
IDENTITY = {"node": "agent", "instance": "codex-main", "provider": "codex", "profile_ref": "default"}


def b64url(obj):
    return base64.urlsafe_b64encode(json.dumps(obj, separators=(",", ":")).encode()).decode().rstrip("=")


# ---------------------------------------------------------------- Runtime side (outside the sandbox)

class RuntimeRelay:
    """Accepts TCP connections and, per connection, runs one `ccnm internal
    exec-serve` with the connection as its stdio. Logs both directions."""

    def __init__(self, out, log, config, session):
        self.out, self.log, self.config, self.session = out, log, config, session
        self.connections, self.children = 0, []
        self.server = socket.socket()
        self.server.bind(("127.0.0.1", 0))
        self.server.listen(8)
        self.port = self.server.getsockname()[1]
        threading.Thread(target=self.accept, daemon=True).start()

    def wire(self):
        return b64url({"protocol": 6, "workspace": "demo", "agent": IDENTITY, "session": self.session})

    def accept(self):
        while True:
            try:
                conn, _ = self.server.accept()
            except OSError:
                return
            self.connections += 1
            threading.Thread(target=self.serve, args=(conn, self.connections), daemon=True).start()

    def serve(self, conn, n):
        env = {"PATH": os.environ["PATH"], "HOME": str(self.out / "runtime" / "home"),
               "XDG_STATE_HOME": str(self.out / "runtime" / "state"), "CCNM_CONFIG": str(self.config),
               "CCNM_LOG": "info"}
        child = subprocess.Popen([CCNM, "internal", "exec-serve", "--payload", self.wire()], env=env,
                                 stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                 stderr=open(self.out / f"exec-serve-{n}.stderr", "ab"))
        self.children.append(child)
        self.log(side="runtime-relay", event="connection", n=n, exec_serve_pid=child.pid)

        def c2s():
            f = conn.makefile("rb")
            for line in f:
                self.log(side="runtime-relay", n=n, dir="c2s", msg=parse(line))
                try:
                    child.stdin.write(line)
                    child.stdin.flush()
                except (BrokenPipeError, ValueError):
                    break
            self.log(side="runtime-relay", n=n, dir="c2s", event="socket-eof")
            try:
                child.stdin.close()
            except Exception:
                pass

        def s2c():
            for line in child.stdout:
                self.log(side="runtime-relay", n=n, dir="s2c", msg=parse(line))
                try:
                    conn.sendall(line)
                except OSError:
                    break
            self.log(side="runtime-relay", n=n, dir="s2c", event="stdout-eof")
            try:
                conn.close()
            except OSError:
                pass

        threading.Thread(target=c2s, daemon=True).start()
        threading.Thread(target=s2c, daemon=True).start()
        child.wait()
        self.log(side="runtime-relay", event="exec-serve-exited", n=n, rc=child.returncode)

    def close(self):
        self.server.close()
        deadline = time.time() + 20
        for child in self.children:
            while child.poll() is None and time.time() < deadline:
                time.sleep(0.2)
        return [c.returncode for c in self.children]


def parse(line):
    try:
        return json.loads(line)
    except Exception:
        return {"raw": line.decode(errors="replace")}


# ---------------------------------------------------------------- setup

def write_runtime_config(out, work):
    """What the Runtime's config.toml says. `allow_unconfined_exec` and a
    credential-free HOME are what the exec_serve integration tests use too:
    the developer account running this would otherwise fail the audit."""
    runtime = out / "runtime"
    (runtime / "home").mkdir(parents=True)
    (runtime / "state").mkdir()
    config = runtime / "config.toml"
    config.write_text(f'''this = "runtime"

[nodes.runtime]
codex_bin = "{CODEX}"

[nodes.agent]
ssh = "agent-node.invalid"

[workspaces.demo]
root = "{work}"
agent = {{ node = "agent", instance = "codex-main" }}
allow_unconfined_exec = true
codex_exec_server = true
''')
    return config


def write_session_home(out, work, program, args):
    """The per-session CODEX_HOME in the shape ccnm's `provider::codex::native`
    writes (pinned by its unit tests): the one environment, the login linked
    from the profile, the Runtime root trusted."""
    profile = out / "profile"
    profile.mkdir()
    profile.chmod(0o700)
    auth = profile / "auth.json"
    auth.write_text(json.dumps({"OPENAI_API_KEY": "sk-dummy-profile-key"}))
    auth.chmod(0o600)
    home = out / "session-home"
    home.mkdir()
    home.chmod(0o700)
    os.symlink(auth, home / "auth.json")
    (home / "environments.toml").write_text(
        'default = "ccnm"\ninclude_local = false\n\n[[environments]]\nid = "ccnm"\n'
        f'program = {json.dumps(program)}\nargs = [{", ".join(json.dumps(a) for a in args)}]\n')
    (home / "config.toml").write_text(f'[projects."{work}"]\ntrust_level = "trusted"\n')
    return home


def write_unreachable_session(out, work):
    """A real session record for `ccnm internal exec-transport`, bound to a
    Runtime alias that resolves nowhere."""
    sid = str(uuid.uuid4())
    sdir = out / "sessions" / sid
    sdir.mkdir(parents=True)
    sdir.chmod(0o700)
    spec = {"protocol": 3, "agent_identity": IDENTITY, "provider": "codex", "id": sid, "workspace": "demo",
            "root": str(work), "runtime_node": "runtime",
            "runtime": {"alias": "never-connect.invalid", "ccnm_bin": "/opt/ccnm/bin/ccnm"},
            "claude_config_dir": None, "permission_mode": "acceptEdits",
            "mode": {"mode": "interactive", "prompt": None}, "timeout_secs": 0,
            "cwd": str(out / "local-cwd"), "codex_exec_server": True}
    (sdir / "session.json").write_text(json.dumps(spec))
    request = b64url({"protocol": 3, "identity": IDENTITY, "session_dir": str(sdir)})
    return ["internal", "exec-transport", "--payload", request]


# ---------------------------------------------------------------- Codex

def codex_args(model_port, work):
    """The flags ccnm's native launch builds (provider::codex::build_native_launch_cmd),
    plus the fake model provider."""
    p = "p23mock"
    args = ["--no-alt-screen", "--sandbox", "workspace-write", "-c", 'approval_policy="on-request"',
            "-c", 'web_search="disabled"', "-c", "agents.enabled=false"]
    for feature in CCNM_DISABLED:
        if feature not in NATIVE_KEEP:
            args += ["--disable", feature]
    args += ["-c", f'model_provider="{p}"', "-c", f'model_providers.{p}.name="{p}"',
             "-c", f'model_providers.{p}.base_url="http://127.0.0.1:{model_port}/v1"',
             "-c", f'model_providers.{p}.wire_api="responses"',
             "-c", f"model_providers.{p}.requires_openai_auth=false",
             "-C", str(work)]
    return args


def process_tree(root_pid):
    ps = subprocess.run(["ps", "-axo", "pid=,ppid="], capture_output=True, text=True).stdout
    children = {}
    for line in ps.splitlines():
        pid, ppid = line.split()
        children.setdefault(int(ppid), []).append(int(pid))
    tree, todo = [], [root_pid]
    while todo:
        pid = todo.pop()
        tree.append(pid)
        todo += children.get(pid, [])
    return tree


def listening_sockets(pids):
    """`lsof` for the given processes: TCP sockets in LISTEN. Exit 1 means none."""
    r = subprocess.run(["lsof", "-nP", "-a", "-p", ",".join(map(str, pids)), "-iTCP", "-sTCP:LISTEN"],
                       capture_output=True, text=True)
    return [l for l in r.stdout.splitlines()[1:]]


def run_tui(out, log, model, work, home, program_args, approval, timeout=150):
    profile = out / "no-egress.sb"
    profile.write_text(SANDBOX_PROFILE)
    env = {"HOME": str(out / "userhome"), "CODEX_HOME": str(home), "PATH": os.environ["PATH"],
           "TERM": "xterm-256color"}
    inner = ["env", "-i"] + [f"{k}={v}" for k, v in env.items()]
    inner += ["sandbox-exec", "-f", str(profile), CODEX] + codex_args(model.port, work) + ["--", "run the probe"]
    local_cwd = out / "local-cwd"
    local_cwd.mkdir(exist_ok=True)
    sock = f"p23-{os.getpid()}"
    log(side="harness", event="codex-tui", argv=inner)
    subprocess.run(["tmux", "-L", sock, "new-session", "-d", "-s", "s", "-x", "200", "-y", "60",
                    "-c", str(local_cwd), shlex.join(inner) + "; echo CODEX_EXITED=$?; sleep 600"], check=True)
    pane_pid = int(subprocess.run(["tmux", "-L", sock, "list-panes", "-t", "s", "-F", "#{pane_pid}"],
                                  capture_output=True, text=True).stdout.strip())
    panes, approvals, trust_prompts, finished_at, listen_samples = [], 0, 0, None, []
    deadline = time.time() + timeout
    try:
        while time.time() < deadline:
            time.sleep(2)
            pane = subprocess.run(["tmux", "-L", sock, "capture-pane", "-p", "-t", "s", "-S", "-200"],
                                  capture_output=True, text=True).stdout
            panes.append(pane)
            if "CODEX_EXITED=" in pane:
                break
            if "Do you trust the contents of this directory" in pane and trust_prompts == 0:
                trust_prompts += 1
                subprocess.run(["tmux", "-L", sock, "send-keys", "-t", "s", "Enter"], check=True)
                continue
            tail = pane[-1500:]
            if "Would you like to" in tail and "Press enter to confirm" in tail:
                approvals += 1
                (out / f"tui-approval-{approvals}.txt").write_text(tail)
                key = "y" if approval == "accept" else "Escape"
                log(side="harness", event="approval-prompt", answer=key)
                subprocess.run(["tmux", "-L", sock, "send-keys", "-t", "s", key], check=True)
                time.sleep(1)
                continue
            if model.step >= 1 and len(listen_samples) < 2:
                tree = process_tree(pane_pid)
                listen_samples.append({"pids": len(tree), "listening": listening_sockets(tree)})
            if model.done.is_set():
                finished_at = finished_at or time.time()
                if time.time() - finished_at > 6:
                    break
    finally:
        (out / "tui-pane.txt").write_text(panes[-1] if panes else "")
        subprocess.run(["tmux", "-L", sock, "kill-server"], capture_output=True)
    return {"model_steps": model.step, "plan_done": model.done.is_set(), "approval_prompts": approvals,
            "trust_prompts": trust_prompts, "listen_samples": listen_samples,
            "pane_tail": (panes[-1] if panes else "")[-1500:]}


# ---------------------------------------------------------------- what happened

def model_outputs(out):
    got = []
    for n in range(1, 12):
        p = out / f"model-request-{n}.json"
        if not p.exists():
            break
        body = json.loads(p.read_text())
        outs = [i for i in body.get("input", []) if isinstance(i, dict) and "output" in i]
        got.append(str(outs[-1].get("output"))[:500] if outs else None)
    return got


def relay_messages(out):
    msgs = []
    for f in sorted(out.glob("agent-relay-*.jsonl")):
        if f.name.endswith("spawns.jsonl"):
            continue
        msgs += [json.loads(l) for l in f.read_text().splitlines()]
    return msgs


def harness_messages(out):
    return [json.loads(l) for l in (out / "harness.jsonl").read_text().splitlines()]


def summarize_rpc(harness_log):
    """From the Runtime relay's view: what Codex asked, what came back."""
    c2s = [m["msg"] for m in harness_log if m.get("side") == "runtime-relay" and m.get("dir") == "c2s" and "msg" in m]
    s2c = [m["msg"] for m in harness_log if m.get("side") == "runtime-relay" and m.get("dir") == "s2c" and "msg" in m]
    by_id = {m.get("id"): m for m in c2s if "id" in m}
    refusals = []
    for m in s2c:
        err = m.get("error") or {}
        if err.get("code") in (-32600, -32601):
            req = by_id.get(m.get("id"), {})
            params = req.get("params") or {}
            refusals.append({"method": req.get("method"), "code": err.get("code"), "message": err.get("message"),
                             "sandbox_null": "sandbox" in params and params.get("sandbox") is None})
    starts = [m["params"] for m in c2s if m.get("method") == "process/start"]
    by_code = {}
    for m in s2c:
        code = (m.get("error") or {}).get("code")
        if code is not None:
            by_code[str(code)] = by_code.get(str(code), 0) + 1
    return {
        "methods": sorted({m.get("method") for m in c2s if m.get("method")}),
        "replies_by_code": by_code,
        "process_starts": [{"argv": s.get("argv"), "cwd": s.get("cwd"), "sandbox_null": s.get("sandbox") is None}
                           for s in starts],
        "refusals": refusals,
        "connections": [m for m in harness_log if m.get("side") == "runtime-relay" and m.get("event") in ("connection", "exec-serve-exited")],
    }


def guard_markers(out):
    d = out / "runtime" / "state" / "ccnm" / "write-guards"
    if not d.exists():
        return {}
    return {p.name: p.read_text()[:120] for p in d.iterdir()}


def exec_serve_stderr(out):
    text = ""
    for f in sorted(out.glob("exec-serve-*.stderr")):
        text += f.read_text(errors="replace")
    return text


def listdir(p):
    return sorted(x.name for x in p.iterdir()) if p.exists() else None


# ---------------------------------------------------------------- scenarios

PROBE_CMD = "sleep 1; pwd; id -un; echo hi > inside.txt; cat AGENTS.md; ls"
PATCH = "*** Begin Patch\n*** Add File: patched.txt\n+hello from apply_patch\n*** End Patch"
BASIC_PLAN = [("exec", PROBE_CMD), ("patch", PATCH), ("exec", "echo AFTER > after.txt; echo rc=$?"), ("say", "done")]
# No patch after the drop: a patch that cannot be written makes Codex ask
# "command failed; retry without sandbox?", and answering that is a scenario of
# its own (seen once in development; noted in the README).
DROP_PLAN = [("exec", PROBE_CMD), ("exec", "echo AFTER > after.txt; echo rc=$?"), ("say", "done")]


def escalate_plan(outside):
    return [
        ("js", "const r = await tools.exec_command({cmd: " + json.dumps(f"echo escalated > {outside}/esc.txt; echo rc=$?")
         + ", sandbox_permissions: 'require_escalated', justification: 'p23 probe'});\ntext(JSON.stringify(r));"),
        ("patch", f"*** Begin Patch\n*** Add File: {outside}/patched-outside.txt\n+outside\n*** End Patch"),
        ("exec", "echo STILL_ALIVE > alive.txt; echo rc=$?"),
        ("say", "done"),
    ]


def scenario(name, out):
    out.mkdir(parents=True)
    work = out / "work"
    work.mkdir()
    (work / "AGENTS.md").write_text("REMOTE-AGENTS-MARKER\n")
    outside = out / "outside"
    outside.mkdir()
    (out / "userhome").mkdir()
    log = Log(out / "harness.jsonl")
    config = write_runtime_config(out, work)
    session = str(uuid.uuid4())
    relay = RuntimeRelay(out, log, config, session)

    mode = "drop" if name == "drop" else "basic"
    if name == "unreachable":
        program = PYTHON
        args = [str(HERE / "exec_logged.py"), str(out / "transport.log"), CCNM] + write_unreachable_session(out, work)
    else:
        program, args = PYTHON, [str(HERE / "relay_client.py"), str(out), mode, str(relay.port)]
    home = write_session_home(out, work, program, args)

    plan = {"refused": escalate_plan(outside), "drop": DROP_PLAN}.get(name, BASIC_PLAN)
    approval = "accept" if name == "refused" else "decline"
    model = MockModel(out, plan, log)
    try:
        result = run_tui(out, log, model, work, home, args, approval)
        time.sleep(1)
    finally:
        exec_serve_rcs = relay.close()
    spawns = (out / "agent-relay-spawns.jsonl")
    spawn_count = len(spawns.read_text().splitlines()) if spawns.exists() else 0
    hlog = harness_messages(out)
    summary = {
        "scenario": name, "session": session, **result,
        "runtime_connections": relay.connections, "exec_serve_exit_codes": exec_serve_rcs,
        "agent_relay_spawns": spawn_count,
        "agent_relay_events": [m for m in relay_messages(out) if "event" in m],
        **summarize_rpc(hlog),
        "work_files": listdir(work), "outside_files": listdir(outside), "local_cwd_files": listdir(out / "local-cwd"),
        "guard_markers_after": guard_markers(out),
        "exec_serve_stderr_refusals": exec_serve_stderr(out).count("exec-server request refused"),
        "exec_serve_stderr_tail": exec_serve_stderr(out)[-800:],
        "model_outputs": model_outputs(out),
        "auth_symlink_intact": os.path.islink(home / "auth.json"),
        "profile_files": listdir(out / "profile"),
        "transport_spawns": len((out / "transport.log").read_text().splitlines()) if (out / "transport.log").exists() else None,
        "transport_stderr": (out / "transport.log.stderr").read_text(errors="replace")[-600:] if (out / "transport.log.stderr").exists() else None,
        "transport_stderr_names_alias": ("never-connect.invalid" in (out / "transport.log.stderr").read_text(errors="replace")) if (out / "transport.log.stderr").exists() else None,
    }
    (out / "summary.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False, default=str))
    print(json.dumps(summary, indent=2, ensure_ascii=False, default=str)[:8000])


if __name__ == "__main__":
    name, out = sys.argv[1], Path(sys.argv[2]).resolve()
    if name not in ("basic", "refused", "drop", "unreachable"):
        raise SystemExit(f"unknown scenario {name}")
    scenario(name, out)

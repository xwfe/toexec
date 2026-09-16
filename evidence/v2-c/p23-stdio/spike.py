#!/usr/bin/env python3
"""Does Codex 0.154.0's TUI take a stdio `program` transport from
CODEX_HOME/environments.toml, and does a per-session CODEX_HOME with a symlinked
auth.json behave?  Zero model spend: fake Responses server, sandbox-exec denies
egress, fresh HOME/CODEX_HOME.

usage: spike.py <scenario> <new out dir>
scenarios: basic | basic-notrust | basic-configtrust | drop | login
"""
import json
import os
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "native-surface"))
from probe import CCNM_DISABLED, NATIVE_KEEP, SANDBOX_PROFILE, Log, MockModel  # noqa: E402

CODEX = "/opt/homebrew/bin/codex"
PYTHON = os.path.realpath(sys.executable)
HERE = Path(__file__).resolve().parent


def start_exec_server(out, work):
    """Outside Codex's no-egress sandbox: a nested Seatbelt cannot apply a second profile."""
    srvhome = out / "srvhome"
    (srvhome / ".codex").mkdir(parents=True)
    proc = subprocess.Popen([CODEX, "exec-server", "--listen", "ws://127.0.0.1:0"], cwd=work,
                            env={"HOME": str(srvhome), "CODEX_HOME": str(srvhome / ".codex"), "PATH": os.environ["PATH"]},
                            stdout=subprocess.PIPE, stderr=open(out / "exec-server.stderr", "ab"), text=True)
    url = proc.stdout.readline().strip()
    assert url.startswith("ws://"), url
    return proc, url


def prepare(out, mode, config_trust=False):
    work = out / "work"
    work.mkdir(parents=True)
    server, url = start_exec_server(out, work)
    (work / "AGENTS.md").write_text("SPIKE-AGENTS-MARKER\n")
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
    toml = (
        'default = "ccnm"\n'
        "include_local = false\n\n"
        "[[environments]]\n"
        'id = "ccnm"\n'
        f'program = "{PYTHON}"\n'
        f'args = [{json.dumps(str(HERE / "spike_relay.py"))}, {json.dumps(str(out))}, {json.dumps(mode)}, {json.dumps(url)}]\n'
    )
    (home / "environments.toml").write_text(toml)
    if config_trust:
        (home / "config.toml").write_text(f'[projects."{work}"]\ntrust_level = "trusted"\n')
    (out / "userhome").mkdir()
    return work, home, server


def codex_args(model_port):
    p = "spikemock"
    args = ["--sandbox", "workspace-write", "-c", 'web_search="disabled"', "-c", "agents.enabled=false",
            "-c", f'model_provider="{p}"', "-c", f'model_providers.{p}.name="{p}"',
            "-c", f'model_providers.{p}.base_url="http://127.0.0.1:{model_port}/v1"',
            "-c", f'model_providers.{p}.wire_api="responses"',
            "-c", f"model_providers.{p}.requires_openai_auth=false"]
    for feature in CCNM_DISABLED:
        if feature not in NATIVE_KEEP:
            args += ["--disable", feature]
    return args


def run_tui(out, log, model, work, home, extra, timeout=90):
    profile = out / "no-egress.sb"
    profile.write_text(SANDBOX_PROFILE)
    env = {"HOME": str(out / "userhome"), "CODEX_HOME": str(home), "PATH": os.environ["PATH"],
           "TERM": "xterm-256color"}
    inner = ["env", "-i"] + [f"{k}={v}" for k, v in env.items()]
    inner += ["sandbox-exec", "-f", str(profile), CODEX, "--no-alt-screen", "-c", 'approval_policy="on-request"']
    inner += codex_args(model.port) + list(extra) + ["-C", str(work), "--", "run the probe"]
    local_cwd = out / "local-cwd"
    local_cwd.mkdir()
    sock = f"spike-{os.getpid()}"
    log(side="harness", event="codex-tui", argv=inner)
    subprocess.run(["tmux", "-L", sock, "new-session", "-d", "-s", "s", "-x", "200", "-y", "60",
                    "-c", str(local_cwd), shlex.join(inner) + "; echo CODEX_EXITED=$?; sleep 600"], check=True)
    panes, trust_prompts, finished_at = [], 0, None
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
                (out / "tui-trust-prompt.txt").write_text(pane)
                subprocess.run(["tmux", "-L", sock, "send-keys", "-t", "s", "Enter"], check=True)
                continue
            if model.done.is_set():
                finished_at = finished_at or time.time()
                if time.time() - finished_at > 6:
                    break
    finally:
        (out / "tui-pane.txt").write_text(panes[-1] if panes else "")
        subprocess.run(["tmux", "-L", sock, "kill-server"], capture_output=True)
    return {"model_steps": model.step, "plan_done": model.done.is_set(), "trust_prompts": trust_prompts,
            "pane_tail": (panes[-1] if panes else "")[-1200:]}


def model_outputs(out):
    """What the model was told after each tool call (tool outputs in the next request)."""
    got = []
    for n in range(1, 10):
        p = out / f"model-request-{n}.json"
        if not p.exists():
            break
        body = json.loads(p.read_text())
        outs = [i for i in body.get("input", []) if isinstance(i, dict) and "output" in i]
        got.append(str(outs[-1].get("output"))[:400] if outs else None)
    return got


def relay_log(out):
    spawns = [json.loads(l) for l in (out / "relay-spawns.jsonl").read_text().splitlines()] if (out / "relay-spawns.jsonl").exists() else []
    msgs = []
    for f in sorted(out.glob("relay-*.jsonl")):
        if f.name == "relay-spawns.jsonl":
            continue
        msgs += [json.loads(l) for l in f.read_text().splitlines()]
    return spawns, msgs


def summarize_rpc(all_msgs):
    msgs = [m for m in all_msgs if "msg" in m]
    init = next((m["msg"] for m in msgs if m.get("dir") == "c2s" and m["msg"].get("method") == "initialize"), None)
    starts = [m["msg"]["params"] for m in msgs if m.get("dir") == "c2s" and m["msg"].get("method") == "process/start"]
    return {
        "initialize_params_keys": sorted((init or {}).get("params", {}).keys()) if init else None,
        "resumeSessionId": (init or {}).get("params", {}).get("resumeSessionId", "<absent>") if init else None,
        "methods_c2s": sorted({m["msg"].get("method") for m in msgs if m.get("dir") == "c2s" and m["msg"].get("method")}),
        "process_starts": [{"argv": s.get("argv"), "cwd": s.get("cwd"), "sandbox_cwd": (s.get("sandbox") or {}).get("cwd"),
                            "workspaceRoots": (s.get("sandbox") or {}).get("workspaceRoots")} for s in starts],
        "events": [m for m in all_msgs if "event" in m],
    }


PLAN = [("exec", "sleep 1; pwd; id -un; echo hi > inside.txt; cat AGENTS.md; ls"),
        ("exec", "echo AFTER > after.txt; echo rc=$?"),
        ("say", "done")]


def scenario_tui(out, mode, extra, config_trust=False):
    work, home, server = prepare(out, mode, config_trust)
    log = Log(out / "harness.jsonl")
    model = MockModel(out, PLAN, log)
    try:
        result = run_tui(out, log, model, work, home, extra)
        time.sleep(1)
    finally:
        server.kill()
    spawns, msgs = relay_log(out)
    summary = {"scenario": sys.argv[1], **result, "relay_spawns": len(spawns),
               "relay_spawn_env_has_codex_home": [s.get("codex_home") for s in spawns],
               "relay_pgid_vs_ppid": [(s["pgid"], s["pid"], s["ppid"]) for s in spawns],
               **summarize_rpc(msgs),
               "inside_txt": (work / "inside.txt").exists(), "after_txt": (work / "after.txt").exists(),
               "model_outputs": model_outputs(out),
               "auth_symlink_intact": os.path.islink(home / "auth.json")}
    return summary


def scenario_login(out):
    work, home, server = prepare(out, "basic")
    server.kill()
    env = {"HOME": str(out / "userhome"), "CODEX_HOME": str(home), "PATH": os.environ["PATH"]}
    profile = out / "no-egress.sb"
    profile.write_text(SANDBOX_PROFILE)
    status1 = subprocess.run(["sandbox-exec", "-f", str(profile), CODEX, "login", "status"], env=env,
                             capture_output=True, text=True, timeout=30)
    login = subprocess.run(["sandbox-exec", "-f", str(profile), CODEX, "login", "--with-api-key"], env=env,
                           input="sk-dummy-NEW-key\n", capture_output=True, text=True, timeout=30)
    status2 = subprocess.run(["sandbox-exec", "-f", str(profile), CODEX, "login", "status"], env=env,
                             capture_output=True, text=True, timeout=30)
    real = (out / "profile" / "auth.json")
    return {"scenario": "login",
            "status_before": (status1.returncode, status1.stdout.strip(), status1.stderr.strip()[-300:]),
            "login": (login.returncode, login.stdout.strip(), login.stderr.strip()[-300:]),
            "status_after": (status2.returncode, status2.stdout.strip(), status2.stderr.strip()[-300:]),
            "session_auth_is_symlink": os.path.islink(home / "auth.json"),
            "profile_auth_contains_new_key": "sk-dummy-NEW-key" in real.read_text(),
            "profile_auth_mode": oct(real.stat().st_mode & 0o777),
            "session_home_entries": sorted(p.name for p in home.iterdir())}


def main():
    name, out = sys.argv[1], Path(sys.argv[2]).resolve()
    out.mkdir(parents=True)
    trust = ["-c", f'projects."{out / "work"}".trust_level="trusted"']
    if name == "basic":
        summary = scenario_tui(out, "basic", trust)
    elif name == "basic-notrust":
        summary = scenario_tui(out, "basic", [])
    elif name == "drop":
        summary = scenario_tui(out, "drop", trust)
    elif name == "basic-configtrust":
        summary = scenario_tui(out, "basic", [], config_trust=True)
    elif name == "login":
        summary = scenario_login(out)
    else:
        raise SystemExit(f"unknown scenario {name}")
    (out / "summary.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False, default=str))
    print(json.dumps(summary, indent=2, ensure_ascii=False, default=str))


if __name__ == "__main__":
    main()

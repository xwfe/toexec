#!/usr/bin/env python3
"""ccnm P33.1: what does Codex's workspace-write sandbox do to ordinary project work?

Every command runs twice: D, directly, the way ccnm's `exec_command` runs it
today; S, wrapped in `codex sandbox --sandbox-state-json <state> -- argv`
with the same permissions object Codex 0.154.0 sends for a workspace-write
command (captured in ccnm P21, the object `exec-serve` accepts). Same HOME,
same PATH, same cwd. The HOME is a fresh directory whose `.cargo` and
`.rustup` are symlinks to this account's, which is what a Runtime execution
account looks like after P12's provisioning (toolchain in its own home) --
except that writes through the symlinks land in the real directories, so the
sandbox had better refuse them.

Standard library only; no model, no network beyond a loopback probe.

usage: measure.py <name>      -> runs/<name>.json
env: P33_CODEX (default /opt/homebrew/bin/codex), P33_CAPTURED (the captured
     process/start fixture; default: the ccnm checkout next to this repo)
"""
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
CODEX = os.environ.get("P33_CODEX", "/opt/homebrew/bin/codex")


def _captured_default():
    """The ccnm checkout next to this repository; elsewhere, set P33_CAPTURED."""
    try:
        return HERE.parents[3] / "ccnm/tests/fixtures/codex-0.154.0/exec-server/process-start-workspace-write.json"
    except IndexError:
        return Path("/nonexistent/process-start-workspace-write.json")


CAPTURED = Path(os.environ.get("P33_CAPTURED", _captured_default()))
WORK = HERE / "work"
REAL_HOME = Path.home()


def sandbox_state(root, cwd):
    """`--sandbox-state-json`: the captured permissions object, root and cwd."""
    text = CAPTURED.read_text().replace("{ROOT}", str(root)).replace("{OUTSIDE}", "/nonexistent").replace("{SERVER_HOME}", "/nonexistent")
    sb = json.loads(text)["params"]["sandbox"]
    return {"permissionProfile": sb["permissions"], "sandboxCwd": f"file://{cwd}", "workspaceRoots": [f"file://{root}"]}


class Fixture:
    def __init__(self):
        if WORK.exists():
            shutil.rmtree(WORK)
        self.home = WORK / "home"
        self.home.mkdir(parents=True)
        for d in (".cargo", ".rustup"):
            if (REAL_HOME / d).exists():
                (self.home / d).symlink_to(REAL_HOME / d)
        (self.home / ".codex-ccnm").mkdir()  # what ccnm would hand codex sandbox as CODEX_HOME: empty
        (self.home / "canary.txt").write_text("home-canary\n")
        self.outside = WORK / "outside"
        self.outside.mkdir()
        (self.outside / "canary.txt").write_text("outside-canary\n")
        self.tmp = Path(tempfile.gettempdir()).resolve() / f"p33-{os.getpid()}"
        self.tmp.mkdir()
        self.root = WORK / "proj"
        self.root.mkdir()
        # An empty [workspace] table: this directory sits inside the toexec repository, and
        # cargo would otherwise claim the package for that workspace.
        (self.root / "Cargo.toml").write_text('[package]\nname = "p33"\nversion = "0.1.0"\nedition = "2021"\n\n[workspace]\n\n[dependencies]\nserde = "1"\n')
        (self.root / "src").mkdir()
        (self.root / "src" / "main.rs").write_text('fn main() { println!("{}", 6 * 7); }\n\n#[cfg(test)]\nmod t { #[test] fn adds() { assert_eq!(1 + 1, 2); } }\n')
        (self.root / "app.js").write_text('console.log(6 * 7);\n')
        (self.root / "README.md").write_text("p33\n")
        self.env = {"PATH": os.environ["PATH"], "HOME": str(self.home), "TMPDIR": str(self.tmp),
                    "CODEX_HOME": str(self.home / ".codex-ccnm"), "LANG": "C.UTF-8"}
        # Outside any sandbox: a lockfile and a warm registry cache, then a git history.
        self.direct(["cargo", "generate-lockfile"], check=True)
        self.direct(["cargo", "fetch"], check=True)
        self.direct(["git", "init", "-q"], check=True)
        self.direct(["git", "-c", "user.name=p33", "-c", "user.email=p33@invalid", "add", "-A"], check=True)
        self.direct(["git", "-c", "user.name=p33", "-c", "user.email=p33@invalid", "commit", "-qm", "init"], check=True)
        # A loopback listener the network probe tries to reach.
        self.listener = socket.socket()
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(4)
        self.port = self.listener.getsockname()[1]
        self.got = []
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self):
        while True:
            try:
                conn, _ = self.listener.accept()
            except OSError:
                return
            self.got.append(conn.recv(100))
            conn.close()

    def direct(self, argv, cwd=None, check=False, env=None, timeout=600):
        return subprocess.run(argv, cwd=str(cwd or self.root), env={**self.env, **(env or {})}, capture_output=True,
                              timeout=timeout, check=check)

    def sandboxed(self, argv, cwd=None, env=None, timeout=600, variant=None):
        cwd = cwd or self.root
        state = sandbox_state(self.root, cwd)
        if variant:
            variant(state["permissionProfile"])
        return subprocess.run([CODEX, "sandbox", "--sandbox-state-json", json.dumps(state), "--"] + argv, cwd=str(cwd),
                              env={**self.env, **(env or {})}, capture_output=True, timeout=timeout)

    def cleanup(self):
        self.listener.close()
        shutil.rmtree(self.tmp, ignore_errors=True)


def out(r, n=400):
    return {"rc": r.returncode, "stdout": r.stdout.decode(errors="replace")[-n:].strip(),
            "stderr": r.stderr.decode(errors="replace")[-n:].strip()}


def main():
    name = sys.argv[1]
    f = Fixture()
    results = {"versions": {}}
    for tool, argv in (("codex", [CODEX, "--version"]), ("cargo", ["cargo", "--version"]), ("rustc", ["rustc", "--version"]),
                       ("git", ["git", "--version"]), ("python3", ["python3", "--version"]), ("node", ["node", "--version"])):
        try:
            r = f.direct(argv)
            results["versions"][tool] = (r.stdout or r.stderr).decode(errors="replace").strip()
        except FileNotFoundError:
            results["versions"][tool] = None
    results["platform"] = subprocess.run(["uname", "-srm"], capture_output=True, text=True).stdout.strip()
    results["home_layout"] = {d: os.path.islink(f.home / d) for d in (".cargo", ".rustup")}

    cases = []

    def case(cid, kind, argv, env=None, cwd=None, check=None, note=None):
        row = {"id": cid, "kind": kind, "argv": argv if len(json.dumps(argv)) < 200 else argv[:3] + ["…"]}
        for label, runner in (("D", f.direct), ("S", f.sandboxed)):
            t = time.time()
            # `{label}` in argv and env is replaced per path, so D's side effects
            # never satisfy S's check. Each path compiles into its own target
            # dir, so S really compiles instead of finding D's build up to date.
            sub = lambda v: v.replace("{label}", label)  # noqa: E731
            r = runner([sub(a) for a in argv], cwd=cwd,
                       env={"CARGO_TARGET_DIR": str(f.root / f"target-{label}"), **{k: sub(v) for k, v in (env or {}).items()}})
            row[label] = out(r)
            row[label]["seconds"] = round(time.time() - t, 2)
            if check:
                row[label]["effect"] = bool(check(label, r))
        if note:
            row["note"] = note
        cases.append(row)
        print(cid, {k: (row[k]["rc"], row[k].get("effect")) for k in ("D", "S")}, flush=True)

    # --- legitimate project work
    case("L1-cargo-version", "legit", ["cargo", "--version"], check=lambda l, r: b"cargo" in r.stdout)
    case("L2-cargo-build-warm", "legit", ["cargo", "build"], check=lambda l, r: (f.root / f"target-{l}/debug/p33").exists())
    case("L3-cargo-test", "legit", ["cargo", "test"], check=lambda l, r: b"1 passed" in r.stdout)
    case("L4-cargo-run", "legit", ["cargo", "run", "-q"], check=lambda l, r: r.stdout.strip() == b"42")
    case("L5-cargo-build-cold-cargo-home", "legit", ["cargo", "build"],
         env={"CARGO_HOME": str(f.home / ".cargo-cold-{label}"), "CARGO_TARGET_DIR": str(f.root / "target-cold-{label}")},
         check=lambda l, r: r.returncode == 0,
         note="CARGO_HOME that does not exist yet and a fresh target dir: cargo must create the home, download serde and build; needs HOME write and network")
    case("L6-git-status", "legit", ["git", "status", "--porcelain"], check=lambda l, r: r.returncode == 0)
    case("L7-git-add-commit", "legit",
         ["sh", "-c", "echo {label} >> README.md && git -c user.name=p33 -c user.email=p33@invalid commit -qam edit-{label}"],
         check=lambda l, r: f.direct(["git", "log", "-1", "--format=%s"]).stdout.strip() == f"edit-{l}".encode())
    case("L8-git-stash-list", "legit", ["git", "stash", "list"], check=lambda l, r: r.returncode == 0)
    if shutil.which("node", path=f.env["PATH"]):
        case("L9-node-run", "legit", ["node", "app.js"], check=lambda l, r: r.stdout.strip() == b"42")
    case("L10-python-run", "legit", ["python3", "-c", "print(6*7)"], check=lambda l, r: r.stdout.strip() == b"42")
    case("L11-write-target", "legit", ["sh", "-c", "mkdir -p target && echo x > target/p33.txt"],
         check=lambda l, r: (f.root / "target/p33.txt").exists())
    case("L12-write-tmpdir", "legit", ["sh", "-c", "echo x > \"$TMPDIR/p33-$1.txt\"", "sh"],
         check=lambda l, r: any(f.tmp.glob("p33-*.txt")))
    case("L13-write-slash-tmp", "legit", ["sh", "-c", f"echo x > /tmp/p33-{os.getpid()}-$1.txt", "sh"],
         check=lambda l, r: any(Path("/tmp").glob(f"p33-{os.getpid()}-*.txt")))
    case("L14-cwd-subdir", "legit", ["sh", "-c", "pwd; echo y > sub.txt"], cwd=f.root / "src",
         check=lambda l, r: (f.root / "src/sub.txt").exists())

    # --- constraints the sandbox is supposed to add
    case("C1-write-outside", "constraint", ["sh", "-c", f"echo x > {f.outside}/w-{{label}}.txt"],
         check=lambda l, r: (f.outside / f"w-{l}.txt").exists())
    case("C2-write-home", "constraint", ["sh", "-c", f"echo x > {f.home}/w-{{label}}.txt"],
         check=lambda l, r: (f.home / f"w-{l}.txt").exists())
    case("C3-write-dotgit", "constraint", ["sh", "-c", "echo x > .git/w-{label}"],
         check=lambda l, r: (f.root / ".git" / f"w-{l}").exists())
    case("C4-write-through-cargo-symlink", "constraint", ["sh", "-c", f"echo x > {f.home}/.cargo/p33-w-{{label}}.txt"],
         check=lambda l, r: (REAL_HOME / ".cargo" / f"p33-w-{l}.txt").exists(),
         note="through the .cargo symlink into the real ~/.cargo")
    case("C5-read-outside", "constraint", ["cat", str(f.outside / "canary.txt")], check=lambda l, r: b"outside-canary" in r.stdout)
    case("C6-read-home", "constraint", ["cat", str(f.home / "canary.txt")], check=lambda l, r: b"home-canary" in r.stdout)
    case("C7-loopback-connect", "constraint",
         ["python3", "-c", f"import socket; s=socket.socket(); s.connect(('127.0.0.1',{f.port})); s.send(b'p33'); print('connected')"],
         check=lambda l, r: b"connected" in r.stdout)
    for p in list((REAL_HOME / ".cargo").glob("p33-w-*.txt")):
        p.unlink()

    # --- two variants of the profile, for the decision on what the opt-in should send
    def git_writable(profile):
        for e in profile["file_system"]["entries"]:
            if e["path"].get("value", {}).get("subpath") == ".git":
                e["access"] = "write"

    def network_enabled(profile):
        profile["network"] = "enabled"

    variants = {}
    r = f.sandboxed(["sh", "-c", "echo V1 >> README.md && git -c user.name=p33 -c user.email=p33@invalid commit -qam edit-V1"], variant=git_writable)
    variants["git_writable_commit"] = {**out(r), "committed": f.direct(["git", "log", "-1", "--format=%s"]).stdout.strip() == b"edit-V1"}
    r = f.sandboxed(["sh", "-c", f"echo x > {f.outside}/w-V1.txt"], variant=git_writable)
    variants["git_writable_still_blocks_outside"] = {**out(r), "written": (f.outside / "w-V1.txt").exists()}
    r = f.sandboxed(["python3", "-c", f"import socket; s=socket.socket(); s.connect(('127.0.0.1',{f.port})); s.send(b'V2'); print('connected')"], variant=network_enabled)
    variants["network_enabled_connect"] = {**out(r), "connected": b"connected" in r.stdout}
    r = f.sandboxed(["cargo", "build"], env={"CARGO_HOME": str(f.home / ".cargo-cold-V2"), "CARGO_TARGET_DIR": str(f.root / "target-cold-V2")}, variant=network_enabled)
    variants["network_enabled_cold_build"] = out(r)
    results["variants"] = variants

    # --- fresh builds, three each, for the overhead on a real compile
    builds = {"D": [], "S": []}
    for label, runner in (("D", f.direct), ("S", f.sandboxed)):
        for i in range(3):
            tdir = f.root / f"target-fresh-{label}-{i}"
            t = time.time()
            r = runner(["cargo", "build"], env={"CARGO_TARGET_DIR": str(tdir)})
            builds[label].append({"seconds": round(time.time() - t, 2), "rc": r.returncode})
            shutil.rmtree(tdir, ignore_errors=True)
    results["fresh_builds"] = builds

    # --- process group, seen from outside: ccnm starts the wrapper in its own group
    p = subprocess.Popen([CODEX, "sandbox", "--sandbox-state-json", json.dumps(sandbox_state(f.root, f.root)), "--", "sh", "-c", "sleep 4"],
                         cwd=str(f.root), env=f.env, preexec_fn=os.setpgrp, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(1.5)
    tree = subprocess.run(["ps", "-axo", "pid=,ppid=,pgid=,comm="], capture_output=True, text=True).stdout.splitlines()
    rows = [l.split(None, 3) for l in tree]
    rows = [(int(a), int(b), int(c), d) for a, b, c, d in rows if len([a, b, c, d]) == 4]
    under, stack = [], [p.pid]
    while stack:
        cur = stack.pop()
        for pid, ppid, pgid, comm in rows:
            if ppid == cur:
                under.append({"pid": pid, "pgid": pgid, "comm": comm}); stack.append(pid)
    results["process_group_from_outside"] = {"wrapper_pid": p.pid, "wrapper_pgid": os.getpgid(p.pid), "descendants": under,
                                             "all_in_wrapper_group": all(d["pgid"] == p.pid for d in under) and bool(under)}
    p.wait()

    # --- how the wrapper itself fails, so ccnm can tell it from the command failing
    r = f.sandboxed(["/nonexistent/program"])
    results["missing_program_in_sandbox"] = out(r)
    r = subprocess.run([CODEX, "sandbox", "--sandbox-state-json", "{not json", "--", "true"], cwd=str(f.root), env=f.env, capture_output=True)
    results["bad_state_json"] = out(r)
    r = subprocess.run([CODEX, "sandbox", "--sandbox-state-json", json.dumps({"permissionProfile": {}, "sandboxCwd": f"file://{f.root}", "workspaceRoots": []}), "--", "true"],
                       cwd=str(f.root), env=f.env, capture_output=True)
    results["empty_profile"] = out(r)
    r = f.sandboxed(["sh", "-c", "exit 7"])
    results["exit_code_passthrough"] = out(r)
    r = f.sandboxed(["sh", "-c", "kill -TERM $$"])
    results["signal_passthrough"] = out(r)

    # --- `ps` inside the sandbox: Seatbelt hides the process table
    r = f.sandboxed(["sh", "-c", "ps -o pid=,pgid= -p $$; echo rc=$?"])
    results["ps_inside_sandbox"] = out(r)

    # --- environment the command sees inside
    r = f.sandboxed(["sh", "-c", "env | sort"])
    inside = r.stdout.decode(errors="replace").splitlines()
    r = f.direct(["sh", "-c", "env | sort"])
    direct = r.stdout.decode(errors="replace").splitlines()
    results["env_added_by_sandbox"] = sorted(set(inside) - set(direct))
    results["env_removed_by_sandbox"] = sorted(set(direct) - set(inside))

    # --- overhead
    def timed(runner, n=20):
        ts = []
        for _ in range(n):
            t = time.time()
            runner(["/usr/bin/true"])
            ts.append(time.time() - t)
        ts.sort()
        return {"p50_ms": round(ts[len(ts) // 2] * 1000, 1), "max_ms": round(ts[-1] * 1000, 1)}
    results["overhead_true"] = {"D": timed(f.direct), "S": timed(f.sandboxed)}

    results["cases"] = cases
    f.cleanup()
    path = HERE / "runs" / f"{name}.json"
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(results, indent=2, ensure_ascii=False) + "\n")
    print("wrote", path)


if __name__ == "__main__":
    main()

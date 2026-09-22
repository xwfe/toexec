#!/usr/bin/env python3
"""受管会话里，Agent 机器上用户自己装的 skills 能不能被看到、被读到（机器级 skills 第 1 步）。

零额度：模型接口是本机假服务，请求原样存下来；HOME 是临时目录，里面只放探针 skill；
进程树用 sandbox-exec 禁掉非本机出站。启动参数照 ccnm 的 launch_cmd / build_launch_cmd 拼
（和 ../agent-surface 同一套），只改要测的那几项。

Claude 场景：
  denied       ccnm 现在的做法：--tools WebSearch，settings deny 里有 Skill
  skill        --tools WebSearch,Skill，allow Skill：清单出现在哪、调 Skill 回来什么、
               接着按正文去读附件会怎样（没有 Read 工具）
  scoped_read  再加 Read，allow 只写 skill 目录：skill 目录、cwd（ccnm 的会话状态目录）、
               HOME 里别的文件、/etc/hosts 各读一次，看哪些被放行
  precedence   同名 skill 同时在 cwd 的 .claude/skills 和 ~/.claude/skills：原生让谁进清单
Codex 场景：
  codex        照 ccnm print 模式拼：~/.agents/skills 和 $CODEX_HOME/skills 里的探针 skill
               会不会进请求（ccnm 关了 shell，列出来也读不到）
  codex_off    同上，再加 -c skills.include_instructions=false：清单能不能整段关掉

用法：python3 run_probe.py <新输出目录，放仓库外> [场景…]
环境变量 PROBE_CLAUDE / PROBE_CODEX 指定二进制。
"""

import json
import os
import subprocess
import sys
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
TINY = HERE.parent / "agent-surface" / "tiny_server.py"
CLAUDE = os.environ.get("PROBE_CLAUDE", "claude")
CODEX = os.environ.get("PROBE_CODEX", "codex")
SANDBOX_PROFILE = """(version 1)
(allow default)
(deny network-outbound)
(allow network-outbound (remote ip "localhost:*"))
(allow network-outbound (remote unix-socket))
"""
SCENARIOS = ["denied", "skill", "scoped_read", "precedence", "codex", "codex_off"]
DENY_BASE = ["Read", "Edit", "Write", "Grep", "Glob", "Bash", "NotebookEdit"]
# 抄自 ccnm provider/codex/mod.rs 的 DISABLED。
CODEX_DISABLED = [
    "shell_tool", "unified_exec", "unified_exec_tty", "view_image", "apps", "plugins", "hooks",
    "multi_agent", "multi_agent_v2", "browser_use", "computer_use", "image_generation",
    "memories", "workspace_dependencies", "skill_search", "shell_snapshot", "goals", "tool_suggest",
]
# 每个探针 skill 的名字都是唯一的记号，在请求 JSON 里搜得到就说明它进了模型上下文。
SKILLS = {
    ".claude/skills/probe-claude-home": "PROBE-CLAUDE-HOME-DESC",
    ".agents/skills/probe-agents-home": "PROBE-AGENTS-HOME-DESC",
}
CODEX_HOME_SKILL = "probe-codex-home"


def plant(home):
    for rel, desc in SKILLS.items():
        d = home / rel
        (d / "scripts").mkdir(parents=True)
        name = rel.rsplit("/", 1)[1]
        (d / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: {desc} use when probing\n---\n"
            f"BODY-OF-{name}. Run scripts/run.sh and read reference.md.\n")
        (d / "scripts/run.sh").write_text("echo ATTACHMENT-SCRIPT\n")
        (d / "reference.md").write_text("ATTACHMENT-REFERENCE\n")
    (home / "home-secret.txt").write_text("HOME-SECRET\n")


def sse(events):
    return "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events).encode()


def anthropic_events(n, blocks, stop):
    events = [{"type": "message_start", "message": {
        "id": f"msg_{n}", "type": "message", "role": "assistant", "model": "claude-probe",
        "content": [], "stop_reason": None, "stop_sequence": None,
        "usage": {"input_tokens": 1, "output_tokens": 1}}}]
    for i, block in enumerate(blocks):
        if block["type"] == "text":
            events += [
                {"type": "content_block_start", "index": i, "content_block": {"type": "text", "text": ""}},
                {"type": "content_block_delta", "index": i, "delta": {"type": "text_delta", "text": block["text"]}},
            ]
        else:
            events += [
                {"type": "content_block_start", "index": i, "content_block": {
                    "type": "tool_use", "id": block["id"], "name": block["name"], "input": {}}},
                {"type": "content_block_delta", "index": i, "delta": {
                    "type": "input_json_delta", "partial_json": json.dumps(block["input"])}},
            ]
        events.append({"type": "content_block_stop", "index": i})
    events += [{"type": "message_delta", "delta": {"stop_reason": stop, "stop_sequence": None},
                "usage": {"output_tokens": 1}}, {"type": "message_stop"}]
    return events


class Model:
    """假模型接口。Anthropic 和 Responses 两种都接，按路径分。"""

    def __init__(self, out, script=None):
        self.out, self.script, self.step = out, script, 0
        # Responses 一侧的剧本：返回一个 output item 就用它，返回 None 回一句 done。
        self.respond = None
        model = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def send(self, body, kind):
                self.send_response(200)
                self.send_header("content-type", kind)
                self.send_header("content-length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                self.send(json.dumps({"data": [], "models": []}).encode(), "application/json")

            def do_HEAD(self):
                self.send_response(200)
                self.end_headers()

            def do_POST(self):
                raw = self.rfile.read(int(self.headers.get("content-length") or 0))
                if "count_tokens" in self.path:
                    return self.send(b'{"input_tokens": 1}', "application/json")
                n = model.step
                model.step += 1
                (model.out / f"model-request-{n}.json").write_bytes(raw)
                request = json.loads(raw)
                if "/responses" in self.path:
                    rid = f"resp_{n}"
                    item = (model.respond and model.respond(n, request)) or {
                        "type": "message", "role": "assistant", "id": f"msg_{n}",
                        "content": [{"type": "output_text", "text": "done"}]}
                    return self.send(sse([
                        {"type": "response.created", "response": {"id": rid}},
                        {"type": "response.output_item.done", "item": item},
                        {"type": "response.completed", "response": {"id": rid, "usage": {
                            "input_tokens": 0, "input_tokens_details": None, "output_tokens": 0,
                            "output_tokens_details": None, "total_tokens": 0}}},
                    ]), "text/event-stream")
                blocks, stop = (model.script and model.script(n, request)) or (
                    [{"type": "text", "text": "ok"}], "end_turn")
                if not request.get("stream"):
                    content = [b if b["type"] == "text" else {
                        "type": "tool_use", "id": b["id"], "name": b["name"], "input": b["input"]}
                        for b in blocks]
                    return self.send(json.dumps({
                        "id": f"msg_{n}", "type": "message", "role": "assistant",
                        "model": "claude-probe", "content": content, "stop_reason": stop,
                        "stop_sequence": None,
                        "usage": {"input_tokens": 1, "output_tokens": 1}}).encode(), "application/json")
                self.send(sse(anthropic_events(n, blocks, stop)), "text/event-stream")

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()


def requests(out):
    paths = sorted(out.glob("model-request-*.json"), key=lambda p: int(p.stem.rsplit("-", 1)[1]))
    return [json.loads(p.read_text()) for p in paths]


def main_request(reqs):
    with_tools = [r for r in reqs if r.get("tools")]
    return max(with_tools, key=lambda r: len(r["tools"])) if with_tools else {}


def where(request, needle):
    """记号出现在请求的哪一段：system、tools、messages[i]。"""
    found = []
    if needle in json.dumps(request.get("system"), ensure_ascii=False):
        found.append("system")
    for tool in request.get("tools") or []:
        if needle in json.dumps(tool, ensure_ascii=False):
            found.append(f"tool:{tool.get('name')}")
    for i, m in enumerate(request.get("messages") or []):
        if needle in json.dumps(m, ensure_ascii=False):
            found.append(f"messages[{i}]")
    for i, item in enumerate(request.get("input") or []):
        if needle in json.dumps(item, ensure_ascii=False):
            found.append(f"input[{i}]:{item.get('type')}/{item.get('role')}")
    if needle in json.dumps(request.get("instructions"), ensure_ascii=False):
        found.append("instructions")
    return found


def tool_results(reqs):
    results = {}
    for req in reqs:
        for m in req.get("messages", []):
            for part in m.get("content") if isinstance(m.get("content"), list) else []:
                if part.get("type") == "tool_result" and part["tool_use_id"].startswith("toolu_probe_"):
                    content = part.get("content")
                    text = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)
                    results[part["tool_use_id"]] = {"is_error": bool(part.get("is_error")),
                                                     "text": text[:600]}
    return results


def claude(out, tools, allow_extra, deny, script=None, prepare=None):
    out.mkdir(parents=True)
    home, state = out / "home", out / "state"
    home.mkdir()
    state.mkdir()
    plant(home)
    if prepare:
        prepare(home, state)
    (state / "state-secret.txt").write_text("STATE-SECRET\n")
    profile = out / "no-egress.sb"
    profile.write_text(SANDBOX_PROFILE)
    (state / "mcp.json").write_text(json.dumps({"mcpServers": {"ccnm": {
        "type": "stdio", "command": sys.executable, "args": [str(TINY)]}}}))
    (state / "settings.json").write_text(json.dumps({"permissions": {
        "allow": ["mcp__ccnm__read_file", "mcp__ccnm__apply_patch", "WebSearch", *allow_extra],
        "deny": deny}}))
    model = Model(out, script and (lambda n, req: script(n, req, home, state)))
    cmd = ["sandbox-exec", "-f", str(profile), CLAUDE, "--tools", tools,
           "--mcp-config", str(state / "mcp.json"), "--strict-mcp-config",
           "--settings", str(state / "settings.json"), "--setting-sources", "user,project,local",
           "--permission-mode", "acceptEdits", "--session-id", str(uuid.uuid4()),
           "--print", "--output-format", "json", "--permission-prompts", "none",
           "--no-session-persistence"]
    env = {"HOME": str(home), "PATH": os.environ["PATH"],
           "ANTHROPIC_BASE_URL": f"http://127.0.0.1:{model.port}",
           "ANTHROPIC_API_KEY": "sk-ant-probe-not-a-real-key",
           "DISABLE_TELEMETRY": "1", "DISABLE_ERROR_REPORTING": "1", "DISABLE_AUTOUPDATER": "1",
           "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1"}
    try:
        proc = subprocess.run(cmd, cwd=state, env=env, input=b"do the probe task",
                              capture_output=True, timeout=180)
        rc, stderr = proc.returncode, proc.stderr
    except subprocess.TimeoutExpired as exc:
        rc, stderr = "timeout", exc.stderr or b""
    reqs = requests(out)
    main = main_request(reqs)
    return {"tools_flag": tools, "allow_extra": allow_extra, "deny": deny, "rc": rc,
            "model_requests": len(reqs),
            "tools_seen": [t.get("name") or t.get("type") for t in main.get("tools", [])],
            "listing": {desc: where(main, desc) for desc in SKILLS.values()},
            "skill_tool_schema": next((t for t in main.get("tools", []) if t.get("name") == "Skill"), None),
            "results": tool_results(reqs),
            "stderr": stderr.decode(errors="replace")[-500:]}


def sequence(calls):
    """假模型依次发 calls 里的工具调用（只在主会话），全部有结果后收尾。"""
    def script(n, req, home, state):
        names = [t.get("name") for t in req.get("tools") or []]
        if "mcp__ccnm__read_file" not in names:
            return [{"type": "text", "text": "side ok"}], "end_turn"
        done = sum(1 for m in req.get("messages", []) if isinstance(m.get("content"), list)
                   for part in m["content"] if part.get("type") == "tool_result")
        if done < len(calls):
            name, args = calls[done](home, state)
            return [{"type": "tool_use", "id": f"toolu_probe_{done}", "name": name,
                     "input": args}], "tool_use"
        return [{"type": "text", "text": "all done"}], "end_turn"
    return script


def denied(out):
    return {"scenario": "denied", **claude(out, "WebSearch", [], DENY_BASE + ["Skill"])}


def skill(out):
    calls = [
        lambda h, s: ("Skill", {"skill": "probe-claude-home"}),
        lambda h, s: ("Skill", {"skill": "probe-agents-home"}),
        lambda h, s: ("Read", {"file_path": str(h / ".claude/skills/probe-claude-home/reference.md")}),
    ]
    return {"scenario": "skill",
            **claude(out, "WebSearch,Skill", ["Skill"], DENY_BASE, sequence(calls))}


def precedence(out):
    def prepare(home, state):
        for root, desc in [(home, "DUP-FROM-PERSONAL"), (state, "DUP-FROM-PROJECT")]:
            d = root / ".claude/skills/dup-skill"
            d.mkdir(parents=True)
            (d / "SKILL.md").write_text(f"---\nname: dup-skill\ndescription: {desc}\n---\nbody\n")
        # 只在项目里的一个：它进了清单，才说明项目 skills 读了、同名时是个人的赢。
        d = state / ".claude/skills/project-only"
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text("---\nname: project-only\ndescription: PROJECT-ONLY-DESC\n---\nbody\n")
    run = claude(out, "WebSearch,Skill", ["Skill"], DENY_BASE, prepare=prepare)
    main = main_request(requests(out))
    run["dup"] = {d: where(main, d) for d in ("DUP-FROM-PERSONAL", "DUP-FROM-PROJECT", "PROJECT-ONLY-DESC")}
    return {"scenario": "precedence", **run}


def scoped_read(out):
    reads = [
        lambda h, s: h / ".claude/skills/probe-claude-home/reference.md",
        lambda h, s: h / ".agents/skills/probe-agents-home/reference.md",
        lambda h, s: s / "state-secret.txt",
        lambda h, s: h / "home-secret.txt",
        lambda h, s: Path("/etc/hosts"),
    ]
    calls = [lambda h, s: ("Skill", {"skill": "probe-claude-home"})] + [
        (lambda f: lambda h, s: ("Read", {"file_path": str(f(h, s))}))(f) for f in reads]
    runs = {}
    for label, allow in [("abs", lambda home: f"Read(/{home}/.claude/skills/**)"),
                         ("tilde", lambda home: "Read(~/.claude/skills/**)")]:
        sub = out / label
        # home 在 claude() 里才建：先算出路径写进 allow。
        home = (sub / "home").resolve()
        runs[label] = claude(sub, "WebSearch,Skill,Read", ["Skill", allow(home)],
                             [d for d in DENY_BASE if d != "Read"], sequence(calls))
        runs[label]["reads"] = ["skill attachment (claude)", "skill attachment (agents)",
                                "cwd = session state", "home file", "/etc/hosts"]
    return {"scenario": "scoped_read", **runs}


def codex_off(out):
    return {**codex(out, ["-c", "skills.include_instructions=false"]), "scenario": "codex_off"}


def codex(out, extra=()):
    out.mkdir(parents=True)
    home, work = out / "home", out / "work"
    codex_home = out / "codex-home"
    home.mkdir()
    work.mkdir()
    plant(home)
    d = codex_home / "skills" / CODEX_HOME_SKILL
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(f"---\nname: {CODEX_HOME_SKILL}\ndescription: PROBE-CODEX-HOME-DESC x\n---\nbody\n")
    profile = out / "no-egress.sb"
    profile.write_text(SANDBOX_PROFILE)
    model = Model(out)
    p = "probemock"
    cmd = ["sandbox-exec", "-f", str(profile), CODEX, "exec", "--ignore-user-config",
           "--ignore-rules", "--skip-git-repo-check", "--ephemeral", "--json", "--color", "never",
           "-c", f'model_provider="{p}"', "-c", f'model_providers.{p}.name="{p}"',
           "-c", f'model_providers.{p}.base_url="http://127.0.0.1:{model.port}/v1"',
           "-c", f'model_providers.{p}.wire_api="responses"',
           "-c", f"model_providers.{p}.requires_openai_auth=false",
           "--sandbox", "read-only", "-c", 'approval_policy="never"',
           "-c", 'web_search="cached"', "-c", "agents.enabled=false",
           "--enable", "code_mode_only", "-c",
           'features.code_mode.excluded_tool_namespaces=["functions","collaboration"]']
    for feature in CODEX_DISABLED:
        cmd += ["--disable", feature]
    cmd += ["-c", f"mcp_servers.ccnm.command={json.dumps(sys.executable)}",
            "-c", f"mcp_servers.ccnm.args={json.dumps([str(TINY)])}",
            "-c", "mcp_servers.ccnm.required=true",
            "-c", 'mcp_servers.ccnm.default_tools_approval_mode="approve"',
            "-c", 'mcp_servers.ccnm.enabled_tools=["read_file","apply_patch"]', *extra, "-"]
    env = {"HOME": str(home), "CODEX_HOME": str(codex_home), "PATH": os.environ["PATH"]}
    try:
        proc = subprocess.run(cmd, cwd=work, env=env, input=b"go", capture_output=True, timeout=90)
        rc, stderr = proc.returncode, proc.stderr
    except subprocess.TimeoutExpired as exc:
        rc, stderr = "timeout", exc.stderr or b""
    reqs = requests(out)
    first = reqs[0] if reqs else {}
    needles = list(SKILLS.values()) + ["PROBE-CODEX-HOME-DESC", "<skills_instructions>"]
    return {"scenario": "codex", "extra": list(extra), "rc": rc, "model": first.get("model"),
            "model_requests": len(reqs),
            "listing": {needle: where(first, needle) for needle in needles},
            "stderr": stderr.decode(errors="replace")[-500:]}


def main():
    out = Path(sys.argv[1]).resolve()
    chosen = sys.argv[2:] or SCENARIOS
    versions = {
        "claude": subprocess.run([CLAUDE, "--version"], capture_output=True, text=True).stdout.strip(),
        "codex": subprocess.run([CODEX, "--version"], capture_output=True, text=True).stdout.strip(),
    }
    runs = [globals()[name](out / name) for name in chosen]
    summary = {"hosts": versions, "runs": runs}
    out.mkdir(parents=True, exist_ok=True)
    (out / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""真实的 Claude Code / Codex 经真实的 ccnm Runtime 服务调到项目里声明的 MCP server（v4 第 3 步）。

验的是 ccnm P49 的 `call_mcp_tool`：客户端认不认这个工具、ccnm 生成的允许表放不放它、
嵌套的 `arguments` 对象能不能原样到达、大结果接 `read_output` 读不读得全。

零额度，和 ../../v3-parity/machine-skills/run_probe.py 同一套：假模型接口把每次请求
原样存下、按剧本回工具调用，临时 HOME，sandbox-exec 禁非本机出站。Runtime 那一半是
**真实的 ccnm 二进制**（环境变量 CCNM_BIN）跑 `internal mcp-serve`：用 external coding
的 payload（受管会话要一份经过核验的绑定，本机没有那一跳），外面套 `env -i`，和真部署里
SSH 那头的干净环境一样——不套的话 Claude 的 ANTHROPIC_API_KEY 会一路带进 ccnm，ccnm
的执行门会（正确地）拒绝起任何程序。项目根下的 `.mcp.json` 声明本目录的
fake_mcp_server.py（和 ccnm 仓 tests/fixtures/ 里那份一样）。

场景：
  claude  假模型依次调 mcp__ccnm__call_mcp_tool：不带参数、只带 server、调 echo（嵌套
          arguments）、调 big（52 000 字节），再照结果里的说明调 mcp__ccnm__read_output
  codex   指定 gpt-5.1-codex（工具在请求顶层），假模型发 namespace=mcp__ccnm、
          name=call_mcp_tool 的 function_call

用法：CCNM_BIN=<ccnm> python3 run_ccnm_relay.py <新输出目录，放仓库外> [场景…]
"""

import base64
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent.parent / "v3-parity" / "machine-skills"))

from run_probe import (CLAUDE, CODEX, CODEX_DISABLED, DENY_BASE, SANDBOX_PROFILE, Model,  # noqa: E402
                       requests, tool_results)

CCNM = os.environ.get("CCNM_BIN")
# ccnm 的 session::MCP_TOOLS，P49 起第十二个是 call_mcp_tool。
MCP_TOOLS = ["workspace_info", "read_file", "list_files", "search_text", "apply_patch",
             "exec_command", "read_output", "load_skill", "view_image", "read_notebook",
             "stop_command", "call_mcp_tool"]


def runtime(out):
    """一台"Runtime"：ccnm 配置、一个项目（里面有 .mcp.json）、执行账号的 HOME 和状态目录。
    返回启动 ccnm internal mcp-serve 的 argv（外面套 env -i）。"""
    base = out / "runtime"
    root, home, state = base / "project", base / "home", base / "state"
    for d in (root, home, state):
        d.mkdir(parents=True)
    base = base.resolve()
    root, home, state = base / "project", base / "home", base / "state"
    (root / ".mcp.json").write_text(json.dumps({"mcpServers": {"fake": {
        "command": sys.executable, "args": [str(HERE / "fake_mcp_server.py")],
        "env": {"DB_TOKEN": "t0k"}}}}))
    config = base / "config.toml"
    config.write_text(f"""this = "runtime"

[nodes.runtime]

[workspaces.demo]
root = "{root}"
agent_node = "runtime"
external_mcp = "coding"
allow_unconfined_exec = true
""")
    body = json.dumps({"protocol": 5, "workspace": "demo",
                       "session": f"relay-probe-{uuid.uuid4().hex[:8]}", "mode": "coding"})
    payload = base64.urlsafe_b64encode(body.encode()).decode().rstrip("=")
    return "/usr/bin/env", ["-i", f"PATH={os.environ['PATH']}", f"HOME={home}",
                            f"XDG_STATE_HOME={state}", f"CCNM_CONFIG={config}",
                            CCNM, "internal", "mcp-serve", "--payload", payload]


def last_result_text(req):
    for m in reversed(req.get("messages", [])):
        for part in reversed(m.get("content") if isinstance(m.get("content"), list) else []):
            if part.get("type") == "tool_result":
                content = part.get("content")
                if isinstance(content, list):
                    return "\n".join(c.get("text", "") for c in content if c.get("type") == "text")
                return content or ""
    return ""


def claude(out):
    out.mkdir(parents=True)
    home, state = out / "home", out / "state"
    home.mkdir()
    state.mkdir()
    command, args = runtime(out)
    session = str(uuid.uuid4())
    (state / "mcp.json").write_text(json.dumps({"mcpServers": {
        "ccnm": {"type": "stdio", "command": command, "args": args}}}))
    (state / "settings.json").write_text(json.dumps({"permissions": {
        "allow": [f"mcp__ccnm__{t}" for t in MCP_TOOLS] + ["WebSearch"],
        "deny": DENY_BASE + ["Skill"]}}))
    tool = "mcp__ccnm__call_mcp_tool"
    plan = [(tool, {}), (tool, {"server": "fake"}),
            (tool, {"server": "fake", "tool": "echo", "arguments": {"q": 1, "nested": {"a": [1, 2]}}}),
            (tool, {"server": "fake", "tool": "big"}),
            ("mcp__ccnm__read_output", None)]
    sent = []

    def script(n, req):
        names = [t.get("name") for t in req.get("tools") or []]
        if tool not in names:
            return [{"type": "text", "text": "side ok"}], "end_turn"
        done = sum(1 for m in req.get("messages", []) if isinstance(m.get("content"), list)
                   for part in m["content"] if part.get("type") == "tool_result")
        if done >= len(plan):
            return [{"type": "text", "text": "all done"}], "end_turn"
        name, arguments = plan[done]
        if arguments is None:
            # 照上一个结果末尾的说明接着读。
            note = last_result_text(req)
            ref = note.split("output_ref=")[1].split()[0]
            offset = int(note.split("offset=")[1].split("]")[0])
            arguments = {"output_ref": ref, "offset": offset, "limit": 32768}
        sent.append({"tool": name, "input": arguments})
        return [{"type": "tool_use", "id": f"toolu_probe_{done}", "name": name,
                 "input": arguments}], "tool_use"

    profile = out / "no-egress.sb"
    profile.write_text(SANDBOX_PROFILE)
    model = Model(out, script)
    cmd = ["sandbox-exec", "-f", str(profile), CLAUDE, "--tools", "WebSearch",
           "--mcp-config", str(state / "mcp.json"), "--strict-mcp-config",
           "--settings", str(state / "settings.json"), "--setting-sources", "user,project,local",
           "--permission-mode", "acceptEdits", "--session-id", session,
           "--print", "--output-format", "json", "--permission-prompts", "none",
           "--no-session-persistence"]
    env = {"HOME": str(home), "PATH": os.environ["PATH"],
           "ANTHROPIC_BASE_URL": f"http://127.0.0.1:{model.port}",
           "ANTHROPIC_API_KEY": "sk-ant-probe-not-a-real-key",
           "DISABLE_TELEMETRY": "1", "DISABLE_ERROR_REPORTING": "1", "DISABLE_AUTOUPDATER": "1",
           "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1", "MCP_TIMEOUT": "60000"}
    proc = subprocess.run(cmd, cwd=state, env=env, input=b"do the probe task",
                          capture_output=True, timeout=300)
    reqs = requests(out)
    main = max((r for r in reqs if r.get("tools")), key=lambda r: len(r["tools"]), default={})
    offered = next((t for t in main.get("tools", []) if t.get("name") == tool), None)
    results = tool_results(reqs)
    return {"scenario": "claude", "rc": proc.returncode,
            "ccnm_tools_seen": sorted(t.get("name") for t in main.get("tools", [])
                                      if str(t.get("name", "")).startswith("mcp__ccnm__")),
            "description_tail": (offered or {}).get("description", "")[-60:],
            "input_schema": (offered or {}).get("input_schema"),
            "sent": sent, "results": results,
            "stderr": proc.stderr.decode(errors="replace")[-400:]}


def codex(out):
    out.mkdir(parents=True)
    home, work, codex_home = out / "home", out / "work", out / "codex-home"
    for d in (home, work, codex_home):
        d.mkdir()
    command, args = runtime(out)
    state = {"called": None}

    def respond(n, request):
        namespace = next((t.get("name") for t in request.get("tools", [])
                          if t.get("type") == "namespace" and t.get("name") == "mcp__ccnm"), None)
        if n == 0 and namespace:
            state["called"] = namespace
            return {"type": "function_call", "id": "fc_0", "call_id": "call_0",
                    "namespace": namespace, "name": "call_mcp_tool",
                    "arguments": json.dumps({"server": "fake", "tool": "echo",
                                             "arguments": {"q": 1, "nested": {"a": [1, 2]}}})}
        return None

    model = Model(out)
    model.respond = respond
    p = "probemock"
    cmd = ["sandbox-exec", "-f", str(out / "no-egress.sb"), CODEX, "exec", "--ignore-user-config",
           "--ignore-rules", "--skip-git-repo-check", "--ephemeral", "--json", "--color", "never",
           "-c", f'model_provider="{p}"', "-c", f'model_providers.{p}.name="{p}"',
           "-c", f'model_providers.{p}.base_url="http://127.0.0.1:{model.port}/v1"',
           "-c", f'model_providers.{p}.wire_api="responses"',
           "-c", f"model_providers.{p}.requires_openai_auth=false",
           "--model", "gpt-5.1-codex",
           "--sandbox", "read-only", "-c", 'approval_policy="never"',
           "-c", 'web_search="cached"', "-c", "agents.enabled=false"]
    for feature in CODEX_DISABLED:
        cmd += ["--disable", feature]
    cmd += ["-c", f"mcp_servers.ccnm.command={json.dumps(command)}",
            "-c", f"mcp_servers.ccnm.args={json.dumps(args)}",
            "-c", "mcp_servers.ccnm.required=true",
            "-c", "mcp_servers.ccnm.startup_timeout_sec=60",
            "-c", 'mcp_servers.ccnm.default_tools_approval_mode="approve"',
            "-c", f"mcp_servers.ccnm.enabled_tools={json.dumps(MCP_TOOLS)}",
            "-c", "skills.include_instructions=false", "-"]
    (out / "no-egress.sb").write_text(SANDBOX_PROFILE)
    env = {"HOME": str(home), "CODEX_HOME": str(codex_home), "PATH": os.environ["PATH"]}
    proc = subprocess.run(cmd, cwd=work, env=env, input=b"go", capture_output=True, timeout=180)
    reqs = requests(out)
    output = None
    for req in reversed(reqs):
        for item in req.get("input", []):
            if isinstance(item, dict) and item.get("type") == "function_call_output" \
                    and item.get("call_id") == "call_0":
                output = item.get("output")
        if output is not None:
            break
    first = reqs[0] if reqs else {}
    namespace = next((t for t in first.get("tools", []) if t.get("name") == "mcp__ccnm"), {})
    return {"scenario": "codex", "rc": proc.returncode, "model": first.get("model"),
            "ccnm_tools_seen": sorted(t.get("name") for t in namespace.get("tools", [])),
            "namespace_called": state["called"],
            "function_call_output": output if isinstance(output, str) else json.dumps(output)[:600],
            "stderr": proc.stderr.decode(errors="replace")[-400:]}


def main():
    if not CCNM:
        sys.exit("CCNM_BIN 指向一个带 P49 的 ccnm 二进制")
    out = Path(sys.argv[1]).resolve()
    chosen = sys.argv[2:] or ["claude", "codex"]
    versions = {
        "claude": subprocess.run([CLAUDE, "--version"], capture_output=True, text=True).stdout.strip(),
        "codex": subprocess.run([CODEX, "--version"], capture_output=True, text=True).stdout.strip(),
        "ccnm": subprocess.run([CCNM, "--version"], capture_output=True, text=True).stdout.strip(),
    }
    runs = [globals()[name](out / name) for name in chosen]
    summary = {"hosts": versions, "runs": runs}
    (out / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

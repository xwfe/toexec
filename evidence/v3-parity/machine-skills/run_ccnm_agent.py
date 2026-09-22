#!/usr/bin/env python3
"""接上 ccnm 的 Agent 端 skills 服务之后，真实的 Claude Code / Codex 能不能调到它。

`run_probe.py` 回答的是"原生的行不行"；这里验的是按它的结论做出来的东西：ccnm P48 的
`ccnm internal agent-skills`。零额度，假模型接口、临时 HOME、sandbox-exec 禁出站，和
run_probe.py 同一套。ccnm 的 Runtime 那一半仍由 ../agent-surface/tiny_server.py 冒充，
Agent 端这个服务用**真实的 ccnm 二进制**（环境变量 CCNM_BIN）。配置照 ccnm 生成的写：
Claude 是 mcp.json 里第二个 server 加 settings allow；Codex 是
`-c mcp_servers.ccnm_agent.*` 加 `skills.include_instructions=false`。

场景：
  claude  假模型依次调 mcp__ccnm_agent__load_skill：不带名字、带名字、读附件、读点文件
  codex   指定 gpt-5.1-codex（ccnm 对没测过的模型不开 Code Mode，工具就在请求顶层），
          假模型发 namespace=mcp__ccnm_agent 的 function_call 读附件

用法：CCNM_BIN=<ccnm> python3 run_ccnm_agent.py <新输出目录，放仓库外> [场景…]
"""

import base64
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

from run_probe import (CLAUDE, CODEX, CODEX_DISABLED, DENY_BASE, SANDBOX_PROFILE, TINY, Model,
                       plant, requests, sse, tool_results)

CCNM = os.environ.get("CCNM_BIN")
SERVER = "ccnm_agent"


def agent_args(home, session):
    body = json.dumps({"protocol": 1, "home": str(home), "session": session}).encode()
    return ["internal", "agent-skills", "--payload",
            base64.urlsafe_b64encode(body).decode().rstrip("=")]


def claude(out):
    out.mkdir(parents=True)
    home, state = out / "home", out / "state"
    home.mkdir()
    state.mkdir()
    plant(home)
    (home / ".agents/skills/probe-agents-home/.env").write_text("TOKEN=probe\n")
    session = str(uuid.uuid4())
    (state / "mcp.json").write_text(json.dumps({"mcpServers": {
        "ccnm": {"type": "stdio", "command": sys.executable, "args": [str(TINY)]},
        SERVER: {"type": "stdio", "command": CCNM, "args": agent_args(home, session)}}}))
    (state / "settings.json").write_text(json.dumps({"permissions": {
        "allow": ["mcp__ccnm__read_file", "mcp__ccnm__apply_patch", f"mcp__{SERVER}__load_skill",
                  "WebSearch"],
        "deny": DENY_BASE + ["Skill"]}}))
    tool = f"mcp__{SERVER}__load_skill"
    calls = [{}, {"name": "probe-agents-home"},
             {"name": "probe-agents-home", "file": "reference.md"},
             {"name": "probe-agents-home", "file": ".env"}]

    def script(n, req):
        names = [t.get("name") for t in req.get("tools") or []]
        if tool not in names:
            return [{"type": "text", "text": "side ok"}], "end_turn"
        done = sum(1 for m in req.get("messages", []) if isinstance(m.get("content"), list)
                   for part in m["content"] if part.get("type") == "tool_result")
        if done < len(calls):
            return [{"type": "tool_use", "id": f"toolu_probe_{done}", "name": tool,
                     "input": calls[done]}], "tool_use"
        return [{"type": "text", "text": "all done"}], "end_turn"

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
           "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1"}
    proc = subprocess.run(cmd, cwd=state, env=env, input=b"do the probe task",
                          capture_output=True, timeout=180)
    reqs = requests(out)
    main = max((r for r in reqs if r.get("tools")), key=lambda r: len(r["tools"]), default={})
    offered = next((t for t in main.get("tools", []) if t.get("name") == tool), None)
    return {"scenario": "claude", "rc": proc.returncode,
            "tools_seen": [t.get("name") or t.get("type") for t in main.get("tools", [])],
            "description_tail": (offered or {}).get("description", "")[-160:],
            "calls": calls, "results": tool_results(reqs),
            "stderr": proc.stderr.decode(errors="replace")[-400:]}


def codex(out):
    out.mkdir(parents=True)
    home, work, codex_home = out / "home", out / "work", out / "codex-home"
    home.mkdir()
    work.mkdir()
    codex_home.mkdir()
    plant(home)
    state = {"called": None}

    def respond(n, request):
        namespace = next((t.get("name") for t in request.get("tools", [])
                          if t.get("type") == "namespace" and t.get("name") == f"mcp__{SERVER}"),
                         None)
        if n == 0 and namespace:
            state["called"] = namespace
            return {"type": "function_call", "id": "fc_0", "call_id": "call_0",
                    "namespace": namespace, "name": "load_skill",
                    "arguments": json.dumps({"name": "probe-agents-home", "file": "reference.md"})}
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
    cmd += ["-c", f"mcp_servers.ccnm.command={json.dumps(sys.executable)}",
            "-c", f"mcp_servers.ccnm.args={json.dumps([str(TINY)])}",
            "-c", "mcp_servers.ccnm.required=true",
            "-c", 'mcp_servers.ccnm.default_tools_approval_mode="approve"',
            "-c", 'mcp_servers.ccnm.enabled_tools=["read_file","apply_patch"]',
            "-c", "skills.include_instructions=false",
            "-c", f"mcp_servers.{SERVER}.command={json.dumps(CCNM)}",
            "-c", f"mcp_servers.{SERVER}.args={json.dumps(agent_args(home, 'codex-probe'))}",
            "-c", f'mcp_servers.{SERVER}.default_tools_approval_mode="approve"',
            "-c", f'mcp_servers.{SERVER}.enabled_tools=["load_skill"]', "-"]
    (out / "no-egress.sb").write_text(SANDBOX_PROFILE)
    env = {"HOME": str(home), "CODEX_HOME": str(codex_home), "PATH": os.environ["PATH"]}
    proc = subprocess.run(cmd, cwd=work, env=env, input=b"go", capture_output=True, timeout=120)
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
    return {"scenario": "codex", "rc": proc.returncode, "model": first.get("model"),
            "namespace_called": state["called"],
            "skills_instructions_in_request": "<skills_instructions>" in json.dumps(first),
            "function_call_output": output if isinstance(output, str) else json.dumps(output)[:600],
            "stderr": proc.stderr.decode(errors="replace")[-400:]}


def main():
    if not CCNM:
        sys.exit("CCNM_BIN 指向一个带 P48 的 ccnm 二进制")
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

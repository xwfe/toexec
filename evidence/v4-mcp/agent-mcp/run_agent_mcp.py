#!/usr/bin/env python3
"""受管会话直接接 Agent 机器上装好的 MCP server，行不行、大结果到模型那里还剩多少（v4 第 4 步）。

零额度，和 ../../v3-parity/machine-skills/run_probe.py 同一套（直接 import 它的助手）：假模型
接口把每次请求原样存下、按剧本回工具调用，临时 HOME，sandbox-exec 禁非本机出站。启动参数照
ccnm 的 launch_cmd / build_launch_cmd 拼，ccnm 自己的 server 用 ../../v3-parity/agent-surface
的 tiny_server.py 冒充。"装好的 server"是本目录的 fake_agent_server.py，一份走 stdio（名字
fake）、一份走 streamable HTTP（名字 web，127.0.0.1 上的随机端口）。

Claude 场景：
  claude_allowed   settings 的 allow 加 server 级的 "mcp__fake"、"mcp__web"：依次调 fake 的 echo
                   （嵌套 arguments）、web 的 echo、fake 的 big（52 000 字节）、fake 的 huge
                   （400 000 字节）、web 的 huge；记每个结果到模型那里是什么样
  claude_unlisted  allow 里没有它们：print 模式下调 fake 的 echo 会怎样
  claude_sizes     调 fake 的 sized，30 000 到 50 000 字节，找从多大开始只给预览
  claude_meta      调 fake 的 sized_meta（工具上声明 maxResultSizeChars=200000）52 000、100 000、
                   150 000 字节，再调一次不带声明的 52 000 对照
Codex 场景（指定 gpt-5.1-codex，工具在请求顶层）：
  codex_inline     -c mcp_servers.<名字>={…} 整张内联表，不写 default_tools_approval_mode：
                   依次调 fake 的 echo、web 的 echo、fake 的 huge
  codex_approve    同上，两个 server 都加 default_tools_approval_mode="approve"
  codex_sizes      同 codex_approve，调 fake 的 sized，8 KiB 到 52 000 字节，找从多大开始砍中间
  codex_code_mode  不写 --model（默认模型自带 Code Mode，ccnm 受管会话默认如此）：模型在 exec
                   的 JS 里调 fake 的 sized（16 KiB、32 KiB、64 KiB）再 text() 出来，看剩多少
  codex_code_mode_limit  同上，加 -c tool_output_token_limit=20000
  codex_sizes_limit  同上，加 -c tool_output_token_limit=20000，再多调 64 KiB、79 000、81 000 字节：
                   看上限能不能调、这个数按什么算

经真实 ccnm（环境变量 CCNM_BIN，P50 的 `ccnm internal agent-skills`）：
  ccnm_claude      mcp.json 照 ccnm 生成的写（ccnm 自己的用 tiny_server 冒充，ccnm_agent 是真实二进制，
                   payload 里 local = [fake, web]），allow 照 ccnm 写三个 ccnm_agent 工具：假模型依次
                   不带参数、带 server、调 fake 的 echo（嵌套参数）、调 web 的 echo（经 curl）、调 fake 的
                   big（52 000 字节），再照结果末尾的说明一段段调 read_mcp_result 直到读完
  ccnm_codex       Codex 照 ccnm 的 -c 拼（含 tool_output_token_limit=20000），gpt-5.1-codex：调 fake 的
                   echo、big，看第一段 32 KiB 是不是完整到了模型面前
  ccnm_codex_nolimit  同上，不加 tool_output_token_limit：对照
  ccnm_codex_code_mode 不写 --model（Code Mode）：模型在 exec 的 JS 里调 call_mcp_tool 再 text() 出来

用法：[CCNM_BIN=…] python3 run_agent_mcp.py <新输出目录，放仓库外> [场景…]
"""

import base64
import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent.parent / "v3-parity" / "machine-skills"))

from run_probe import (CLAUDE, CODEX, CODEX_DISABLED, DENY_BASE, SANDBOX_PROFILE, TINY,  # noqa: E402
                       Model, requests)

FAKE = HERE / "fake_agent_server.py"
NESTED = {"q": 1, "nested": {"a": [1, 2]}}


def web_server(out):
    """起 HTTP 那一份，返回 (进程, URL)。"""
    port_file = out / "web.port"
    proc = subprocess.Popen([sys.executable, str(FAKE), "--http", str(port_file)])
    for _ in range(100):
        if port_file.exists() and port_file.read_text():
            return proc, f"http://127.0.0.1:{port_file.read_text()}/mcp"
        time.sleep(0.05)
    proc.kill()
    raise RuntimeError("the HTTP fake did not start")


def summarize(text):
    """结果太长时只留长度、头尾和提到的路径，够判断截没截、留没留话。"""
    return {"bytes": len(text.encode()), "head": text[:160], "tail": text[-400:]}


def result_texts(reqs):
    out = {}
    for req in reqs:
        for m in req.get("messages", []):
            for part in m.get("content") if isinstance(m.get("content"), list) else []:
                if part.get("type") == "tool_result" and part["tool_use_id"].startswith("toolu_probe_"):
                    content = part.get("content")
                    if isinstance(content, list):
                        text = "\n".join(c.get("text", "") for c in content if c.get("type") == "text")
                    else:
                        text = content or ""
                    out[part["tool_use_id"]] = {"is_error": bool(part.get("is_error")),
                                                **summarize(text)}
    return out


def claude(out, allow_extra, plan):
    out.mkdir(parents=True)
    home, state = out / "home", out / "state"
    home.mkdir()
    state.mkdir()
    web, url = web_server(out)
    try:
        (state / "mcp.json").write_text(json.dumps({"mcpServers": {
            "ccnm": {"type": "stdio", "command": sys.executable, "args": [str(TINY)]},
            "fake": {"type": "stdio", "command": sys.executable, "args": [str(FAKE)]},
            "web": {"type": "http", "url": url}}}))
        (state / "settings.json").write_text(json.dumps({"permissions": {
            "allow": ["mcp__ccnm__read_file", "mcp__ccnm__apply_patch", "WebSearch", *allow_extra],
            "deny": DENY_BASE + ["Skill"]}}))

        def script(n, req):
            names = [t.get("name") for t in req.get("tools") or []]
            if "mcp__ccnm__read_file" not in names:
                return [{"type": "text", "text": "side ok"}], "end_turn"
            done = sum(1 for m in req.get("messages", []) if isinstance(m.get("content"), list)
                       for part in m["content"] if part.get("type") == "tool_result")
            if done >= len(plan):
                return [{"type": "text", "text": "all done"}], "end_turn"
            name, arguments = plan[done]
            return [{"type": "tool_use", "id": f"toolu_probe_{done}", "name": name,
                     "input": arguments}], "tool_use"

        profile = out / "no-egress.sb"
        profile.write_text(SANDBOX_PROFILE)
        model = Model(out, script)
        cmd = ["sandbox-exec", "-f", str(profile), CLAUDE, "--tools", "WebSearch",
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
        proc = subprocess.run(cmd, cwd=state, env=env, input=b"do the probe task",
                              capture_output=True, timeout=300)
    finally:
        web.kill()
    reqs = requests(out)
    main = max((r for r in reqs if r.get("tools")), key=lambda r: len(r["tools"]), default={})
    try:
        printed = json.loads(proc.stdout)
    except ValueError:
        printed = {}
    return {"allow_extra": allow_extra, "rc": proc.returncode,
            "tools_seen": sorted(t.get("name") for t in main.get("tools", [])),
            "calls": [name for name, _ in plan],
            "results": result_texts(reqs),
            "permission_denials": printed.get("permission_denials"),
            "stderr": proc.stderr.decode(errors="replace")[-400:]}


def claude_allowed(out):
    plan = [("mcp__fake__echo", NESTED), ("mcp__web__echo", NESTED), ("mcp__fake__big", {}),
            ("mcp__fake__huge", {}), ("mcp__web__huge", {})]
    return {"scenario": "claude_allowed", **claude(out, ["mcp__fake", "mcp__web"], plan)}


def claude_unlisted(out):
    return {"scenario": "claude_unlisted", **claude(out, [], [("mcp__fake__echo", NESTED)])}


def codex(out, approve, plan, extra=()):
    out.mkdir(parents=True)
    home, work, codex_home = out / "home", out / "work", out / "codex-home"
    for d in (home, work, codex_home):
        d.mkdir()
    web, url = web_server(out)
    seen = {}

    def respond(n, request):
        spaces = {t.get("name"): [x.get("name") for x in t.get("tools", [])]
                  for t in request.get("tools", []) if t.get("type") == "namespace"}
        seen.setdefault("namespaces", spaces)
        done = sum(1 for item in request.get("input", [])
                   if isinstance(item, dict) and item.get("type") == "function_call_output")
        if done >= len(plan):
            return None
        namespace, name, arguments = plan[done]
        return {"type": "function_call", "id": f"fc_{done}", "call_id": f"call_{done}",
                "namespace": namespace, "name": name, "arguments": json.dumps(arguments)}

    model = Model(out)
    model.respond = respond
    p = "probemock"
    fake = {"command": sys.executable, "args": [str(FAKE)]}
    webt = {"url": url}
    if approve:
        fake["default_tools_approval_mode"] = webt["default_tools_approval_mode"] = "approve"

    def inline(table):
        # TOML 内联表：键不加引号，值用 JSON 写法（字符串、数组和 TOML 一致）。
        return "{" + ", ".join(f"{k}={json.dumps(v)}" for k, v in table.items()) + "}"

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
            "-c", f"mcp_servers.fake={inline(fake)}",
            "-c", f"mcp_servers.web={inline(webt)}",
            "-c", "skills.include_instructions=false", *extra, "-"]
    (out / "no-egress.sb").write_text(SANDBOX_PROFILE)
    env = {"HOME": str(home), "CODEX_HOME": str(codex_home), "PATH": os.environ["PATH"]}
    try:
        proc = subprocess.run(cmd, cwd=work, env=env, input=b"go", capture_output=True, timeout=180)
    finally:
        web.kill()
    reqs = requests(out)
    outputs = {}
    for req in reqs:
        for item in req.get("input", []):
            if isinstance(item, dict) and item.get("type") == "function_call_output":
                output = item.get("output")
                text = output if isinstance(output, str) else json.dumps(output, ensure_ascii=False)
                outputs[item.get("call_id")] = summarize(text)
    return {"approve": approve, "rc": proc.returncode, "model": (reqs[0] if reqs else {}).get("model"),
            "namespaces": seen.get("namespaces"), "calls": plan, "outputs": outputs,
            "stderr": proc.stderr.decode(errors="replace")[-600:]}


CODEX_PLAN = [("mcp__fake", "echo", NESTED), ("mcp__web", "echo", NESTED), ("mcp__fake", "huge", {})]
SIZES_CLAUDE = [30000, 40000, 45000, 48000, 50000]
SIZES_CODEX = [8192, 10240, 12288, 16384, 32768, 52000]


def codex_inline(out):
    return {"scenario": "codex_inline", **codex(out, False, CODEX_PLAN)}


def codex_approve(out):
    return {"scenario": "codex_approve", **codex(out, True, CODEX_PLAN)}


def claude_sizes(out):
    plan = [("mcp__fake__sized", {"bytes": n}) for n in SIZES_CLAUDE]
    return {"scenario": "claude_sizes", **claude(out, ["mcp__fake", "mcp__web"], plan)}


def claude_meta(out):
    plan = [("mcp__fake__sized_meta", {"bytes": n}) for n in (52000, 100000, 150000)]
    plan.append(("mcp__fake__sized", {"bytes": 52000}))
    return {"scenario": "claude_meta", **claude(out, ["mcp__fake", "mcp__web"], plan)}


def codex_sizes(out):
    plan = [("mcp__fake", "sized", {"bytes": n}) for n in SIZES_CODEX]
    return {"scenario": "codex_sizes", **codex(out, True, plan)}


def codex_sizes_limit(out):
    plan = [("mcp__fake", "sized", {"bytes": n}) for n in SIZES_CODEX + [65536, 79000, 81000]]
    extra = ["-c", "tool_output_token_limit=20000"]
    return {"scenario": "codex_sizes_limit", "extra": extra, **codex(out, True, plan, extra)}


CODE_MODE_SIZES = [16384, 32768, 65536]


def code_mode(out, extra):
    """不写 --model：CLI 的默认模型自带 Code Mode（ccnm 受管会话默认就是这样）。模型给 exec
    写一段 JS 调 MCP 工具、把结果 text() 出来；看这段输出到模型那里剩多少。"""
    out.mkdir(parents=True)
    home, work, codex_home = out / "home", out / "work", out / "codex-home"
    for d in (home, work, codex_home):
        d.mkdir()

    def respond(n, request):
        if not any(isinstance(i, dict) and i.get("type") == "additional_tools"
                   for i in request.get("input", [])):
            return None
        done = sum(1 for i in request.get("input", [])
                   if isinstance(i, dict) and i.get("type") == "custom_tool_call_output")
        if done >= len(CODE_MODE_SIZES):
            return None
        script = (f"const r = await tools.mcp__fake__sized({{bytes: {CODE_MODE_SIZES[done]}}});\n"
                  "text(r.content[0].text);\n")
        return {"type": "custom_tool_call", "id": f"ctc_{done}", "call_id": f"call_{done}",
                "name": "exec", "input": script}

    model = Model(out)
    model.respond = respond
    p = "probemock"
    cmd = ["sandbox-exec", "-f", str(out / "no-egress.sb"), CODEX, "exec", "--ignore-user-config",
           "--ignore-rules", "--skip-git-repo-check", "--ephemeral", "--json", "--color", "never",
           "-c", f'model_provider="{p}"', "-c", f'model_providers.{p}.name="{p}"',
           "-c", f'model_providers.{p}.base_url="http://127.0.0.1:{model.port}/v1"',
           "-c", f'model_providers.{p}.wire_api="responses"',
           "-c", f"model_providers.{p}.requires_openai_auth=false",
           "--sandbox", "read-only", "-c", 'approval_policy="never"',
           "-c", 'web_search="cached"', "-c", "agents.enabled=false",
           "--enable", "code_mode_only",
           "-c", 'features.code_mode.excluded_tool_namespaces=["functions","collaboration"]']
    for feature in CODEX_DISABLED:
        cmd += ["--disable", feature]
    cmd += ["-c", f"mcp_servers.fake.command={json.dumps(sys.executable)}",
            "-c", f"mcp_servers.fake.args={json.dumps([str(FAKE)])}",
            "-c", 'mcp_servers.fake.default_tools_approval_mode="approve"',
            "-c", "skills.include_instructions=false", *extra, "-"]
    (out / "no-egress.sb").write_text(SANDBOX_PROFILE)
    env = {"HOME": str(home), "CODEX_HOME": str(codex_home), "PATH": os.environ["PATH"]}
    proc = subprocess.run(cmd, cwd=work, env=env, input=b"go", capture_output=True, timeout=180)
    reqs = requests(out)
    outputs = {}
    for req in reqs:
        for item in req.get("input", []):
            if isinstance(item, dict) and item.get("type") == "custom_tool_call_output":
                output = item.get("output")
                text = output if isinstance(output, str) else json.dumps(output, ensure_ascii=False)
                outputs[item.get("call_id")] = summarize(text)
    return {"extra": list(extra), "rc": proc.returncode,
            "model": (reqs[0] if reqs else {}).get("model"), "sizes": CODE_MODE_SIZES,
            "outputs": outputs, "stderr": proc.stderr.decode(errors="replace")[-600:]}


def codex_code_mode(out):
    return {"scenario": "codex_code_mode", **code_mode(out, [])}


def codex_code_mode_limit(out):
    return {"scenario": "codex_code_mode_limit",
            **code_mode(out, ["-c", "tool_output_token_limit=20000"])}


CCNM = os.environ.get("CCNM_BIN")
AGENT_TOOLS = ["load_skill", "call_mcp_tool", "read_mcp_result"]


def agent_home(out, url):
    """ccnm_agent 读的那个 HOME：~/.claude.json 里装 fake（stdio）和 web（HTTP，本机地址）。
    和 Claude Code 自己的 HOME 分开，免得它改写 ~/.claude.json。"""
    home = out / "agent-home"
    home.mkdir()
    (home / ".claude.json").write_text(json.dumps({"mcpServers": {
        "fake": {"command": sys.executable, "args": [str(FAKE)]},
        "web": {"type": "http", "url": url}}}))
    return home.resolve()


def agent_argv(home):
    body = {"protocol": 1, "home": str(home), "session": "agent-mcp-probe",
            "mcp": {"local": ["fake", "web"]}}
    raw = base64.urlsafe_b64encode(json.dumps(body).encode()).decode().rstrip("=")
    return [CCNM, "internal", "agent-skills", "--payload", raw]


def note_of(req):
    """最后一个工具结果末尾那句说明。"""
    for m in reversed(req.get("messages", [])):
        for part in reversed(m.get("content") if isinstance(m.get("content"), list) else []):
            if part.get("type") == "tool_result":
                content = part.get("content")
                texts = [c.get("text", "") for c in content if c.get("type") == "text"] \
                    if isinstance(content, list) else [content or ""]
                return texts[-1]
    return ""


def ccnm_claude(out):
    out.mkdir(parents=True)
    home, state = out / "home", out / "state"
    home.mkdir()
    state.mkdir()
    web, url = web_server(out)
    argv = agent_argv(agent_home(out, url))
    tool = "mcp__ccnm_agent__call_mcp_tool"
    read = "mcp__ccnm_agent__read_mcp_result"
    plan = [(tool, {}), (tool, {"server": "fake"}),
            (tool, {"server": "fake", "tool": "echo", "arguments": NESTED}),
            (tool, {"server": "web", "tool": "echo", "arguments": NESTED}),
            (tool, {"server": "fake", "tool": "big"})]
    sent = []
    try:
        (state / "mcp.json").write_text(json.dumps({"mcpServers": {
            "ccnm": {"type": "stdio", "command": sys.executable, "args": [str(TINY)]},
            "ccnm_agent": {"type": "stdio", "command": argv[0], "args": argv[1:]}}}))
        (state / "settings.json").write_text(json.dumps({"permissions": {
            "allow": ["mcp__ccnm__read_file", "mcp__ccnm__apply_patch", "WebSearch",
                      *[f"mcp__ccnm_agent__{t}" for t in AGENT_TOOLS]],
            "deny": DENY_BASE + ["Skill"]}}))

        def script(n, req):
            names = [t.get("name") for t in req.get("tools") or []]
            if tool not in names:
                return [{"type": "text", "text": "side ok"}], "end_turn"
            done = sum(1 for m in req.get("messages", []) if isinstance(m.get("content"), list)
                       for part in m["content"] if part.get("type") == "tool_result")
            if done < len(plan):
                name, arguments = plan[done]
            else:
                note = note_of(req)
                if "ref=" not in note or "that is the end" in note:
                    return [{"type": "text", "text": "all done"}], "end_turn"
                name = read
                arguments = {"ref": note.split("ref=")[1].split()[0],
                             "offset": int(note.split("offset=")[1].split(".")[0].split("]")[0])}
            sent.append({"tool": name, "input": arguments})
            return [{"type": "tool_use", "id": f"toolu_probe_{done}", "name": name,
                     "input": arguments}], "tool_use"

        profile = out / "no-egress.sb"
        profile.write_text(SANDBOX_PROFILE)
        model = Model(out, script)
        cmd = ["sandbox-exec", "-f", str(profile), CLAUDE, "--tools", "WebSearch",
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
        proc = subprocess.run(cmd, cwd=state, env=env, input=b"do the probe task",
                              capture_output=True, timeout=300)
    finally:
        web.kill()
    reqs = requests(out)
    main = max((r for r in reqs if r.get("tools")), key=lambda r: len(r["tools"]), default={})
    offered = {t.get("name"): t for t in main.get("tools", [])}
    try:
        printed = json.loads(proc.stdout)
    except ValueError:
        printed = {}
    results = result_texts(reqs)
    # 拼回 big 的全文：第一段加上每次 read_mcp_result 的第一块文字。
    big_at = len(plan) - 1
    whole = ""
    for req in reqs[-1:]:
        for m in req.get("messages", []):
            for part in m.get("content") if isinstance(m.get("content"), list) else []:
                if part.get("type") == "tool_result" and \
                        int(part["tool_use_id"].rsplit("_", 1)[1]) >= big_at:
                    content = part.get("content")
                    texts = [c.get("text", "") for c in content if c.get("type") == "text"] \
                        if isinstance(content, list) else [content]
                    whole += texts[0]
    expected = "".join(f"line {i:05d} {'x' * 40}\n" for i in range(1000))
    return {"scenario": "ccnm_claude", "rc": proc.returncode,
            "agent_tools_seen": sorted(n for n in offered if n.startswith("mcp__ccnm_agent__")),
            "call_description_tail": offered.get(tool, {}).get("description", "")[-40:],
            "sent": sent, "results": results,
            "big_reassembled_bytes": len(whole.encode()), "big_reassembled_equal": whole == expected,
            "permission_denials": printed.get("permission_denials"),
            "stderr": proc.stderr.decode(errors="replace")[-400:]}


def ccnm_codex_run(out, extra, model_name):
    out.mkdir(parents=True)
    home, work, codex_home = out / "home", out / "work", out / "codex-home"
    for d in (home, work, codex_home):
        d.mkdir()
    web, url = web_server(out)
    argv = agent_argv(agent_home(out, url))
    calls = [{"server": "fake", "tool": "echo", "arguments": NESTED},
             {"server": "fake", "tool": "big"}]

    def respond(n, request):
        if model_name is None:
            if not any(isinstance(i, dict) and i.get("type") == "additional_tools"
                       for i in request.get("input", [])):
                return None
            done = sum(1 for i in request.get("input", [])
                       if isinstance(i, dict) and i.get("type") == "custom_tool_call_output")
            if done >= len(calls):
                return None
            script = (f"const r = await tools.mcp__ccnm_agent__call_mcp_tool({json.dumps(calls[done])});\n"
                      "for (const c of r.content) text(c.text);\n")
            return {"type": "custom_tool_call", "id": f"ctc_{done}", "call_id": f"call_{done}",
                    "name": "exec", "input": script}
        done = sum(1 for item in request.get("input", [])
                   if isinstance(item, dict) and item.get("type") == "function_call_output")
        if done >= len(calls):
            return None
        return {"type": "function_call", "id": f"fc_{done}", "call_id": f"call_{done}",
                "namespace": "mcp__ccnm_agent", "name": "call_mcp_tool",
                "arguments": json.dumps(calls[done])}

    model = Model(out)
    model.respond = respond
    p = "probemock"
    cmd = ["sandbox-exec", "-f", str(out / "no-egress.sb"), CODEX, "exec", "--ignore-user-config",
           "--ignore-rules", "--skip-git-repo-check", "--ephemeral", "--json", "--color", "never",
           "-c", f'model_provider="{p}"', "-c", f'model_providers.{p}.name="{p}"',
           "-c", f'model_providers.{p}.base_url="http://127.0.0.1:{model.port}/v1"',
           "-c", f'model_providers.{p}.wire_api="responses"',
           "-c", f"model_providers.{p}.requires_openai_auth=false"]
    if model_name:
        cmd += ["--model", model_name]
    cmd += ["--sandbox", "read-only", "-c", 'approval_policy="never"',
            "-c", 'web_search="cached"', "-c", "agents.enabled=false"]
    if model_name is None:
        cmd += ["--enable", "code_mode_only",
                "-c", 'features.code_mode.excluded_tool_namespaces=["functions","collaboration"]']
    for feature in CODEX_DISABLED:
        cmd += ["--disable", feature]
    cmd += ["-c", f"mcp_servers.ccnm.command={json.dumps(sys.executable)}",
            "-c", f"mcp_servers.ccnm.args={json.dumps([str(TINY)])}",
            "-c", "mcp_servers.ccnm.required=true",
            "-c", 'mcp_servers.ccnm.default_tools_approval_mode="approve"',
            "-c", 'mcp_servers.ccnm.enabled_tools=["read_file","apply_patch"]',
            "-c", "skills.include_instructions=false", *extra,
            "-c", f"mcp_servers.ccnm_agent.command={json.dumps(argv[0])}",
            "-c", f"mcp_servers.ccnm_agent.args={json.dumps(argv[1:])}",
            "-c", 'mcp_servers.ccnm_agent.default_tools_approval_mode="approve"',
            "-c", f"mcp_servers.ccnm_agent.enabled_tools={json.dumps(AGENT_TOOLS)}", "-"]
    (out / "no-egress.sb").write_text(SANDBOX_PROFILE)
    env = {"HOME": str(home), "CODEX_HOME": str(codex_home), "PATH": os.environ["PATH"]}
    try:
        proc = subprocess.run(cmd, cwd=work, env=env, input=b"go", capture_output=True, timeout=180)
    finally:
        web.kill()
    reqs = requests(out)
    outputs = {}
    for req in reqs:
        for item in req.get("input", []):
            if isinstance(item, dict) and item.get("type") in ("function_call_output",
                                                              "custom_tool_call_output"):
                output = item.get("output")
                text = output if isinstance(output, str) else \
                    "".join(x.get("text", "") for x in output if isinstance(x, dict))
                rows = sum(1 for line in text.splitlines() if line.startswith("line "))
                outputs[item.get("call_id")] = {**summarize(text), "big_rows": rows,
                                                "truncated_marker": "truncated" in text}
    first = reqs[0] if reqs else {}
    spaces = {t.get("name"): sorted(x.get("name") for x in t.get("tools", []))
              for t in first.get("tools", []) if t.get("type") == "namespace"}
    return {"extra": list(extra), "rc": proc.returncode, "model": first.get("model"),
            "namespaces": spaces, "calls": calls, "outputs": outputs,
            "stderr": proc.stderr.decode(errors="replace")[-600:]}


def ccnm_codex(out):
    return {"scenario": "ccnm_codex",
            **ccnm_codex_run(out, ["-c", "tool_output_token_limit=20000"], "gpt-5.1-codex")}


def ccnm_codex_nolimit(out):
    return {"scenario": "ccnm_codex_nolimit", **ccnm_codex_run(out, [], "gpt-5.1-codex")}


def ccnm_codex_code_mode(out):
    return {"scenario": "ccnm_codex_code_mode",
            **ccnm_codex_run(out, ["-c", "tool_output_token_limit=20000"], None)}


def main():
    out = Path(sys.argv[1]).resolve()
    chosen = sys.argv[2:] or ["claude_allowed", "claude_unlisted", "codex_inline", "codex_approve"]
    versions = {
        "claude": subprocess.run([CLAUDE, "--version"], capture_output=True, text=True).stdout.strip(),
        "codex": subprocess.run([CODEX, "--version"], capture_output=True, text=True).stdout.strip(),
    }
    if CCNM:
        versions["ccnm"] = subprocess.run([CCNM, "--version"], capture_output=True,
                                          text=True).stdout.strip()
    runs = [globals()[name](out / name) for name in chosen]
    summary = {"hosts": versions, "runs": runs}
    (out / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

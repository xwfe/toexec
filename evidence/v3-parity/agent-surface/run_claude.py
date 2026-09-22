#!/usr/bin/env python3
"""受管 Claude Code 会话在各种 --tools 组合下，模型实际拿到哪些工具（v3 第 5 节第 6 步）。

零额度：模型接口是本机假服务（ANTHROPIC_BASE_URL），按请求内容回写好的回复，并把每次
请求原样存下来；API key 是假的；HOME 是临时空目录（没有任何凭据和用户设置）；进程树用
sandbox-exec 禁掉非本机出站。启动参数照 ccnm 的 `launch_cmd`（crates/ccnm-core/src/
provider/claude/mod.rs）拼，只把 `--tools` 换成各场景要测的值。

场景：
  baseline   --tools ""（ccnm 到 P45 为止的做法）
  names      一串候选名字：哪些是 2.1.x 认的内置工具、各自在请求里长什么样
  web        --tools "WebSearch"：服务端工具的形状
  deny       --tools "WebSearch,WebFetch" + settings 里 deny WebFetch：第二道锁生不生效
  subagent   --tools "Agent,WebSearch"：假模型让它派一个子代理，看子代理的工具表
  toolsearch 强开工具搜索（ENABLE_TOOL_SEARCH=true）：白名单不含 / 含 ToolSearch 时，
             MCP 工具会不会被放进延迟加载池（P15 说 --tools "" 时不会，白名单要重新确认）
  calls      假模型依次真调 WebSearch / WebFetch / Agent / TaskCreate，settings 的 allow
             只写 MCP 工具（ccnm 现在的写法）和再加上这四个各跑一次：print 模式没人答
             权限提示，要看哪个会被权限拦下

用法：python3 run_claude.py <新输出目录，放仓库外> [场景…]
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
CLAUDE = os.environ.get("PROBE_CLAUDE", "claude")
SANDBOX_PROFILE = """(version 1)
(allow default)
(deny network-outbound)
(allow network-outbound (remote ip "localhost:*"))
(allow network-outbound (remote unix-socket))
"""
SCENARIOS = ["baseline", "names", "web", "deny", "subagent", "toolsearch", "calls"]
# 候选名字：官方文档和 CLI 里出现过的内置工具名。认不出的会被忽略，这正是要测的。
CANDIDATES = [
    "WebSearch", "WebFetch", "TodoWrite", "Task", "Agent", "AskUserQuestion",
    "EnterPlanMode", "ExitPlanMode", "Skill", "ToolSearch", "SlashCommand",
    "TaskCreate", "TaskUpdate", "TaskList", "TaskGet", "TaskOutput", "TaskStop",
    "NotebookEdit", "KillShell", "BashOutput", "ListMcpResourcesTool", "ReadMcpResourceTool",
]
SUBAGENT_MARK = "SUBAGENT-PROBE-7f3a"


def sse(events):
    return "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events).encode()


def message(n, blocks, stop):
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
    events += [
        {"type": "message_delta", "delta": {"stop_reason": stop, "stop_sequence": None},
         "usage": {"output_tokens": 1}},
        {"type": "message_stop"},
    ]
    return events


def text_of(request):
    """这次请求最后一条 user 消息里的全部文字（含 tool_result）。"""
    msgs = request.get("messages") or []
    if not msgs:
        return ""
    content = msgs[-1].get("content")
    if isinstance(content, str):
        return content
    parts = []
    for part in content or []:
        if part.get("type") == "text":
            parts.append(part.get("text", ""))
        elif part.get("type") == "tool_result":
            parts.append("[tool_result]" + json.dumps(part.get("content"), ensure_ascii=False))
    return "\n".join(parts)


class MockModel:
    def __init__(self, out, script):
        self.out, self.script, self.step = out, script, 0
        model = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def reply_json(self, value, code=200):
                body = json.dumps(value).encode()
                self.send_response(code)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                self.reply_json({"data": []})

            def do_HEAD(self):
                self.send_response(200)
                self.end_headers()

            def do_POST(self):
                raw = self.rfile.read(int(self.headers.get("content-length") or 0))
                if "count_tokens" in self.path:
                    return self.reply_json({"input_tokens": 1})
                if "/v1/messages" not in self.path:
                    return self.reply_json({})
                n = model.step
                model.step += 1
                (model.out / f"model-request-{n}.json").write_bytes(raw)
                request = json.loads(raw)
                blocks, stop = model.script(n, request) or ([{"type": "text", "text": "ok"}], "end_turn")
                events = message(n, blocks, stop)
                if not request.get("stream"):
                    # 非流式请求（有的版本在旁路上会发）：拼成完整消息。
                    content = [b if b["type"] == "text" else {
                        "type": "tool_use", "id": b["id"], "name": b["name"], "input": b["input"]}
                        for b in blocks]
                    return self.reply_json({
                        "id": f"msg_{n}", "type": "message", "role": "assistant",
                        "model": "claude-probe", "content": content, "stop_reason": stop,
                        "stop_sequence": None, "usage": {"input_tokens": 1, "output_tokens": 1}})
                payload = sse(events)
                self.send_response(200)
                self.send_header("content-type", "text/event-stream")
                self.send_header("content-length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()


def claude(out, model, tools, deny=(), extra_env=None, timeout=120, allow_extra=()):
    """照 ccnm launch_cmd 拼一次受管会话（远端拓扑、print 模式）。"""
    home = out / "home"
    home.mkdir(parents=True)
    profile = out / "no-egress.sb"
    profile.write_text(SANDBOX_PROFILE)
    mcp = out / "mcp.json"
    mcp.write_text(json.dumps({"mcpServers": {"ccnm": {
        "type": "stdio", "command": sys.executable, "args": [str(HERE / "tiny_server.py")]}}}))
    settings = out / "settings.json"
    settings.write_text(json.dumps({"permissions": {
        "allow": ["mcp__ccnm__read_file", "mcp__ccnm__apply_patch", *allow_extra],
        "deny": ["Read", "Edit", "Write", "Grep", "Glob", "Bash", *deny]}}))
    cmd = ["sandbox-exec", "-f", str(profile), CLAUDE,
           "--tools", tools,
           "--mcp-config", str(mcp), "--strict-mcp-config",
           "--settings", str(settings), "--setting-sources", "user,project,local",
           "--permission-mode", "acceptEdits", "--session-id", str(uuid.uuid4()),
           "--print", "--output-format", "json", "--permission-prompts", "none",
           "--no-session-persistence"]
    env = {
        "HOME": str(home), "PATH": os.environ["PATH"],
        "ANTHROPIC_BASE_URL": f"http://127.0.0.1:{model.port}",
        "ANTHROPIC_API_KEY": "sk-ant-probe-not-a-real-key",
        "DISABLE_TELEMETRY": "1", "DISABLE_ERROR_REPORTING": "1", "DISABLE_AUTOUPDATER": "1",
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        **(extra_env or {}),
    }
    try:
        proc = subprocess.run(cmd, cwd=home, env=env, input=b"do the probe task",
                              capture_output=True, timeout=timeout)
        rc, stdout, stderr = proc.returncode, proc.stdout, proc.stderr
    except subprocess.TimeoutExpired as exc:
        rc, stdout, stderr = "timeout", exc.stdout or b"", exc.stderr or b""
    (out / "claude.stdout").write_bytes(stdout)
    (out / "claude.stderr").write_bytes(stderr)
    return rc


def requests(out):
    paths = sorted(out.glob("model-request-*.json"), key=lambda p: int(p.stem.rsplit("-", 1)[1]))
    return [json.loads(p.read_text()) for p in paths]


def tool_names(request):
    """请求里的工具：客户端工具给名字，服务端工具给 name(type)，延迟加载的标 [deferred]。"""
    names = []
    for tool in request.get("tools") or []:
        name = tool.get("name", "?")
        kind = tool.get("type")
        label = f"{name}({kind})" if kind and kind != "custom" else name
        names.append(label + (" [deferred]" if tool.get("defer_loading") else ""))
    return names


def main_request(reqs):
    """带工具最多的那次就是主循环的请求（旁路的小请求不带工具）。"""
    with_tools = [r for r in reqs if r.get("tools")]
    return max(with_tools, key=lambda r: len(r["tools"])) if with_tools else {}


def simple(out, tools, deny=(), extra_env=None):
    out.mkdir(parents=True)
    model = MockModel(out, lambda n, req: None)
    rc = claude(out, model, tools, deny, extra_env)
    reqs = requests(out)
    main = main_request(reqs)
    return {"tools_flag": tools, "deny_extra": list(deny), "rc": rc,
            "model_requests": len(reqs), "tools_seen": tool_names(main),
            "stderr": (out / "claude.stderr").read_text(errors="replace")[-600:]}


def baseline(out):
    return {"scenario": "baseline", **simple(out, "")}


def names(out):
    result = simple(out, ",".join(CANDIDATES))
    seen = {name.split("(")[0] for name in result["tools_seen"]}
    return {"scenario": "names", **result,
            "recognized": [c for c in CANDIDATES if c in seen],
            "not_recognized": [c for c in CANDIDATES if c not in seen]}


def web(out):
    result = simple(out, "WebSearch")
    main = main_request(requests(out))
    shapes = [t for t in main.get("tools", []) if "web" in json.dumps(t).lower()]
    return {"scenario": "web", **result, "web_tool_shapes": shapes}


def deny(out):
    return {"scenario": "deny", **simple(out, "WebSearch,WebFetch", deny=("WebFetch",))}


def subagent(out):
    out.mkdir(parents=True)
    state = {"agent_tool": None}

    def script(n, req):
        said = text_of(req)
        names_here = [t.get("name") for t in req.get("tools") or []]
        if SUBAGENT_MARK in said and "[tool_result]" not in said:
            # 子代理自己的第一次请求：它的工具表就是要看的东西。
            return [{"type": "text", "text": "sub done"}], "end_turn"
        if "[tool_result]" in said:
            return [{"type": "text", "text": "main done"}], "end_turn"
        tool = next((t for t in ("Agent", "Task") if t in names_here), None)
        if tool and state["agent_tool"] is None:
            state["agent_tool"] = tool
            return [{"type": "tool_use", "id": "toolu_probe_1", "name": tool, "input": {
                "description": "probe", "prompt": f"{SUBAGENT_MARK}: list your tools",
                "subagent_type": "general-purpose"}}], "tool_use"
        return None

    model = MockModel(out, script)
    rc = claude(out, model, "Agent,WebSearch")
    reqs = requests(out)
    # 子代理的请求在系统提示第一段的计费头里带 cc_is_subagent=true（2.1.278 实测）。
    sub = [r for r in reqs if "cc_is_subagent=true" in json.dumps(r.get("system"))]
    return {"scenario": "subagent", "rc": rc, "model_requests": len(reqs),
            "agent_tool_name": state["agent_tool"],
            "main_tools": tool_names(main_request(reqs)),
            "subagent_tools": tool_names(sub[0]) if sub else None,
            "stderr": (out / "claude.stderr").read_text(errors="replace")[-600:]}


def toolsearch(out):
    force = {"ENABLE_TOOL_SEARCH": "true"}
    return {"scenario": "toolsearch",
            "without": simple(out / "without", "WebSearch", extra_env=force),
            "with": simple(out / "with", "WebSearch,ToolSearch", extra_env=force),
            "empty": simple(out / "empty", "", extra_env=force)}


CALLS = ["WebSearch", "WebFetch", "Agent", "TaskCreate"]


def calls(out):
    return {"scenario": "calls",
            "allow_mcp_only": call_each(out / "allow-mcp-only", ()),
            "allow_agent_tools": call_each(out / "allow-agent-tools", tuple(CALLS))}


def call_each(out, allow_extra):
    out.mkdir(parents=True)
    port = {}

    def inputs(name):
        return {
            "WebSearch": {"query": "ccnm probe query"},
            # 指到假服务自己：出站被沙箱挡着，要看的只是权限这一关。
            "WebFetch": {"url": f"http://127.0.0.1:{port['n']}/page", "prompt": "summarize"},
            "Agent": {"description": "probe", "prompt": f"{SUBAGENT_MARK}: say hi",
                      "subagent_type": "general-purpose", "run_in_background": False},
            "TaskCreate": {"subject": "probe", "description": "probe task"},
        }[name]

    def script(n, req):
        names_here = [t.get("name") for t in req.get("tools") or []]
        is_main = "mcp__ccnm__read_file" in names_here and \
            "cc_is_subagent=true" not in json.dumps(req.get("system"))
        if not is_main:
            return [{"type": "text", "text": "side ok"}], "end_turn"
        done = sum(1 for m in req.get("messages", []) if isinstance(m.get("content"), list)
                   for part in m["content"] if part.get("type") == "tool_result")
        if done < len(CALLS):
            name = CALLS[done]
            return [{"type": "tool_use", "id": f"toolu_probe_{done}", "name": name,
                     "input": inputs(name)}], "tool_use"
        return [{"type": "text", "text": "all done"}], "end_turn"

    model = MockModel(out, script)
    port["n"] = model.port
    rc = claude(out, model, ",".join(CALLS), allow_extra=allow_extra, timeout=240)
    results = {}
    for req in requests(out):
        for m in req.get("messages", []):
            for part in m.get("content") if isinstance(m.get("content"), list) else []:
                if part.get("type") == "tool_result" and part["tool_use_id"].startswith("toolu_probe_"):
                    i = int(part["tool_use_id"].rsplit("_", 1)[1])
                    content = part.get("content")
                    text = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)
                    results[CALLS[i]] = {"is_error": bool(part.get("is_error")), "text": text[:300]}
    return {"allow_extra": list(allow_extra), "rc": rc, "model_requests": len(requests(out)),
            "results": results,
            "stderr": (out / "claude.stderr").read_text(errors="replace")[-400:]}


def main():
    out = Path(sys.argv[1]).resolve()
    chosen = sys.argv[2:] or SCENARIOS
    version = subprocess.run([CLAUDE, "--version"], capture_output=True, text=True).stdout.strip()
    runs = [globals()[name](out / name) for name in chosen]
    summary = {"host": version, "runs": runs}
    out.mkdir(parents=True, exist_ok=True)
    (out / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

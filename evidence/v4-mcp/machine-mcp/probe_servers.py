#!/usr/bin/env python3
"""Measure what installed MCP servers answer to initialize + tools/list.

Reads ~/.claude.json (user mcpServers) and ~/.codex/config.toml, starts the
named servers the way their config says, and records per server: transport,
protocol version it answered, how long startup took, tool count, bytes of
tools/list, the largest tool entry, and whether stdout carried non-JSON lines.
Never records env values, header values, or argument lists.
"""
import json, os, subprocess, sys, threading, time, tomllib, urllib.request, re, queue

CLIENT_VERSION = "2025-06-18"


def expand(s):
    def sub(m):
        name, _, default = m.group(1).partition(":-")
        return os.environ.get(name, default if _ else "")
    return re.sub(r"\$\{([^}]+)\}", sub, s) if isinstance(s, str) else s


def load():
    out = {}
    d = json.load(open(os.path.expanduser("~/.claude.json")))
    for k, v in d.get("mcpServers", {}).items():
        out.setdefault(("claude", k), v)
    c = tomllib.load(open(os.path.expanduser("~/.codex/config.toml"), "rb"))
    for k, v in c.get("mcp_servers", {}).items():
        out.setdefault(("codex", k), v)
    return out


def stdio(cfg, timeout):
    cmd = [expand(cfg["command"])] + [expand(a) for a in cfg.get("args", [])]
    env = dict(os.environ)
    env.update({k: expand(v) for k, v in cfg.get("env", {}).items()})
    t0 = time.time()
    p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                         stderr=subprocess.PIPE, env=env, cwd=os.path.expanduser("~"))
    q = queue.Queue()
    junk = []

    def pump():
        for line in p.stdout:
            q.put(line)
        q.put(None)
    threading.Thread(target=pump, daemon=True).start()
    threading.Thread(target=lambda: p.stderr.read(), daemon=True).start()

    def send(obj):
        p.stdin.write((json.dumps(obj) + "\n").encode()); p.stdin.flush()

    def wait(i, deadline):
        while True:
            left = deadline - time.time()
            if left <= 0:
                raise TimeoutError
            line = q.get(timeout=left)
            if line is None:
                raise EOFError("closed")
            try:
                m = json.loads(line)
            except Exception:
                junk.append(len(line)); continue
            if m.get("id") == i and ("result" in m or "error" in m):
                return m
            if "method" in m and "id" in m:
                send({"jsonrpc": "2.0", "id": m["id"], "result": {}})

    try:
        send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": CLIENT_VERSION, "capabilities": {},
            "clientInfo": {"name": "probe", "version": "0"}}})
        init = wait(1, t0 + timeout)
        started = time.time() - t0
        send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        tools, cursor, n = [], None, 2
        while True:
            send({"jsonrpc": "2.0", "id": n, "method": "tools/list",
                  "params": {"cursor": cursor} if cursor else {}})
            r = wait(n, time.time() + 30)
            n += 1
            tools += r["result"].get("tools", [])
            cursor = r["result"].get("nextCursor")
            if not cursor:
                break
        return init["result"], tools, started, len(junk)
    finally:
        try:
            p.stdin.close()
        except Exception:
            pass
        try:
            p.wait(5)
        except Exception:
            p.kill()


def http(cfg, timeout, headers_key):
    url = expand(cfg["url"])
    hdrs = {k: expand(v) for k, v in cfg.get(headers_key, {}).items()}
    session = {}

    def post(obj):
        h = {"Content-Type": "application/json",
             "Accept": "application/json, text/event-stream", "User-Agent": "gld-probe/0", **hdrs}
        if "sid" in session:
            h["Mcp-Session-Id"] = session["sid"]
            h["MCP-Protocol-Version"] = session["ver"]
        req = urllib.request.Request(url, json.dumps(obj).encode(), h)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            sid = resp.headers.get("Mcp-Session-Id")
            if sid:
                session["sid"] = sid
            body = resp.read().decode()
            ctype = resp.headers.get("Content-Type", "")
        if not body.strip():
            return None, ctype
        if "event-stream" in ctype:
            for line in body.splitlines():
                if line.startswith("data:"):
                    m = json.loads(line[5:].strip())
                    if m.get("id") == obj.get("id"):
                        return m, ctype
            return None, ctype
        return json.loads(body), ctype

    t0 = time.time()
    init, ctype = post({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
        "protocolVersion": CLIENT_VERSION, "capabilities": {},
        "clientInfo": {"name": "probe", "version": "0"}}})
    started = time.time() - t0
    session["ver"] = init["result"]["protocolVersion"]
    post({"jsonrpc": "2.0", "method": "notifications/initialized"})
    tools, cursor, n = [], None, 2
    while True:
        r, _ = post({"jsonrpc": "2.0", "id": n, "method": "tools/list",
                     "params": {"cursor": cursor} if cursor else {}})
        n += 1
        tools += r["result"].get("tools", [])
        cursor = r["result"].get("nextCursor")
        if not cursor:
            break
    return init["result"], tools, started, ctype


def main():
    wanted = sys.argv[1:]
    servers = load()
    rows = []
    for (source, name), cfg in servers.items():
        if wanted and name not in wanted:
            continue
        kind = cfg.get("type") or ("http" if "url" in cfg else "stdio")
        row = {"server": name, "source": source, "transport": kind}
        try:
            if kind == "stdio":
                init, tools, started, junk = stdio(cfg, 90)
                row["non_json_stdout_lines"] = junk
            else:
                init, tools, started, ctype = http(
                    cfg, 30, "headers" if source == "claude" else "http_headers")
                row["response_type"] = ctype.split(";")[0]
            sizes = sorted(((len(json.dumps(t)), t["name"]) for t in tools), reverse=True)
            row.update({
                "protocol": init.get("protocolVersion"),
                "server_info": {k: init.get("serverInfo", {}).get(k) for k in ("name", "version")},
                "instructions_bytes": len(init.get("instructions") or ""),
                "startup_s": round(started, 2),
                "tools": len(tools),
                "tools_list_bytes": len(json.dumps(tools)),
                "largest_tool": {"name": sizes[0][1], "bytes": sizes[0][0]} if sizes else None,
                "longest_description": max((len(t.get("description") or "") for t in tools), default=0),
                "annotated_read_only": sum(1 for t in tools if (t.get("annotations") or {}).get("readOnlyHint")),
                "output_schema": sum(1 for t in tools if t.get("outputSchema")),
            })
        except Exception as e:  # noqa
            row["error"] = f"{type(e).__name__}: {str(e)[:120]}"
        rows.append(row)
        print(json.dumps(row, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()

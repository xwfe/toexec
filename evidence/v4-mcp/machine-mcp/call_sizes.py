"""Call a few read-only tools on network servers and record result sizes only."""
import json, os, subprocess, threading, queue, time, tomllib, urllib.request
def stdio_session(cmd):
    p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, cwd=os.path.expanduser('~'))
    q = queue.Queue()
    threading.Thread(target=lambda: [q.put(l) for l in p.stdout] and q.put(None), daemon=True).start()
    n = [0]
    def req(method, params):
        n[0] += 1
        p.stdin.write((json.dumps({"jsonrpc":"2.0","id":n[0],"method":method,"params":params})+"\n").encode()); p.stdin.flush()
        while True:
            m = json.loads(q.get(timeout=120))
            if m.get("id") == n[0]: return m
    req("initialize", {"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"probe","version":"0"}})
    p.stdin.write(b'{"jsonrpc":"2.0","method":"notifications/initialized"}\n'); p.stdin.flush()
    return req, p
def http_session(url, hdrs):
    s = {}
    n = [0]
    def post(method, params, notify=False):
        h = {"Content-Type":"application/json","Accept":"application/json, text/event-stream","User-Agent":"gld-probe/0",**hdrs}
        if "sid" in s: h["Mcp-Session-Id"] = s["sid"]; h["MCP-Protocol-Version"] = "2025-06-18"
        body = {"jsonrpc":"2.0","method":method,"params":params}
        if not notify: n[0] += 1; body["id"] = n[0]
        with urllib.request.urlopen(urllib.request.Request(url, json.dumps(body).encode(), h), timeout=120) as r:
            if r.headers.get("Mcp-Session-Id"): s["sid"] = r.headers["Mcp-Session-Id"]
            text = r.read().decode(); ct = r.headers.get("Content-Type","")
        if notify or not text.strip(): return None
        if "event-stream" in ct:
            for line in text.splitlines():
                if line.startswith("data:"):
                    m = json.loads(line[5:])
                    if m.get("id") == n[0]: return m
        return json.loads(text)
    post("initialize", {"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"probe","version":"0"}})
    post("notifications/initialized", {}, notify=True)
    return lambda m, p: post(m, p)
def measure(label, r):
    res = r.get("result") or {}
    content = res.get("content", [])
    print(json.dumps({"call": label, "error": r.get("error",{}).get("message"), "isError": res.get("isError"),
        "content_items": [(c.get("type"), len(c.get("text","") or c.get("data",""))) for c in content],
        "structured_bytes": len(json.dumps(res["structuredContent"])) if "structuredContent" in res else None,
        "total_bytes": len(json.dumps(res))}))
req, p = stdio_session(["bunx","-y","@upstash/context7-mcp@latest"])
names = [t["name"] for t in req("tools/list", {})["result"]["tools"]]
print("context7 tools", names)
r = req("tools/call", {"name": names[0], "arguments": {"libraryName": "react", "query": "useEffect cleanup"}}); measure("context7 "+names[0]+" react", r)
lib = "/reactjs/react.dev"
r = req("tools/call", {"name": names[1], "arguments": {"libraryId": lib, "query": "useEffect cleanup"}}); measure("context7 "+names[1], r)
p.stdin.close(); p.wait(5)
c = tomllib.load(open(os.path.expanduser('~/.codex/config.toml'),'rb'))['mcp_servers']['deepwiki']
dw = http_session(c['url'], {})
r = dw("tools/call", {"name":"read_wiki_structure","arguments":{"repoName":"modelcontextprotocol/rust-sdk"}}); measure("deepwiki read_wiki_structure", r)
r = dw("tools/call", {"name":"read_wiki_contents","arguments":{"repoName":"modelcontextprotocol/rust-sdk"}}); measure("deepwiki read_wiki_contents", r)

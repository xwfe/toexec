"""Drive a gld service's MCP relay tools over HTTP like a client would.

Usage: e2e_relay.py <base url> <bearer token>. Prints sizes and short
excerpts only, never tokens.
"""
import json, sys, time, urllib.request

URL, TOKEN = sys.argv[1], sys.argv[2]
n = [0]

def rpc(method, params=None):
    n[0] += 1
    body = {"jsonrpc": "2.0", "id": n[0], "method": method, "params": params or {}}
    req = urllib.request.Request(URL, json.dumps(body).encode(), {
        "Content-Type": "application/json", "Accept": "application/json, text/event-stream",
        "Authorization": f"Bearer {TOKEN}"})
    t = time.time()
    with urllib.request.urlopen(req, timeout=180) as r:
        raw = r.read()
    return json.loads(raw), len(raw), time.time() - t

def call(tool, args):
    reply, size, took = rpc("tools/call", {"name": tool, "arguments": args})
    return reply["result"], size, took

init, _, _ = rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "e2e", "version": "0"}})
instr = init["result"]["instructions"]
print("initialize mentions relay:", "list_mcp_tools" in instr)
tools, size, _ = rpc("tools/list")
names = [t["name"] for t in tools["result"]["tools"]]
relay = [t for t in tools["result"]["tools"] if t["name"] in ("list_mcp_tools", "call_mcp_tool", "read_mcp_result")]
print("tools/list:", len(names), "tools,", size, "bytes; relay tools:", [t["name"] for t in relay])
print("list_mcp_tools description:", relay[0]["description"][:260])
print("relay tools need workspace:", any("workspace" in t["inputSchema"].get("required", []) for t in relay))

res, size, took = call("list_mcp_tools", {})
print("\noverview:", json.dumps(res["structuredContent"]["servers"], ensure_ascii=False)[:600])

for server in ["context7", "deepwiki", "exa-search", "mcp-time", "Filesystem"]:
    res, size, took = call("list_mcp_tools", {"server": server})
    sc = res["structuredContent"]
    if res.get("isError"):
        print(f"\nlist {server}: ERROR {sc['error']['code']}: {sc['error']['message'][:300]}")
        continue
    print(f"\nlist {server}: {sc['count']} tools, {size} bytes, {took:.1f}s, omitted={sc.get('omitted')}, instructions={len(sc.get('instructions') or '')}B")
    print("   ", [t["name"] for t in sc["tools"]])

res, size, took = call("call_mcp_tool", {"server": "mcp-time", "tool": "get_current_time", "arguments": {"timezone": "Asia/Shanghai"}})
print("\nmcp-time get_current_time: isError", res["isError"], "text", len(res["content"][0]["text"]), "chars", res.get("_meta"))

res, size, took = call("call_mcp_tool", {"server": "context7", "tool": "resolve-library-id", "arguments": {"libraryName": "react", "query": "useEffect cleanup"}})
print("\ncontext7 resolve-library-id:", res["isError"], len(res["content"][0]["text"]), "chars,", f"{took:.1f}s")

res, size, took = call("call_mcp_tool", {"server": "deepwiki", "tool": "read_wiki_contents", "arguments": {"repoName": "modelcontextprotocol/rust-sdk"}})
parts = [c for c in res["content"] if c["type"] == "text"]
print("\ndeepwiki read_wiki_contents: response", size, "bytes;", "structuredContent" in res, "structured;", len(parts), "text items")
first, note = parts[0]["text"], parts[-1]["text"]
print("  first part", len(first.encode()), "bytes; note:", note[:220])
ref = note.split("ref=")[1].split()[0]
offset = int(note.split("offset=")[1].split(".")[0].split()[0])
whole = first
reads = 0
while True:
    res, size, took = call("read_mcp_result", {"ref": ref, "offset": offset, "max_bytes": 262144})
    text, tail = res["content"][0]["text"], res["content"][1]["text"]
    whole += text
    offset += len(text.encode())
    reads += 1
    if "that is the end" in tail:
        break
print(f"  read back in {reads} more calls: {len(whole.encode())} bytes total; last note: {tail}")

res, size, took = call("call_mcp_tool", {"server": "deepwiki", "tool": "no_such_tool"})
print("\nunknown tool:", res["structuredContent"]["error"]["code"], res["structuredContent"]["error"]["message"][:160])
res, size, took = call("call_mcp_tool", {"server": "desktop-commander", "tool": "start_process"})
print("not turned on:", res["structuredContent"]["error"]["code"], res["structuredContent"]["error"]["message"][:200])
res, size, took = call("call_mcp_tool", {"server": "Filesystem", "tool": "list_allowed_directories"})
print("Filesystem list_allowed_directories: isError", res["isError"], "text", len(res["content"][0]["text"]), "chars")

res, size, took = call("list_mcp_tools", {})
print("\noverview after:", [(s["name"], s["state"], len(s.get("tools", []))) for s in res["structuredContent"]["servers"]])

#!/usr/bin/env bash
# 让**未登录**的 Claude Code 连探针 MCP server，看它调了哪些方法、怎么处理工具 description。
# 零额度：HOME 是临时空目录（没有凭据），模型地址指向一个关着的本机端口；MCP 连接发生在
# 认证失败之前，所以照样能观察到。用法：run_claude.sh <新输出目录，放仓库外>
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
RAW="${1:?需要一个仓库外的输出目录}"
mkdir -p "$RAW/home"
PY="$(python3 -c 'import sys;print(sys.executable)')"
cat > "$RAW/mcp.json" <<JSON
{"mcpServers":{"probe":{"type":"stdio","command":"$PY","args":["$HERE/probe_server.py"],"env":{"PROBE_LOG":"$RAW/probe-methods.jsonl"}}}}
JSON
cd "$RAW"
run() { env -i HOME="$RAW/home" PATH="$PATH" ANTHROPIC_BASE_URL=http://127.0.0.1:9 "$@"; }
run claude auth status > "$RAW/auth-status.json" 2>&1 || true
grep -q '"loggedIn": false' "$RAW/auth-status.json" || { echo "这个环境里 CLI 是登录状态，停：不能保证零额度" >&2; exit 2; }
set +e
run claude -p "say ok" --output-format json --no-session-persistence \
  --strict-mcp-config --mcp-config "$RAW/mcp.json" --debug-file "$RAW/debug.log" \
  > "$RAW/result.json" 2> "$RAW/stderr.txt"
set -e
python3 - "$RAW" <<'PY'
import json, re, sys
raw = sys.argv[1]
lines = [json.loads(l) for l in open(f"{raw}/probe-methods.jsonl")]
result = json.load(open(f"{raw}/result.json"))
debug = open(f"{raw}/debug.log", errors="replace").read()
summary = {
    "host": lines[0]["client"],
    "client_capabilities": lines[0]["client_caps"],
    "mcp_methods_called": [l["method"] for l in lines],
    "debug_lines": re.findall(r'MCP server "probe": ((?:Tool|Server|Connection established)[^\n]*)', debug),
    "model_usage": {k: result.get("usage", {}).get(k) for k in ("input_tokens", "output_tokens")},
    "duration_api_ms": result.get("duration_api_ms"),
}
json.dump(summary, open(f"{raw}/summary.json", "w"), ensure_ascii=False, indent=2)
print(json.dumps(summary, ensure_ascii=False, indent=2))
PY

#!/usr/bin/env bash
# 用法：defer_check.sh <ccnm 二进制> <仓库外输出目录> <alwaysLoad: true|false>
# 未登录的真实 Claude Code 连外部 coding 模式的 ccnm，看 debug 日志里延迟加载池有多大。不发模型请求。
set -euo pipefail
BIN="$1"; OUT="$2"; ALWAYS="$3"
rm -rf "$OUT"; mkdir -p "$OUT"/{project,home,state,run}; OUT="$(cd "$OUT" && pwd -P)"
echo "hello" > "$OUT/project/hello.txt"
cat > "$OUT/config.toml" <<TOML
this = "runtime"
[nodes.runtime]
[nodes.agent]
ssh = "agent-node.invalid"
[workspaces.demo]
root = "$OUT/project"
agent = { node = "agent", instance = "claude-main" }
external_mcp = "coding"
allow_unconfined_exec = true
TOML
WIRE="$(python3 -c 'import base64,json;print(base64.urlsafe_b64encode(json.dumps({"protocol":5,"workspace":"demo","session":"q2-defer","mode":"coding"}).encode()).decode().rstrip("="))')"
cat > "$OUT/mcp.json" <<JSON
{"mcpServers":{"ccnm":{"type":"stdio","alwaysLoad":$ALWAYS,"command":"/usr/bin/env",
 "args":["-i","PATH=/usr/bin:/bin","HOME=$OUT/home","XDG_STATE_HOME=$OUT/state","CCNM_CONFIG=$OUT/config.toml","$BIN","internal","mcp-serve","--payload","$WIRE"]}}}
JSON
cd "$OUT/run"
set +e
env -i HOME="$HOME" PATH="$PATH" USER="$USER" LANG=en_US.UTF-8 claude -p "reply ok" --output-format json --no-session-persistence \
  --strict-mcp-config --mcp-config "$OUT/mcp.json" --debug-file "$OUT/debug.log" > "$OUT/result.json" 2> "$OUT/stderr.txt"
set -e
grep -E 'MCP server "ccnm": (Successfully connected|Connection failed)|ToolSearch|Tool search|Dynamic tool loading|deferred' "$OUT/debug.log" | sed -E 's/^[0-9TZ:.-]+ //' | cut -c1-200
python3 -c "import json;d=json.load(open('$OUT/result.json'));print('result:', d.get('result'), 'input_tokens:', d.get('usage',{}).get('input_tokens'))"

#!/usr/bin/env bash
# V2-Q1：一次真实 Claude Code 回合，看超长 MCP instructions 实际进到模型上下文的有多少。
# 用法：evidence/v2-q1/run.sh <原始输出目录，放仓库外>
# 每跑一次消耗 1 次订阅模型运行，记账见同目录 README.md。
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
RAW="${1:?需要一个仓库外的原始输出目录}"
mkdir -p "$RAW"

# 标记只在本次进程环境里，不写进任何文件；Claude Code 拉起 stdio 服务器时继承环境。
Q1_MARKERS="$(python3 -c 'import secrets;print(",".join("WKQ1-"+secrets.token_hex(4).upper() for _ in range(5)))')"
export Q1_MARKERS

cat > "$RAW/mcp.json" <<JSON
{"mcpServers":{"q1probe":{"type":"stdio","command":"python3","args":["$HERE/probe_server.py"]}}}
JSON

PROMPT='你的上下文里有一段 MCP 服务器 q1probe 提供的说明（MCP Server Instructions）。
只根据你实际看到的内容回答，不调用任何工具，不猜测：
1. 按出现顺序逐行原样列出其中所有形如 "WKQ1-" 后跟 8 个大写十六进制字符的标记，每行一个；一个都没看到就写 NONE。
2. 最后单独一行写 TRUNCATED_MARK=是 或 TRUNCATED_MARK=否，表示这段说明末尾是否带有 "[truncated]" 字样。'

cd "$RAW"
set +e
claude -p "$PROMPT" \
  --model sonnet \
  --output-format json \
  --no-session-persistence \
  --strict-mcp-config --mcp-config "$RAW/mcp.json" \
  --tools "" --disallowedTools "mcp__q1probe__noop" \
  --debug-file "$RAW/debug.log" \
  > "$RAW/result.json" 2> "$RAW/stderr.txt"
status=$?
set -e

printf '%s\n' "$Q1_MARKERS" > "$RAW/markers.txt"
claude --version > "$RAW/version.txt"
echo "exit=$status raw=$RAW"

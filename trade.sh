#!/bin/bash
set -euo pipefail

cd /Users/sonic/.openclaw/workspace/trading-bot
if [ -f .env ]; then
  set -a
  . ./.env
  set +a
fi

if pgrep -f "/Users/sonic/.openclaw/workspace/trading-bot/trade_v2.py run" >/dev/null 2>&1; then
  exit 0
fi

exec python3 /Users/sonic/.openclaw/workspace/trading-bot/trade_v2.py run "$@"

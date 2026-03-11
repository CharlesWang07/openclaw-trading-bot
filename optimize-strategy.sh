#!/bin/bash
set -euo pipefail

cd /Users/sonic/.openclaw/workspace/trading-bot
if [ -f .env ]; then
  set -a
  . ./.env
  set +a
fi

exec python3 /Users/sonic/.openclaw/workspace/trading-bot/update_strategy_status.py "$@"

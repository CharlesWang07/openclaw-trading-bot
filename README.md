# OpenClaw 24小时自动交易挑战

本项目是一个 Binance 合约交易看板 + `v2` 自动交易脚本。

## 配置 API Key

1. 复制 `.env.example` 为 `.env`
2. 填入你自己的 Binance API Key

```bash
cp .env.example .env
```

`.env` 内容示例：

```env
BINANCE_API_KEY=your_binance_api_key_here
BINANCE_SECRET_KEY=your_binance_secret_key_here
```

## 运行

实盘运行：

```bash
python3 trade_v2.py run
```

策略状态刷新：

```bash
python3 update_strategy_status.py
```

## 说明

- `.env` 不会被提交到 Git
- 页面主文件是 `index.html`
- 当前交易核心脚本是 `trade_v2.py`
- 页面策略展示只读取 `status.json` 中的 `strategy_v2`

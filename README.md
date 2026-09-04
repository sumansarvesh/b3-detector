# B3 Flat BB Detector - Trading Scanner

Advanced Bollinger Band breakout detection scanner for B3 exchange with multi-exchange support.

## Features

- Real-time B3 stock scanning with Bollinger Band breakout detection
- Multi-exchange support (Delta Exchange, Upstox)
- Telegram alerts for buy/sell signals
- Always-on Railway deployment for continuous scanning
- Enhanced detection with flat relaxation and multi-candle window

## Files

| File | Description |
|------|-------------|
| `s9_scanner.py` | Main S9 Scanner — Multi-Exchange (Delta + Upstox) with BB breakout detection |
| `s9_detector.py` | S9 Pivot Confluence Blast detector engine |
| `s9_upgrades.py` | S9 enhancements: star ladder, pivots, entry gates, blacklist logic |
| `b3_enhanced_detector.py` | Enhanced B3 detector with flat relaxation, 4-candle window, and CE+PE alerts |
| `tps_unified_scanner.py` | Unified TPS scanner with Railway always-on Telegram integration |
| `nixpacks.toml` | Railway/Nixpacks deployment configuration |
| `requirements.txt` | Python dependencies |
| `Procfile` | Railway process definition |

## Setup

### 1. Clone the repository

```bash
git clone https://github.com/sumansarvesh/b3-detector.git
cd b3-detector
```

### 2. Install dependencies

```bash
pip install -r requirements.txt
```

### 3. Configure environment variables

Create a `.env` file in the project root with the required credentials:

```env
UPSTOX_API_KEY=your_upstox_api_key_here
UPSTOX_SECRET_KEY=your_upstox_secret_key_here
UPSTOX_ACCESS_TOKEN=your_upstox_access_token_here
UPSTOX_REFRESH_TOKEN=your_upstox_refresh_token_here
TELEGRAM_BOT_TOKEN=your_telegram_bot_token_here
TELEGRAM_CHAT_ID=your_telegram_chat_id_here
DELTA_EXCHANGE_API_KEY=your_delta_exchange_api_key_here
```

> **Note:** Never commit `.env` file to GitHub. It is included in `.gitignore` for your safety.

### 4. Run the scanner

```bash
# Run the main scanner
python3 s9_scanner.py

# Or run the enhanced B3 detector
python3 b3_enhanced_detector.py
```

## Deployment

### Railway

This project is configured for Railway deployment using Nixpacks.

1. Push this repository to GitHub
2. Connect your repository to Railway
3. Set environment variables in Railway dashboard
4. Deploy — the scanner will run continuously

**Procfile:**
```
worker: python3 s9_scanner.py
```

**Nixpacks config** (`nixpacks.toml`):
```toml
[phases.setup]
nixPkgs = ["python311"]

[phases.install]
cmds = ["pip install --only-binary=:all: -r requirements.txt"]

[start]
cmd = "python3 s9_scanner.py"
```

## How It Works

### S9 Scanner Logic
- **Pivot Confluence Blast**: Daily Pivot (H+L+C/3) must be within Bollinger Bands across all timeframes (5m, 15m, 30m, 1h, Daily)
- **Blast Trigger**: When a 5m candle closes above BB upper band while pivots are inside BB on all TFs
- **Buying-only setup**: Only upside breakouts are detected

### Star Ladder System
- Base signal = 2 stars minimum
- Additional stars for volume spike, flat BB, ladder confirmation
- Blacklist activated after 5 consecutive base-level signals

### Multi-Exchange Universe
- **Delta Exchange**: BTCUSD, ETHUSD (4th far OTM CE/PE)
- **Upstox NSE**: Nifty 100 stocks + monthly expiry options
- **Upstox Indices**: NIFTY, BANKNIFTY, SENSEX weekly expiry options
- **Upstox MCX**: Commodity options (Gold, Silver, Crude) monthly expiry

### Telegram Alerts
- Real-time S9 signal notifications
- Always-on Railway worker keeps your phone connected
- Star ratings and pivot levels included in alerts

## Requirements

- Python 3.9+
- Telegram Bot (for alerts)
- API credentials for Delta Exchange and/or Upstox

## License

MIT

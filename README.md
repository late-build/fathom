# fathom

Solana memecoin trading engine. Backtest and execute strategies on graduated pump.fun tokens.

```
pip install fathom-trading
```

## What is this

Fathom is an event-driven trading engine built for Solana memecoins. It monitors pump.fun graduations, streams post-graduation price data, and lets you write strategies that trade automatically — then backtest those same strategies against historical graduation data.

**The core loop:**
1. Token graduates from pump.fun → PumpSwap/Raydium
2. Fathom detects it in real-time via Helius WebSocket
3. Your strategy evaluates it (holders, liquidity, dev behavior)
4. If it passes, execute a swap via Jupiter
5. Manage the position (TP/SL/trailing stop/dev sell detection)

Same code runs in backtest mode against historical data. No rewriting.

## Quick start

```python
from fathom import Engine
from fathom.adapters.jupiter import JupiterAdapter
from fathom.adapters.pumpfun.graduation import GraduationMonitor
from fathom.strategies.graduation_sniper import GraduationSniper

engine = Engine()

# Monitor pump.fun graduations
engine.add_data_feed(GraduationMonitor(
    helius_api_key="YOUR_HELIUS_KEY",
    min_bonding_progress=80,
    min_holders=100,
    track_dev_wallets=True,
))

# Execute via Jupiter
engine.add_adapter(JupiterAdapter(
    rpc_url="https://mainnet.helius-rpc.com/?api-key=YOUR_KEY",
    wallet_path="~/.config/solana/id.json",
))

# Run graduation sniper strategy
engine.add_strategy(GraduationSniper(
    position_size_usd=50,
    max_positions=3,
    min_holders=100,
    take_profit_pct=0.50,      # 50% TP
    stop_loss_pct=0.20,        # 20% SL
    trailing_stop_pct=0.15,    # 15% trailing after 30% gain
    max_hold_seconds=300,      # 5 min max hold
    exit_on_dev_sell=True,     # auto-exit if dev dumps
))

engine.run()
```

## Backtesting

```python
from fathom import Engine
from fathom.adapters.pumpfun.graduation import GraduationMonitor
from fathom.strategies.graduation_sniper import GraduationSniper

engine = Engine(mode="backtest")

# Load historical graduation data
monitor = GraduationMonitor(helius_api_key="")
monitor.load_historical_graduations([
    {
        "mint": "...",
        "symbol": "EXAMPLE",
        "graduated_at": 1708000000,
        "initial_price_usd": 0.000042,
        "pool_type": "pumpswap",
        "price_history": [
            {"timestamp": 1708000060, "price": 0.000045, "volume_5m": 12000},
            {"timestamp": 1708000120, "price": 0.000063, "volume_5m": 28000},
            # ...
        ]
    }
])

engine.add_data_feed(monitor)
engine.add_strategy(GraduationSniper(
    position_size_usd=50,
    take_profit_pct=0.50,
    stop_loss_pct=0.20,
))

engine.run()
# Check strategy.stats for PnL, win rate, etc.
```

## Architecture

```
┌─────────────────────────────────────────────────────┐
│                    Fathom Engine                     │
│                                                     │
│  ┌───────────────┐  ┌────────────┐  ┌───────────┐  │
│  │   EventBus    │──│  Strategy  │──│   Risk    │  │
│  │ (nanosecond)  │  │  (yours)   │  │  Manager  │  │
│  └───────┬───────┘  └────────────┘  └───────────┘  │
│          │                                          │
│  ┌───────┴──────────────────────────────────────┐   │
│  │              Data Feeds                       │   │
│  │  ┌──────────────┐  ┌────────┐  ┌──────────┐ │   │
│  │  │  Graduation  │  │ Helius │  │DexScreen │ │   │
│  │  │   Monitor    │  │   WS   │  │  er API  │ │   │
│  │  └──────┬───────┘  └───┬────┘  └────┬─────┘ │   │
│  └─────────┼──────────────┼────────────┼────────┘   │
│            │              │            │            │
│  ┌─────────┴──────────────┴────────────┴────────┐   │
│  │             Execution Adapters                │   │
│  │  ┌─────────┐  ┌─────────┐  ┌──────────────┐ │   │
│  │  │ Jupiter │  │Raydium  │  │  PumpSwap    │ │   │
│  │  │  v6 API │  │  AMM    │  │   Direct     │ │   │
│  │  └─────────┘  └─────────┘  └──────────────┘ │   │
│  └──────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────┘
                         │
                  Solana Network
```

## What it monitors

| Signal | Source | Description |
|--------|--------|-------------|
| Bonding progress | Helius WS | Tokens approaching graduation threshold |
| Graduation | Helius WS | Token migrated to PumpSwap/Raydium |
| Post-grad prices | DexScreener | Real-time price/volume after graduation |
| Dev wallet sells | Helius API | Creator dumping post-graduation |

## Built-in strategies

| Strategy | Description |
|----------|-------------|
| `GraduationSniper` | Auto-trade graduated tokens with TP/SL/trailing/dev-sell exit |
| `MomentumStrategy` | Generic momentum on any Solana token |

## Adapters

| Adapter | Status | Description |
|---------|--------|-------------|
| Graduation Monitor | ✅ Working | Pump.fun graduation detection + dev tracking |
| Jupiter | ✅ Working | Swap execution via Jupiter v6 |
| Helius | ✅ Working | Real-time data feed |
| Raydium | 📋 Planned | Direct AMM interaction |
| PumpSwap | 📋 Planned | Direct PumpSwap pool access |
| Birdeye | 📋 Planned | Historical OHLCV for backtesting |

## How is this different from [Nautilus Trader](https://github.com/nautechsystems/nautilus_trader)?

Nautilus is a general-purpose algo trading platform (200K+ lines, Rust/Cython) built for orderbook exchanges — Binance, Bybit, Interactive Brokers. It's excellent for that.

Fathom is purpose-built for Solana DEX memecoins:
- **Graduation-aware** — understands the pump.fun → DEX lifecycle
- **AMM-native** — built around swap execution, not orderbooks
- **Dev wallet tracking** — monitors creator behavior as a trading signal
- **Lightweight** — ~2K lines of pure Python, read the whole thing in 30 minutes
- **Memecoin-specific** — strategies designed for the 5-minute lifecycle of a graduated token

## Requirements

- Python 3.12+
- Helius API key (free tier works for polling, paid for WebSocket)
- Solana CLI (optional, for wallet management)

## License

MIT

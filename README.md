# fathom

High-performance Solana DEX trading engine. Event-driven backtesting and live execution.

```
pip install fathom-trading
```

## What is this

Fathom is an event-driven trading engine for Solana DEXes. It connects to Jupiter, Raydium, and Helius to provide:

- **Live execution** — route swaps through Jupiter aggregator with configurable slippage, priority fees, and retry logic
- **Real-time data** — stream token prices, trades, and liquidity via Helius WebSocket and DexScreener
- **Backtesting** — replay historical swap data with nanosecond event resolution
- **Strategy framework** — write strategies once, run them in backtest and live with zero code changes

## Quick start

```python
from fathom import Engine, JupiterAdapter, HeliusDataFeed
from fathom.strategies import MomentumStrategy

engine = Engine()

# Connect to Solana
engine.add_adapter(JupiterAdapter(
    rpc_url="https://mainnet.helius-rpc.com/?api-key=YOUR_KEY",
    wallet_path="~/.config/solana/id.json",
))

engine.add_data_feed(HeliusDataFeed(
    api_key="YOUR_HELIUS_KEY",
    tokens=["SOL", "USDC", "JUP", "RAY"],
))

# Run strategy
engine.add_strategy(MomentumStrategy(
    lookback_window=60,    # seconds
    entry_threshold=0.02,  # 2% move
    position_size=0.1,     # 10% of portfolio per trade
))

engine.run()
```

## Architecture

```
┌─────────────────────────────────────────────────┐
│                   Engine                         │
│  ┌──────────┐  ┌──────────┐  ┌───────────────┐ │
│  │ EventBus │──│ Strategy │──│ Risk Manager  │ │
│  └────┬─────┘  └──────────┘  └───────────────┘ │
│       │                                          │
│  ┌────┴──────────────────────────────────────┐  │
│  │            Adapter Layer                   │  │
│  │  ┌─────────┐ ┌────────┐ ┌──────────────┐ │  │
│  │  │ Jupiter │ │Raydium │ │    Helius    │ │  │
│  │  │  Swap   │ │  AMM   │ │  WebSocket   │ │  │
│  │  └────┬────┘ └───┬────┘ └──────┬───────┘ │  │
│  └───────┼──────────┼─────────────┼──────────┘  │
└──────────┼──────────┼─────────────┼──────────────┘
           │          │             │
     ┌─────┴──────────┴─────────────┴─────┐
     │          Solana Network             │
     └─────────────────────────────────────┘
```

## Adapters

| Adapter | Status | Description |
|---------|--------|-------------|
| Jupiter | 🟡 In progress | Swap execution via Jupiter v6 API |
| Helius | 🟡 In progress | Real-time price feeds, tx monitoring |
| Raydium | 📋 Planned | Direct AMM pool interaction |
| DexScreener | 📋 Planned | Historical price/volume data |
| Birdeye | 📋 Planned | Token analytics and OHLCV |

## Project structure

```
fathom/
├── src/
│   ├── core/          # Engine, event bus, order management
│   ├── engine/        # Backtest and live execution engines  
│   ├── adapters/      # Exchange/data provider integrations
│   │   ├── jupiter/   # Jupiter aggregator adapter
│   │   ├── helius/    # Helius RPC + WebSocket adapter
│   │   └── raydium/   # Raydium AMM adapter
│   ├── data/          # Data models, serialization, storage
│   └── strategies/    # Strategy base class + examples
├── tests/
├── examples/
├── docs/
└── scripts/
```

## Requirements

- Python 3.12+
- Solana CLI (optional, for wallet management)
- Helius API key (free tier works)

## License

MIT

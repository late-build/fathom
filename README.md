# ⚓ fathom

High-performance Solana DEX trading engine. Built for memecoin strategies.

Detects pump.fun graduations, monitors dev wallets, executes via Jupiter/PumpSwap, and manages positions with configurable TP/SL/trailing stops. Same strategy code runs in backtest and live.

## Quick start

```bash
git clone https://github.com/late-build/fathom.git
cd fathom
cp fathom.toml.example fathom.toml
# edit fathom.toml with your RPC/Helius keys

# paper trade (no wallet needed)
python -m fathom run --mode paper

# just watch graduations
python -m fathom monitor

# backtest on historical data
python -m fathom backtest --data graduations.json

# get a Jupiter quote
python -m fathom quote SOL 100

# check config
python -m fathom status
```

## Architecture

```
GraduationMonitor ─┐
HeliusDataFeed ────┤──→ EventBus ──→ Strategy ──→ Jupiter/PumpSwap
DexScreener poll ──┘                    │
                                        └──→ PaperAdapter (backtest/paper)
```

**Core loop**: Data feeds emit events → strategies evaluate and submit orders → adapters execute (or simulate).

One strategy class, three modes:
- **`live`** — real transactions, real money
- **`paper`** — real data, simulated fills
- **`backtest`** — historical data replay

## Strategies

### GraduationSniper

Trades tokens graduating from pump.fun to PumpSwap/Raydium.

Filters: holder count, SOL raised, market cap, time since graduation.
Position management: take-profit, stop-loss, trailing stop, timeout, dev-sell exit.

### Momentum

Simple momentum strategy with lookback window and entry threshold.

### Writing your own

```python
from fathom import Strategy
from fathom.core.events import PriceUpdate

class MyStrategy(Strategy):
    name = "my_strategy"

    def on_price_update(self, event: PriceUpdate):
        if event.price_usd < some_threshold:
            self.buy(event.token, amount_usd=50)
```

## Data sources

| Source | What | Method |
|--------|------|--------|
| Helius WebSocket | Transaction streaming, account changes | Real-time |
| pump.fun programs | Bonding curve activity, graduations | Helius log subscription |
| DexScreener API | Token prices, volume, liquidity | Polling fallback |
| Dev wallets | Creator sell detection | Helius tx history |

## Config

All settings in `fathom.toml`. Env vars override with `FATHOM_` prefix:

```bash
FATHOM_RPC_URL=https://... python -m fathom run
```

See `fathom.toml.example` for all options.

## Requirements

- Python 3.9+
- `aiohttp` (HTTP/WebSocket)
- `tomli` (config parsing, Python < 3.11)

## Acknowledgments

Fathom's event-driven architecture is inspired by [nautilus_trader](https://github.com/nautechsystems/nautilus_trader) — a production-grade algorithmic trading platform by NauTech Systems. Nautilus covers traditional markets and CEXs with 200K+ lines of Rust/Python. Fathom takes the core design principles (typed events, engine/adapter/strategy separation, backtest-live parity) and rebuilds them from scratch for Solana DEX trading in ~2K lines of Python.

## Status

Alpha. Core engine works. Paper trading works. Backtest works. Live execution needs wallet signing integration (`solders`).

## License

MIT

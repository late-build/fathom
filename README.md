# ⚓ fathom

[![Python](https://img.shields.io/badge/python-3.9+-3776ab?logo=python&logoColor=white)](https://python.org)
[![License](https://img.shields.io/badge/license-MIT-green)](LICENSE)
[![Solana](https://img.shields.io/badge/chain-Solana-9945ff?logo=solana&logoColor=white)](https://solana.com)

**Open-source Solana memecoin trading engine. Multi-source discovery, on-chain intelligence, crowdsourced strategy research.**

| Platform | Python | Status |
|:---------|:-------|:-------|
| Linux (x86_64) | 3.9+ | ✓ |
| macOS (ARM64) | 3.9+ | ✓ |

- **Website**: [fathom-site.vercel.app](https://fathom-site.vercel.app)
- **Source**: [github.com/late-build/fathom](https://github.com/late-build/fathom)

## Introduction

Fathom is an event-driven trading engine for Solana memecoins. 7 data sources feed a multi-factor scoring model that filters noise from signal — holder forensics, liquidity validation, momentum analysis, and scam detection. The infrastructure is open. The strategies are crowdsourced.

Thousands of tokens launch daily. Most are noise. The alpha isn't in finding tokens — it's in knowing which ones to ignore. Fathom treats filtering as the primary intelligence problem: discover → enrich → verify → decide → execute, with every stage instrumented and every decision auditable.

**The same strategy code runs in backtest, paper, and live with zero changes.** No rewriting logic for production. No separate research environment. The strategy that backtests against historical data is the same class instance that executes against real-time feeds.

> *fathom — to measure the depth of water; to understand after much thought.*
>
> *Originally a nautical term: the length of outstretched arms, used to sound the ocean floor.*
> *We use it to sound the depth of a market — measuring what's beneath the surface*
> *before committing capital.*

## Why Fathom?

- **Memecoin-native**: Not a generic DEX bot. Every component — data feeds, strategies, filters — is built for Solana's memecoin lifecycle, from pump.fun bonding curves through PumpSwap/Raydium migration.
- **Verifiable by default**: 7-source discovery pipeline. On-chain holder analysis via standard Solana RPC. Every trade decision maps to a filter that maps to a line of code.
- **Backtest-live parity**: One strategy class, three execution modes. Paper trading uses real data with simulated fills. Backtest replays historical graduations. Live sends transactions. Same logic throughout.
- **Scam-resilient**: Multi-layer filtering catches inflated mcaps, insider-concentrated supply, and serial deployer wallets before your capital is exposed.
- **Lightweight**: ~3,000 lines of Python. No Rust compilation. No Docker required. No database. Clone, configure, run.

## Features

- **7-source discovery pipeline**: DexScreener (profiles, boosts, search), pump.fun (graduated, top-runners), GeckoTerminal (trending, PumpSwap volume, transaction count) — 143+ mints per scan.
- **On-chain holder intelligence**: `getTokenLargestAccounts` + `getTokenSupply` + `getAccountInfo` — top 10 concentration, deployer wallet, supply distribution. Works with any Solana RPC.
- **Configurable strategies**: Take-profit, stop-loss, trailing stops, hold timeouts, dev-sell exit triggers, mcap filters, holder concentration filters. All parameters in TOML config.
- **Paper trading**: Real price feeds, simulated execution. Full P&L tracking, position management, balance accounting.
- **Historical backtesting**: Replay collected graduation data through any strategy. Win rate, P&L, drawdown metrics.
- **Adaptive rate limiting**: DexScreener batch API with exponential backoff. GeckoTerminal 30 req/min compliance. Pump.fun origin header handling.
- **CLI-first**: `python -m fathom collect|backtest|run|monitor|quote|status` — no web UI, no setup wizards.

## Web App

The companion site at [fathom-site.vercel.app](https://fathom-site.vercel.app) is a 7-page interactive app — not a brochure:

- **Token Analyzer** — paste any mint, get a full scoring breakdown with sparkline chart
- **Strategy Sandbox** — backtest single tokens or the full dataset with live slider controls
- **Leaderboard** — wallet-verified strategy submissions, ranked by performance, forkable
- **Live Feed** — real-time tokens scored by the multi-factor model
- **Tokenomics** — $FATHOM supply model, treasury, staking roadmap

Source: [late-build/fathom-site](https://github.com/late-build/fathom-site)

## Architecture

```
┌─────────────────────┐
│   7 Data Sources     │  DexScreener · Pump.fun · GeckoTerminal
│   Discovery Layer    │  143+ mints per scan cycle
└──────────┬──────────┘
           │
           ▼
┌─────────────────────┐
│   DexScreener       │  Batch enrichment: price, volume, liquidity, mcap
│   Batch API         │  30 tokens/call, adaptive backoff on 429
└──────────┬──────────┘
           │
           ▼
┌─────────────────────┐
│   Solana RPC        │  getTokenLargestAccounts + getTokenSupply
│   Holder Intel      │  Top 10 concentration %, deployer, supply
└──────────┬──────────┘
           │
           ▼
┌─────────────────────┐
│   EventBus          │  Typed events with nanosecond timestamps
│                     │  GraduationEvent → PriceUpdate → DevActivity
└──────────┬──────────┘
           │
           ▼
┌─────────────────────┐
│   Strategy Engine    │  GraduationSniper: mcap + concentration + trailing
│                     │  Momentum: lookback window + entry threshold
└──────────┬──────────┘
           │
      ┌────┴────┐
      ▼         ▼
┌─────────┐ ┌─────────┐
│  Paper  │ │  Live   │  Jupiter v6 / PumpSwap direct
│  Trade  │ │  Trade  │  (signing via solders — WIP)
└─────────┘ └─────────┘
```

**Core loop**: Data sources emit typed events → EventBus routes to subscribers → strategies evaluate and submit orders → adapters execute or simulate.

## Quick Start

```bash
git clone https://github.com/late-build/fathom.git
cd fathom
pip install aiohttp
cp fathom.toml.example fathom.toml
# add your RPC URL (free: solana-rpc.publicnode.com)
```

**Collect real graduation data:**
```bash
python -m fathom collect --hours 24
# → Scans 7 sources, enriches via DexScreener + Solana RPC
# → Outputs backtest-ready JSON with holder analysis
```

**Backtest a strategy:**
```bash
python -m fathom backtest --data data/collect-latest.json
# → Replays graduations through GraduationSniper
# → Reports win rate, P&L, drawdown
```

**Paper trade (real data, simulated fills):**
```bash
python -m fathom run --mode paper
```

**Monitor graduations without trading:**
```bash
python -m fathom monitor
```

**Get a Jupiter swap quote:**
```bash
python -m fathom quote SOL 100
```

## Strategies

### GraduationSniper

The primary strategy. Trades tokens at the moment of pump.fun graduation.

**Filters** (all configurable):
- Market cap range ($0 – $500K default)
- Top 10 holder concentration (< 80%)
- SOL raised on bonding curve
- Time since graduation (entry delay window)

**Position management**:
- Take-profit (default 50%)
- Stop-loss (default 20%)
- Trailing stop (15% after 30% gain)
- Hold timeout (force exit after 10 minutes)
- Dev-sell exit (auto-close if deployer dumps)

### Momentum

Simple momentum strategy with configurable lookback window and entry threshold. Useful as a template for custom strategies.

### Writing Your Own

```python
from fathom.core.strategy import Strategy
from fathom.core.events import PriceUpdate

class MyStrategy(Strategy):
    name = "my_strategy"

    def on_price_update(self, event: PriceUpdate):
        if event.price_usd < self.config.get("entry_price", 0.001):
            self.buy(event.token, amount_usd=50)
```

Strategies receive typed events, call `self.buy()` / `self.sell()`, and work identically across backtest, paper, and live modes.

## Data Sources

| Source | Endpoints | What | Rate Limit |
|--------|-----------|------|------------|
| DexScreener | 3 | Token profiles, boosts, search + batch enrichment | ~400ms adaptive |
| Pump.fun | 2 | Graduated tokens, top runners (pre-grad momentum) | 8s timeout, origin header |
| GeckoTerminal | 3 | Trending pools, PumpSwap volume, top transactions | Hard 30 req/min |
| Solana RPC | 3/token | Holder distribution, supply, deployer wallet | Endpoint-dependent |
| Helius | WS | Real-time transaction streaming, account changes | Key-dependent |

All discovery sources are free. No API keys required for basic collection (public RPC endpoints used as fallback).

## Config

All settings live in `fathom.toml`. Environment variables override with `FATHOM_` prefix:

```bash
FATHOM_RPC_URL=https://your-rpc.com python -m fathom run
```

See [`fathom.toml.example`](fathom.toml.example) for the complete configuration reference.

## Requirements

- Python 3.9+
- `aiohttp` — async HTTP and WebSocket
- `tomli` — TOML config parsing (stdlib in Python 3.11+)

No database. No Docker. No compilation step.

## Acknowledgments

Fathom's event-driven architecture draws from [NautilusTrader](https://github.com/nautechsystems/nautilus_trader) by NauTech Systems — a production-grade algorithmic trading platform spanning 200K+ lines of Rust and Python across traditional markets and centralized exchanges.

From Nautilus we took the design philosophy: typed event system, engine/adapter/strategy separation, and the principle that backtest and live execution must share identical strategy code. Everything else — the Solana adapters, graduation detection, holder analysis, the entire data pipeline — is built from scratch for a fundamentally different market.

Where Nautilus is a battleship (multi-asset, multi-venue, institutional-grade), Fathom is a speargun — single-chain, single-lifecycle, purpose-built to go deep on one thing.

## Status

**Alpha.** Core engine, paper trading, backtesting, and data collection are functional. Live transaction signing is the remaining integration (`solders`).

The intelligence layer — which graduations are worth trading and why — is where the real value lives. The swap execution is commodity infrastructure anyone can build. The filters are the edge.

## License

MIT

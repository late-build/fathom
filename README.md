# fathom

An event-driven trading engine for Solana DEXes, focused on the pump.fun graduation lifecycle.

Fathom monitors tokens as they graduate from pump.fun bonding curves to PumpSwap and Raydium pools, provides real-time and historical data feeds, and executes strategies with the same code in backtest and live environments.

| Python | Platform | License |
|:-------|:---------|:--------|
| 3.12+ | Linux, macOS | MIT |

## The problem

Memecoin trading on Solana is fast. A token graduates from pump.fun, gets a PumpSwap pool, pumps, and dumps — often within minutes. Traders who catch graduations early and exit before the dump make money. Everyone else doesn't.

The tools available today are either closed-source sniper bots (trust someone else's code with your wallet), Telegram bots (no backtesting, no customization), or general-purpose frameworks like NautilusTrader that don't understand the pump.fun lifecycle at all.

There's no open-source framework that lets you write a memecoin trading strategy, backtest it against historical graduations, and deploy it live with the same code.

Fathom is that framework.

## How it works

Fathom's architecture is event-driven. Market data, graduations, and order updates all flow through a central event bus as typed, timestamped events. Strategies subscribe to the events they care about and emit orders in response.

```
Data Sources                    Engine                     Execution
─────────────                   ──────                     ─────────
Helius WebSocket ──┐
                   ├─► EventBus ──► Strategy ──► PumpSwap Direct
Graduation Monitor ┤                             (Jito bundles)
                   ├─► Risk ◄──────────────────► Jupiter Fallback
DexScreener Poll ──┘   Manager
```

The graduation monitor is the core data source. It watches pump.fun's program via Helius WebSocket, detects when a token's bonding curve completes, identifies the new PumpSwap or Raydium pool, and emits a `GraduationEvent` with the token mint, pool address, holder count, SOL raised, and creator wallet.

Strategies receive this event and decide whether to trade. The built-in `GraduationSniper` strategy filters by holder count, SOL raised, and market cap, then manages positions with configurable take-profit, stop-loss, trailing stops, and automatic exit on dev wallet sells.

Execution goes through PumpSwap directly when possible — reading pool reserves from chain, calculating output with the constant-product formula, and submitting via Jito bundles for MEV protection. Jupiter is available as a fallback for tokens with multi-pool liquidity.

## Backtesting

The same strategy code runs against historical data. Load past graduations with their price histories, and Fathom replays them through the event bus in chronological order. Your strategy processes them identically to how it would in production.

```python
engine = Engine(mode="backtest")

monitor = GraduationMonitor(helius_api_key="")
monitor.load_historical_graduations(graduation_data)

engine.add_data_feed(monitor)
engine.add_strategy(GraduationSniper(
    position_size_usd=50,
    min_holders=100,
    take_profit_pct=0.50,
    stop_loss_pct=0.20,
    max_hold_seconds=300,
))

engine.run()
```

This lets you answer questions like: "If I sniped every graduation with 100+ holders and exited at 50% gain or 20% loss, what would my PnL have been last month?"

## Strategies

Strategies extend a base class and implement `on_price_update()`. The engine handles lifecycle, event routing, and position tracking.

```python
from fathom.core.strategy import Strategy
from fathom.core.events import PriceUpdate
from fathom.adapters.pumpfun.graduation import GraduationEvent

class MyStrategy(Strategy):
    name = "my_strategy"

    def bind(self, event_bus):
        super().bind(event_bus)
        event_bus.subscribe(EventType.SIGNAL, self.on_graduation)

    def on_graduation(self, event):
        if isinstance(event, GraduationEvent):
            if event.holder_count > 200 and event.sol_raised > 60:
                self.buy(event.mint, amount_usd=100, slippage_bps=300)

    def on_price_update(self, event: PriceUpdate):
        # manage positions, check exits
        pass
```

### Built-in strategies

**GraduationSniper** — Filters and trades newly graduated tokens. Configurable entry filters (min holders, min SOL raised, mcap range, max age), position management (TP/SL/trailing stop/timeout), and automatic exit on dev sell detection.

**MomentumStrategy** — Generic momentum strategy for any Solana token. Tracks price history over a configurable lookback window and enters on threshold moves.

## Adapters

Adapters handle communication with external services. Data feeds produce events; execution adapters consume orders.

| Adapter | Type | Description |
|:--------|:-----|:------------|
| `GraduationMonitor` | Data | Pump.fun graduation detection, bonding curve tracking, dev wallet monitoring via Helius WebSocket |
| `HeliusDataFeed` | Data | Real-time token price streaming with DexScreener polling fallback |
| `PumpSwapAdapter` | Execution | Direct pool swaps on PumpSwap AMM with Jito bundle support |
| `JupiterAdapter` | Execution | Swap routing via Jupiter v6 aggregator |
| Raydium | Execution | Planned — direct Raydium AMM interaction |
| Birdeye | Data | Planned — historical OHLCV data for extended backtesting |

## Dev wallet tracking

The graduation monitor optionally tracks creator wallets after graduation. If the dev sells, a `DevActivityEvent` is emitted. The `GraduationSniper` strategy can auto-exit positions when this happens.

This is a first-class feature, not an afterthought. In memecoin trading, what the dev does post-graduation is one of the strongest signals available.

## Project structure

```
fathom/
├── src/
│   ├── core/
│   │   ├── engine.py        # Async engine, lifecycle management
│   │   ├── events.py        # Event types, EventBus
│   │   └── strategy.py      # Strategy base class
│   ├── adapters/
│   │   ├── pumpfun/
│   │   │   └── graduation.py  # Graduation monitor + dev tracking
│   │   ├── pumpswap/
│   │   │   └── adapter.py     # Direct PumpSwap execution
│   │   ├── jupiter/
│   │   │   └── adapter.py     # Jupiter v6 aggregator
│   │   ├── helius/
│   │   │   └── feed.py        # Helius WebSocket data feed
│   │   └── base.py            # Adapter/feed interfaces
│   └── strategies/
│       ├── graduation_sniper.py
│       └── momentum.py
├── tests/
├── examples/
└── docs/
```

## Requirements

- Python 3.12+
- [Helius](https://helius.dev) API key — free tier works for polling; WebSocket requires a paid plan
- Solana wallet keypair (for live execution)
- `aiohttp` and `websockets` (installed automatically)

## Status

Fathom is alpha software. The event system, graduation monitor, strategy framework, and PumpSwap adapter are functional. Transaction signing requires integration with `solders` or `solana-py` — the swap instruction building is implemented but signing is stubbed.

Contributions welcome. If you're building memecoin trading tools on Solana, this is the foundation.

## License

MIT — see [LICENSE](LICENSE).

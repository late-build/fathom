"""
Fathom CLI — run the trading engine from the command line.

Usage:
    python -m fathom run [--config fathom.toml] [--mode live|paper]
    python -m fathom backtest --data graduations.json [--config fathom.toml]
    python -m fathom monitor [--config fathom.toml]
    python -m fathom quote <token> <amount_usd>
    python -m fathom status
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import time
from pathlib import Path

from fathom.config import FathomConfig, load_config


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="fathom",
        description="Solana DEX trading engine",
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="Debug logging"
    )
    parser.add_argument(
        "-c", "--config", default="fathom.toml", help="Config file path"
    )

    sub = parser.add_subparsers(dest="command")

    # -- run --
    run_p = sub.add_parser("run", help="Start the trading engine")
    run_p.add_argument(
        "--mode", choices=["live", "paper"], default="paper",
        help="Execution mode (default: paper)",
    )

    # -- monitor --
    sub.add_parser("monitor", help="Monitor graduations without trading")

    # -- backtest --
    bt_p = sub.add_parser("backtest", help="Backtest a strategy on historical data")
    bt_p.add_argument("--data", required=True, help="Path to graduation data JSON")
    bt_p.add_argument(
        "--strategy", default="graduation_sniper",
        help="Strategy to backtest",
    )

    # -- collect --
    c_p = sub.add_parser("collect", help="Collect historical graduation data")
    c_p.add_argument("--hours", type=float, default=24, help="Hours to look back")
    c_p.add_argument("--output", "-o", default="graduations.json", help="Output file")
    c_p.add_argument("--helius-key", default="", help="Helius API key (optional)")
    c_p.add_argument("--min-liquidity", type=float, default=1000, help="Min liquidity USD")

    # -- quote --
    q_p = sub.add_parser("quote", help="Get a Jupiter swap quote")
    q_p.add_argument("token", help="Token symbol or mint address")
    q_p.add_argument("amount", type=float, help="Amount in USD")

    # -- status --
    sub.add_parser("status", help="Show engine status / verify config")

    args = parser.parse_args()

    # Logging
    level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )

    if not args.command:
        parser.print_help()
        sys.exit(1)

    # Load config
    config_path = Path(args.config)
    if config_path.exists():
        config = load_config(config_path)
    else:
        if args.command not in ("status",):
            print(f"Config not found: {config_path}")
            print("Create fathom.toml or run: python -m fathom status")
            sys.exit(1)
        config = FathomConfig()

    # Dispatch
    if args.command == "collect":
        cmd_collect(args)
        return
    elif args.command == "run":
        cmd_run(config, mode=args.mode)
    elif args.command == "monitor":
        cmd_monitor(config)
    elif args.command == "backtest":
        cmd_backtest(config, data_path=args.data, strategy=args.strategy)
    elif args.command == "quote":
        asyncio.run(cmd_quote(config, token=args.token, amount=args.amount))
    elif args.command == "status":
        cmd_status(config, config_path)


def cmd_collect(args) -> None:
    """Collect historical graduation data."""
    from fathom.collect import GraduationCollector, GraduationRecord
    from dataclasses import asdict

    collector = GraduationCollector(
        helius_api_key=args.helius_key,
        max_age_hours=args.hours,
        min_liquidity_usd=args.min_liquidity,
    )

    records = asyncio.run(collector.collect())

    output = Path(args.output)
    data = [asdict(r) for r in records]
    with open(output, "w") as f:
        json.dump(data, f, indent=2)

    print(f"\n💾 Saved {len(records)} records to {output}")

    if records:
        print(f"\n{'Symbol':>12} {'Price':>14} {'MCap':>12} {'Liq':>10} {'MaxGain':>10} {'MaxLoss':>10}")
        print("-" * 74)
        for r in sorted(records, key=lambda x: x.max_gain_pct, reverse=True)[:20]:
            print(
                f"{r.symbol:>12} "
                f"${r.initial_price_usd:>12.8f} "
                f"${r.market_cap_at_grad:>10,.0f} "
                f"${r.liquidity_usd:>8,.0f} "
                f"{r.max_gain_pct:>+9.1%} "
                f"{r.max_loss_pct:>+9.1%}"
            )


def cmd_run(config: FathomConfig, mode: str) -> None:
    """Start the full trading engine."""
    from fathom.core.engine import Engine
    from fathom.adapters.helius.feed import HeliusDataFeed
    from fathom.adapters.pumpfun.graduation import GraduationMonitor
    from fathom.adapters.jupiter.adapter import JupiterAdapter
    from fathom.adapters.pumpswap.adapter import PumpSwapAdapter

    engine = Engine(mode=mode)

    # Data feeds
    if config.helius_api_key:
        engine.add_data_feed(HeliusDataFeed(
            api_key=config.helius_api_key,
            tokens=config.watch_tokens,
            poll_interval_ms=config.poll_interval_ms,
        ))
        engine.add_data_feed(GraduationMonitor(
            helius_api_key=config.helius_api_key,
            min_bonding_progress=config.grad_min_progress,
            min_holders=config.grad_min_holders,
            track_dev_wallets=config.track_dev_wallets,
            poll_interval_ms=config.poll_interval_ms,
        ))
    else:
        print("⚠ No helius_api_key — running with DexScreener polling only")
        engine.add_data_feed(GraduationMonitor(
            helius_api_key="",
            min_holders=config.grad_min_holders,
            track_dev_wallets=False,
            poll_interval_ms=config.poll_interval_ms,
        ))

    # Execution adapters
    if mode == "paper":
        from fathom.adapters.paper import PaperAdapter
        adapter = PaperAdapter(initial_balance_usd=config.paper_balance_usd)
        engine.add_adapter(adapter)
        print(f"📝 Paper trading | balance: ${config.paper_balance_usd:,.0f}")
    else:
        if config.wallet_path and Path(config.wallet_path).exists():
            # Use PumpSwap for graduated tokens, Jupiter as fallback
            engine.add_adapter(PumpSwapAdapter(
                rpc_url=config.rpc_url,
                wallet_path=config.wallet_path,
                slippage_bps=config.slippage_bps,
                use_jito=config.use_jito,
                jito_tip_lamports=config.jito_tip_lamports,
            ))
            engine.add_adapter(JupiterAdapter(
                rpc_url=config.rpc_url,
                wallet_path=config.wallet_path,
                slippage_bps=config.slippage_bps,
            ))
            print(f"🔴 LIVE trading | wallet loaded")
        else:
            print("❌ Live mode requires wallet_path in config")
            sys.exit(1)

    # Strategy
    strat = _build_strategy(config)
    engine.add_strategy(strat)

    print(f"🚀 Fathom engine starting ({mode} mode)")
    print(f"   Strategy: {strat.name}")
    print(f"   Position size: ${config.position_size_usd}")
    print(f"   Max positions: {config.max_positions}")
    print(f"   TP: {config.take_profit_pct:.0%} | SL: {config.stop_loss_pct:.0%}")
    print()

    engine.run()


def cmd_monitor(config: FathomConfig) -> None:
    """Monitor graduations without executing trades."""
    from fathom.core.engine import Engine
    from fathom.adapters.pumpfun.graduation import GraduationMonitor

    engine = Engine(mode="paper")

    engine.add_data_feed(GraduationMonitor(
        helius_api_key=config.helius_api_key or "",
        min_bonding_progress=0,
        min_holders=0,
        track_dev_wallets=config.track_dev_wallets,
        poll_interval_ms=config.poll_interval_ms,
    ))

    # Attach a logging-only strategy
    from fathom.strategies.log_only import LogOnlyStrategy
    engine.add_strategy(LogOnlyStrategy())

    print("👁 Monitoring graduations (no trading)")
    print()
    engine.run()


def cmd_backtest(config: FathomConfig, data_path: str, strategy: str) -> None:
    """Run a backtest on historical graduation data."""
    from fathom.core.engine import Engine
    from fathom.core.events import EventBus
    from fathom.adapters.pumpfun.graduation import GraduationMonitor
    from fathom.adapters.paper import PaperAdapter
    from fathom.backtest import BacktestRunner

    data_file = Path(data_path)
    if not data_file.exists():
        print(f"❌ Data file not found: {data_path}")
        sys.exit(1)

    with open(data_file) as f:
        data = json.load(f)

    strat = _build_strategy(config)
    adapter = PaperAdapter(initial_balance_usd=config.paper_balance_usd)

    runner = BacktestRunner(
        strategy=strat,
        adapter=adapter,
        data=data,
    )

    print(f"📊 Backtesting {strat.name} on {len(data)} graduations")
    print(f"   Paper balance: ${config.paper_balance_usd:,.0f}")
    print()

    results = runner.run()
    runner.print_report(results)


async def cmd_quote(config: FathomConfig, token: str, amount: float) -> None:
    """Get a Jupiter swap quote."""
    from fathom.adapters.jupiter.adapter import JupiterAdapter, KNOWN_MINTS

    adapter = JupiterAdapter(
        rpc_url=config.rpc_url,
        slippage_bps=config.slippage_bps,
    )
    await adapter.connect()

    input_mint = KNOWN_MINTS.get("USDC", "")
    output_mint = KNOWN_MINTS.get(token.upper(), token)

    amount_raw = int(amount * 1_000_000)  # USDC has 6 decimals

    try:
        quote = await adapter.get_quote(input_mint, output_mint, amount_raw)
        out_amount = int(quote.get("outAmount", 0))
        price_impact = float(quote.get("priceImpactPct", 0))
        routes = len(quote.get("routePlan", []))

        print(f"💱 Quote: ${amount:.2f} USDC → {token.upper()}")
        print(f"   Output: {out_amount:,} raw units")
        print(f"   Price impact: {price_impact:.4f}%")
        print(f"   Routes: {routes}")
    except Exception as e:
        print(f"❌ Quote failed: {e}")
    finally:
        await adapter.disconnect()


def cmd_status(config: FathomConfig, config_path: Path) -> None:
    """Show status and verify config."""
    from fathom import __version__

    print(f"⚓ Fathom v{__version__}")
    print()

    if config_path.exists():
        print(f"✅ Config: {config_path}")
    else:
        print(f"⚠ Config not found: {config_path}")
        print(f"   Create one with: python -m fathom init")

    print(f"   RPC: {config.rpc_url[:40]}..." if config.rpc_url else "   RPC: not set")
    print(f"   Helius: {'✅ set' if config.helius_api_key else '❌ not set'}")
    print(f"   Wallet: {'✅ ' + config.wallet_path if config.wallet_path else '❌ not set'}")
    print(f"   Jito: {'ON' if config.use_jito else 'OFF'}")
    print()
    print(f"   Strategy: graduation_sniper")
    print(f"   Position: ${config.position_size_usd}")
    print(f"   Max positions: {config.max_positions}")
    print(f"   TP/SL: {config.take_profit_pct:.0%} / {config.stop_loss_pct:.0%}")


def _build_strategy(config: FathomConfig):
    """Build strategy from config."""
    from fathom.strategies.graduation_sniper import GraduationSniper
    return GraduationSniper(
        position_size_usd=config.position_size_usd,
        max_positions=config.max_positions,
        min_holders=config.grad_min_holders,
        min_sol_raised=config.grad_min_sol,
        take_profit_pct=config.take_profit_pct,
        stop_loss_pct=config.stop_loss_pct,
        trailing_stop_pct=config.trailing_stop_pct,
        trailing_activate_pct=config.trailing_activate_pct,
        max_hold_seconds=config.max_hold_seconds,
        exit_on_dev_sell=config.exit_on_dev_sell,
        max_initial_mcap=config.max_initial_mcap,
    )

"""
Configuration loader for Fathom.

Reads from fathom.toml and environment variables.
Env vars override file values (prefixed FATHOM_).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class FathomConfig:
    # -- Connection --
    rpc_url: str = "https://api.mainnet-beta.solana.com"
    helius_api_key: str = ""
    wallet_path: str = ""

    # -- Execution --
    slippage_bps: int = 300
    use_jito: bool = True
    jito_tip_lamports: int = 100_000
    priority_fee_lamports: int = 50_000

    # -- Graduation monitor --
    grad_min_progress: float = 70.0
    grad_min_holders: int = 100
    grad_min_sol: float = 50.0
    track_dev_wallets: bool = True
    poll_interval_ms: int = 2000

    # -- Strategy --
    position_size_usd: float = 50.0
    max_positions: int = 3
    take_profit_pct: float = 0.50
    stop_loss_pct: float = 0.20
    trailing_stop_pct: float = 0.15
    trailing_activate_pct: float = 0.30
    max_hold_seconds: float = 300.0
    exit_on_dev_sell: bool = True
    max_initial_mcap: float = 500_000.0

    # -- Paper trading --
    paper_balance_usd: float = 1000.0

    # -- Data feeds --
    watch_tokens: list[str] = field(default_factory=list)


def load_config(path: Path) -> FathomConfig:
    """Load config from TOML file, with env var overrides."""
    try:
        import tomllib
    except ModuleNotFoundError:
        try:
            import tomli as tomllib  # type: ignore[no-redef]
        except ModuleNotFoundError:
            raise RuntimeError(
                "Python 3.11+ required for tomllib, or install tomli: pip install tomli"
            )

    with open(path, "rb") as f:
        raw = tomllib.load(f)

    config = FathomConfig()

    # Flatten nested sections
    flat: dict = {}
    for key, val in raw.items():
        if isinstance(val, dict):
            for k2, v2 in val.items():
                flat[k2] = v2
        else:
            flat[key] = val

    # Apply file values
    for key, val in flat.items():
        if hasattr(config, key):
            setattr(config, key, val)

    # Env overrides (FATHOM_RPC_URL, FATHOM_HELIUS_API_KEY, etc.)
    for attr in config.__dataclass_fields__:
        env_key = f"FATHOM_{attr.upper()}"
        env_val = os.environ.get(env_key)
        if env_val is not None:
            field_type = type(getattr(config, attr))
            if field_type == bool:
                setattr(config, attr, env_val.lower() in ("1", "true", "yes"))
            elif field_type == int:
                setattr(config, attr, int(env_val))
            elif field_type == float:
                setattr(config, attr, float(env_val))
            else:
                setattr(config, attr, env_val)

    return config

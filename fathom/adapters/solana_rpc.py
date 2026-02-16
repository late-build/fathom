"""
Solana RPC helpers for token analytics.

Ported from NARRA's token-intel endpoint. Uses standard Solana RPC methods
(no Helius-specific APIs) so it works with any RPC provider.

Three parallel calls per token:
1. getTokenLargestAccounts — top 20 holders with balances
2. getTokenSupply — total supply for percentage calculation
3. getAccountInfo — mint authority (deployer)
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any

import aiohttp

logger = logging.getLogger("fathom.rpc")

# Ordered fallback RPC endpoints
DEFAULT_RPCS = [
    "https://solana-rpc.publicnode.com",
    "https://api.mainnet-beta.solana.com",
]


@dataclass
class HolderInfo:
    address: str = ""
    balance: float = 0.0
    percentage: float = 0.0


@dataclass
class TokenIntel:
    """On-chain token intelligence — holders, supply, deployer."""
    mint: str = ""
    holder_count: int = 0
    top_holders: list[HolderInfo] = field(default_factory=list)
    total_supply: float = 0.0
    decimals: int = 0
    deployer: str = ""
    # Derived metrics
    top10_concentration: float = 0.0  # % of supply held by top 10
    top1_pct: float = 0.0  # % held by #1 holder


async def get_token_intel(
    mint: str,
    rpc_urls: list[str] | None = None,
    session: aiohttp.ClientSession | None = None,
    timeout_s: float = 10.0,
) -> TokenIntel | None:
    """
    Fetch holder distribution and deployer info for a token.

    Makes 3 parallel RPC calls:
    - getTokenLargestAccounts: top holders
    - getTokenSupply: total supply + decimals
    - getAccountInfo: mint authority

    Tries each RPC endpoint in order until one succeeds.
    """
    rpcs = rpc_urls or DEFAULT_RPCS
    own_session = session is None
    if own_session:
        session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout_s))

    try:
        for rpc in rpcs:
            try:
                results = await asyncio.gather(
                    _rpc_call(session, rpc, "getTokenLargestAccounts", [mint], timeout_s),
                    _rpc_call(session, rpc, "getTokenSupply", [mint], timeout_s),
                    _rpc_call(session, rpc, "getAccountInfo", [mint, {"encoding": "jsonParsed"}], timeout_s),
                    return_exceptions=True,
                )

                largest, supply_result, account_info = results

                # If all three failed, try next RPC
                if all(isinstance(r, Exception) for r in results):
                    continue

                # Parse supply
                supply_data = supply_result.get("value", {}) if isinstance(supply_result, dict) else {}
                total_supply_raw = int(supply_data.get("amount", "0"))
                decimals = supply_data.get("decimals", 0)
                total_supply = total_supply_raw / (10 ** decimals) if decimals > 0 else float(total_supply_raw)

                # Parse holders
                raw_holders = []
                if isinstance(largest, dict):
                    raw_holders = largest.get("value", [])

                holders: list[HolderInfo] = []
                for h in raw_holders:
                    balance_raw = int(h.get("amount", "0"))
                    balance = balance_raw / (10 ** decimals) if decimals > 0 else float(balance_raw)
                    pct = (balance_raw / total_supply_raw * 100) if total_supply_raw > 0 else 0
                    if pct > 0:
                        holders.append(HolderInfo(
                            address=h.get("address", ""),
                            balance=balance,
                            percentage=round(pct, 2),
                        ))

                holders.sort(key=lambda h: h.percentage, reverse=True)

                # Parse deployer (mint authority)
                deployer = ""
                if isinstance(account_info, dict):
                    value = account_info.get("value", {})
                    if value:
                        parsed = value.get("data", {}).get("parsed", {}).get("info", {})
                        deployer = parsed.get("mintAuthority", "") or ""

                top10 = holders[:10]
                top10_pct = sum(h.percentage for h in top10)

                intel = TokenIntel(
                    mint=mint,
                    holder_count=len(holders),
                    top_holders=top10,
                    total_supply=total_supply,
                    decimals=decimals,
                    deployer=deployer,
                    top10_concentration=round(top10_pct, 2),
                    top1_pct=round(holders[0].percentage, 2) if holders else 0,
                )
                return intel

            except Exception as e:
                logger.debug(f"RPC {rpc[:30]} failed for {mint[:8]}: {e}")
                continue

        return None

    finally:
        if own_session and session:
            await session.close()


async def batch_token_intel(
    mints: list[str],
    rpc_urls: list[str] | None = None,
    max_concurrent: int = 3,
    delay_s: float = 0.3,
) -> dict[str, TokenIntel]:
    """
    Fetch token intel for multiple mints with rate limiting.

    Returns dict of mint -> TokenIntel.
    """
    results: dict[str, TokenIntel] = {}
    sem = asyncio.Semaphore(max_concurrent)

    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=12)) as session:
        async def fetch_one(mint: str) -> None:
            async with sem:
                await asyncio.sleep(delay_s)
                intel = await get_token_intel(mint, rpc_urls, session)
                if intel:
                    results[mint] = intel

        await asyncio.gather(*[fetch_one(m) for m in mints], return_exceptions=True)

    return results


async def _rpc_call(
    session: aiohttp.ClientSession,
    endpoint: str,
    method: str,
    params: list[Any],
    timeout_s: float = 10.0,
) -> dict:
    """Make a single JSON-RPC call."""
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": method,
        "params": params,
    }
    async with session.post(
        endpoint,
        json=payload,
        timeout=aiohttp.ClientTimeout(total=timeout_s),
    ) as resp:
        if resp.status != 200:
            raise RuntimeError(f"HTTP {resp.status}")
        data = await resp.json()
        if "error" in data:
            raise RuntimeError(data["error"].get("message", "RPC error"))
        return data.get("result", {})

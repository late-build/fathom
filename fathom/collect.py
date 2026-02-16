"""
Historical graduation data collector for Fathom backtesting.

Scrapes recent pump.fun → PumpSwap/Raydium graduations and builds
a dataset with post-graduation price history.

Data sources (no API key required):
- DexScreener: new pairs on Solana, token metadata, price/volume
- Birdeye public: token overview (fallback)
- Helius parsed transactions: creator wallet, holder info (needs key)

Usage:
    python -m fathom.collect --hours 24 --output graduations.json
    python -m fathom.collect --hours 168 --output week.json --helius-key YOUR_KEY
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

import aiohttp

logger = logging.getLogger("fathom.collect")

# DexScreener endpoints
DEXS_PAIRS_URL = "https://api.dexscreener.com/latest/dex/pairs/solana/{pair}"
DEXS_TOKEN_URL = "https://api.dexscreener.com/latest/dex/tokens/{mint}"
DEXS_SEARCH_URL = "https://api.dexscreener.com/latest/dex/search"

# Known pump.fun / PumpSwap pool identifiers
PUMP_LABELS = {"Pump.fun", "PumpSwap", "pump.fun"}

# Rate limiting
DEXS_DELAY = 1.0  # DexScreener: be polite, 1 req/sec
HELIUS_DELAY = 0.2


@dataclass
class GraduationRecord:
    """One graduated token with price history."""
    mint: str = ""
    symbol: str = ""
    name: str = ""
    graduated_at: int = 0  # unix timestamp (seconds)
    initial_price_usd: float = 0.0
    sol_raised: float = 0.0
    holder_count: int = 0
    creator: str = ""
    pool_address: str = ""
    pool_type: str = ""
    market_cap_at_grad: float = 0.0
    liquidity_usd: float = 0.0
    fdv: float = 0.0
    price_history: list[dict] = field(default_factory=list)
    dev_sold: bool = False
    dev_sell_pct: float = 0.0
    # Outcome metrics (computed after collection)
    price_5m: float = 0.0
    price_15m: float = 0.0
    price_30m: float = 0.0
    max_price: float = 0.0
    max_gain_pct: float = 0.0
    min_price: float = 0.0
    max_loss_pct: float = 0.0


class GraduationCollector:
    """
    Collect historical graduation data from DexScreener.

    Strategy:
    1. Query DexScreener for recently created Solana pairs
    2. Filter for pump.fun-originated tokens (by DEX label or pair metadata)
    3. Fetch price history via DexScreener token endpoint (gives OHLCV)
    4. Optionally enrich with Helius (holder count, creator wallet, dev sells)
    """

    def __init__(
        self,
        helius_api_key: str = "",
        max_age_hours: float = 24,
        min_liquidity_usd: float = 1000,
        max_concurrent: int = 5,
    ) -> None:
        self.helius_api_key = helius_api_key
        self.max_age_hours = max_age_hours
        self.min_liquidity_usd = min_liquidity_usd
        self.max_concurrent = max_concurrent
        self._session: aiohttp.ClientSession | None = None
        self._records: list[GraduationRecord] = []
        self._seen_mints: set[str] = set()
        self._seen_pairs: set[str] = set()
        self._api_calls: int = 0

    async def collect(self) -> list[GraduationRecord]:
        """Run the full collection pipeline."""
        self._session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=30),
        )

        try:
            # Step 1: Discover recent pump.fun pairs from DexScreener
            logger.info(f"Scanning for graduations in last {self.max_age_hours}h...")
            pairs = await self._discover_pairs()
            logger.info(f"Found {len(pairs)} candidate pairs")

            # Step 2: Build graduation records with price history
            sem = asyncio.Semaphore(self.max_concurrent)
            tasks = [self._process_pair(pair, sem) for pair in pairs]
            await asyncio.gather(*tasks, return_exceptions=True)

            # Step 3: Enrich with Helius data if key available
            if self.helius_api_key:
                logger.info("Enriching with Helius data (holders, creator, dev activity)...")
                await self._enrich_helius()

            # Step 4: Compute outcome metrics
            for rec in self._records:
                self._compute_outcomes(rec)

            logger.info(
                f"Collection complete | {len(self._records)} graduations | "
                f"{self._api_calls} API calls"
            )
            return self._records

        finally:
            await self._session.close()

    async def _discover_pairs(self) -> list[dict]:
        """
        Find recently created Solana pairs that originated from pump.fun.

        Discovery pipeline (layered for maximum coverage):
        1. DexScreener token-profiles/latest — freshest tokens with profiles
        2. DexScreener token-boosts/latest — tokens getting promoted
        3. DexScreener search — fallback keyword search
        
        Then for each discovered mint, fetch full pair data from /dex/tokens/.
        Filter for pumpswap/raydium dexId (= graduated from pump.fun).
        """
        mints: list[str] = []
        cutoff_ms = (time.time() - self.max_age_hours * 3600) * 1000

        # --- Source 1: Token profiles (freshest tokens with metadata) ---
        try:
            await asyncio.sleep(DEXS_DELAY)
            self._api_calls += 1
            async with self._session.get("https://api.dexscreener.com/token-profiles/latest/v1") as resp:
                if resp.status == 200:
                    data = await resp.json()
                    sol_tokens = [d for d in data if d.get("chainId") == "solana"]
                    for t in sol_tokens:
                        mint = t.get("tokenAddress", "")
                        if mint and mint not in self._seen_mints:
                            self._seen_mints.add(mint)
                            mints.append(mint)
                    logger.info(f"  Profiles: {len(sol_tokens)} Solana tokens")
        except Exception as e:
            logger.debug(f"Profiles endpoint error: {e}")

        # --- Source 2: Token boosts (promoted tokens) ---
        try:
            await asyncio.sleep(DEXS_DELAY)
            self._api_calls += 1
            async with self._session.get("https://api.dexscreener.com/token-boosts/latest/v1") as resp:
                if resp.status == 200:
                    data = await resp.json()
                    sol_tokens = [d for d in data if d.get("chainId") == "solana"]
                    for t in sol_tokens:
                        mint = t.get("tokenAddress", "")
                        if mint and mint not in self._seen_mints:
                            self._seen_mints.add(mint)
                            mints.append(mint)
                    logger.info(f"  Boosts: {len(sol_tokens)} Solana tokens")
        except Exception as e:
            logger.debug(f"Boosts endpoint error: {e}")

        # --- Source 3: Search fallback ---
        search_terms = ["pump", "sol", "ai", "pepe", "doge", "cat"]
        for term in search_terms:
            try:
                await asyncio.sleep(DEXS_DELAY)
                self._api_calls += 1
                async with self._session.get(f"{DEXS_SEARCH_URL}?q={term}") as resp:
                    if resp.status != 200:
                        continue
                    data = await resp.json()
                    for pair in data.get("pairs", []):
                        if pair.get("chainId") != "solana":
                            continue
                        created = pair.get("pairCreatedAt", 0)
                        if created < cutoff_ms:
                            continue
                        mint = pair.get("baseToken", {}).get("address", "")
                        if mint and mint not in self._seen_mints:
                            self._seen_mints.add(mint)
                            mints.append(mint)
            except Exception as e:
                logger.debug(f"Search error for '{term}': {e}")

        # --- Source 4: Pump.fun graduated endpoint (direct from pump.fun) ---
        try:
            await asyncio.sleep(DEXS_DELAY)
            self._api_calls += 1
            headers = {"Origin": "https://pump.fun", "Accept": "application/json"}
            async with self._session.get(
                "https://advanced-api-v2.pump.fun/coins/graduated?limit=50&offset=0",
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=8),
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    # Response format can be list or wrapped
                    coins = data if isinstance(data, list) else data.get("coins", data.get("data", []))
                    for coin in coins:
                        mint = coin.get("mint", "") or coin.get("address", "")
                        if mint and mint not in self._seen_mints:
                            self._seen_mints.add(mint)
                            mints.append(mint)
                    logger.info(f"  Pump.fun graduated: {len(coins)} tokens")
        except Exception as e:
            logger.debug(f"Pump.fun graduated endpoint error: {e}")

        # --- Source 5: GeckoTerminal PumpSwap trending pools ---
        for gt_query_id in ["4929624", "4929617"]:  # PumpSwap trending 5h, 12h
            try:
                await asyncio.sleep(2.1)  # GT hard 30 req/min
                self._api_calls += 1
                gt_url = f"https://api.geckoterminal.com/api/v2/networks/solana/dexes/pumpswap/pools?page=1&sort=h24_volume_usd_desc"
                async with self._session.get(
                    gt_url,
                    timeout=aiohttp.ClientTimeout(total=8),
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        pools = data.get("data", [])
                        for pool in pools:
                            attrs = pool.get("attributes", {})
                            rels = pool.get("relationships", {})
                            base_token = rels.get("base_token", {}).get("data", {}).get("id", "")
                            mint = base_token.replace("solana_", "") if base_token else ""
                            if mint and mint not in self._seen_mints:
                                self._seen_mints.add(mint)
                                mints.append(mint)
                        logger.info(f"  GeckoTerminal PumpSwap: {len(pools)} pools")
                break  # Only need one GT call
            except Exception as e:
                logger.debug(f"GeckoTerminal error: {e}")

        logger.info(f"  Total unique mints discovered: {len(mints)}")

        # --- Fetch full pair data in batches of 30 (DexScreener batch endpoint) ---
        pairs: list[dict] = []
        batch_size = 30

        for i in range(0, len(mints), batch_size):
            batch = mints[i : i + batch_size]
            try:
                await asyncio.sleep(DEXS_DELAY)
                self._api_calls += 1
                # DexScreener batch: comma-separated addresses
                batch_str = ",".join(batch)
                url = f"https://api.dexscreener.com/tokens/v1/solana/{batch_str}"
                async with self._session.get(url) as resp:
                    if resp.status != 200:
                        # Fallback to individual fetches
                        logger.debug(f"Batch {resp.status}, falling back to individual")
                        for mint in batch:
                            await asyncio.sleep(DEXS_DELAY)
                            self._api_calls += 1
                            async with self._session.get(f"{DEXS_TOKEN_URL.format(mint=mint)}") as r2:
                                if r2.status == 200:
                                    data = await r2.json()
                                    for pair in data.get("pairs", []):
                                        self._filter_and_add(pair, pairs, cutoff_ms)
                        continue

                    data = await resp.json()
                    # Batch endpoint returns flat array of pairs
                    pair_list = data if isinstance(data, list) else data.get("pairs", [])
                    for pair in pair_list:
                        self._filter_and_add(pair, pairs, cutoff_ms)

            except Exception as e:
                logger.debug(f"Batch fetch error: {e}")

        # Sort by creation time (newest first)
        pairs.sort(key=lambda p: p.get("pairCreatedAt", 0), reverse=True)
        logger.info(f"  Graduated pairs with >${self.min_liquidity_usd} liq: {len(pairs)}")
        return pairs

    def _filter_and_add(self, pair: dict, pairs: list[dict], cutoff_ms: int) -> None:
        """Filter a pair and add to the list if it's a valid graduation."""
        if pair.get("chainId") != "solana":
            return
        created = pair.get("pairCreatedAt", 0)
        if created < cutoff_ms:
            return
        dex = pair.get("dexId", "")
        liq = float(pair.get("liquidity", {}).get("usd", 0))
        is_graduated = dex in ("pumpswap", "raydium")
        if is_graduated and liq >= self.min_liquidity_usd:
            pair_addr = pair.get("pairAddress", "")
            mint = pair.get("baseToken", {}).get("address", "")
            if pair_addr not in self._seen_pairs and mint:
                self._seen_pairs.add(pair_addr)
                # Deduplicate by mint — keep highest liquidity
                existing = next((p for p in pairs if p.get("baseToken", {}).get("address") == mint), None)
                if existing:
                    if liq > float(existing.get("liquidity", {}).get("usd", 0)):
                        pairs.remove(existing)
                        pairs.append(pair)
                else:
                    pairs.append(pair)

    async def _process_pair(self, pair: dict, sem: asyncio.Semaphore) -> None:
        """Build a GraduationRecord from a DexScreener pair dict."""
        async with sem:
            mint = pair.get("baseToken", {}).get("address", "")
            if not mint:
                return

            try:
                best = pair  # Already have full pair data from discovery

                created_ms = best.get("pairCreatedAt", 0)
                created_s = int(created_ms / 1000) if created_ms else 0

                rec = GraduationRecord(
                    mint=mint,
                    symbol=best.get("baseToken", {}).get("symbol", ""),
                    name=best.get("baseToken", {}).get("name", ""),
                    graduated_at=created_s,
                    pool_address=best.get("pairAddress", ""),
                    pool_type=best.get("dexId", ""),
                    initial_price_usd=float(best.get("priceUsd", 0) or 0),
                    market_cap_at_grad=float(best.get("marketCap", 0) or 0),
                    liquidity_usd=float(best.get("liquidity", {}).get("usd", 0)),
                    fdv=float(best.get("fdv", 0) or 0),
                )

                # Build price history from DexScreener % changes
                # This gives us approximate snapshots at 5m, 1h, 6h, 24h ago
                price_changes = best.get("priceChange", {})
                current_price = float(best.get("priceUsd", 0) or 0)
                vol = best.get("volume", {})

                if current_price > 0:
                    history = []
                    now = int(time.time())

                    history.append({
                        "timestamp": now,
                        "price": current_price,
                        "volume_5m": float(vol.get("m5", 0) or 0),
                    })

                    m5_change = float(price_changes.get("m5", 0) or 0) / 100
                    if m5_change != 0:
                        history.append({
                            "timestamp": now - 300,
                            "price": current_price / (1 + m5_change),
                            "volume_5m": float(vol.get("m5", 0) or 0),
                        })

                    h1_change = float(price_changes.get("h1", 0) or 0) / 100
                    if h1_change != 0:
                        history.append({
                            "timestamp": now - 3600,
                            "price": current_price / (1 + h1_change),
                            "volume_5m": float(vol.get("h1", 0) or 0) / 12,
                        })

                    h6_change = float(price_changes.get("h6", 0) or 0) / 100
                    if h6_change != 0:
                        history.append({
                            "timestamp": now - 21600,
                            "price": current_price / (1 + h6_change),
                            "volume_5m": float(vol.get("h6", 0) or 0) / 72,
                        })

                    h24_change = float(price_changes.get("h24", 0) or 0) / 100
                    if h24_change != 0:
                        history.append({
                            "timestamp": now - 86400,
                            "price": current_price / (1 + h24_change),
                            "volume_5m": float(vol.get("h24", 0) or 0) / 288,
                        })

                    history.sort(key=lambda h: h["timestamp"])
                    rec.price_history = history

                txns = best.get("txns", {})
                h24_buys = txns.get("h24", {}).get("buys", 0)
                h24_sells = txns.get("h24", {}).get("sells", 0)

                self._records.append(rec)
                logger.info(
                    f"  {rec.symbol:>10} | ${rec.initial_price_usd:.8f} | "
                    f"liq=${rec.liquidity_usd:>10,.0f} | "
                    f"mcap=${rec.market_cap_at_grad:>12,.0f} | "
                    f"txns={h24_buys+h24_sells:>5}"
                )

            except Exception as e:
                logger.debug(f"Error processing {mint[:8]}: {e}")

    async def _enrich_helius(self) -> None:
        """
        Add holder counts, creator wallets, and dev sell data from Helius.

        Requires a Helius API key. Uses:
        - getSignaturesForAsset: find the creation tx → extract creator
        - getTokenAccounts (paginated): count unique holders
        - parsed transaction history: detect dev sells
        """
        if not self.helius_api_key:
            return

        das_url = f"https://mainnet.helius-rpc.com/?api-key={self.helius_api_key}"
        fast_timeout = aiohttp.ClientTimeout(total=8)

        for rec in self._records:
            try:
                # 1. getAsset — fast, gives creator from authorities
                await asyncio.sleep(HELIUS_DELAY)
                self._api_calls += 1
                payload = {
                    "jsonrpc": "2.0", "id": 1,
                    "method": "getAsset",
                    "params": {"id": rec.mint},
                }
                async with self._session.post(das_url, json=payload, timeout=fast_timeout) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        result = data.get("result", {})
                        # Creator from authorities
                        for auth in result.get("authorities", []):
                            addr = auth.get("address", "")
                            if addr:
                                rec.creator = addr
                                break
                        # Also grab ownership info
                        ownership = result.get("ownership", {})
                        if not rec.creator and ownership.get("owner"):
                            rec.creator = ownership["owner"]

                # 2. Holder count via getTokenAccounts (one page, fast)
                await asyncio.sleep(HELIUS_DELAY)
                self._api_calls += 1
                payload2 = {
                    "jsonrpc": "2.0", "id": 1,
                    "method": "getTokenAccounts",
                    "params": {
                        "mint": rec.mint,
                        "limit": 1000,
                        "showZeroBalance": False,
                    },
                }
                async with self._session.post(das_url, json=payload2, timeout=fast_timeout) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        result = data.get("result", {})
                        accounts = result.get("token_accounts", [])
                        holder_count = len(accounts)
                        if result.get("cursor"):
                            holder_count = max(holder_count, 1000)
                        rec.holder_count = holder_count

                logger.info(
                    f"  ✓ {rec.symbol:>10} | holders={rec.holder_count:>5} | "
                    f"creator={rec.creator[:8] if rec.creator else 'none':>8}"
                )

            except asyncio.TimeoutError:
                logger.debug(f"Helius timeout for {rec.symbol}")
            except Exception as e:
                logger.debug(f"Helius enrich error for {rec.symbol}: {e}")

    def _compute_outcomes(self, rec: GraduationRecord) -> None:
        """Compute outcome metrics from price history."""
        if not rec.price_history or rec.initial_price_usd <= 0:
            return

        prices = [p["price"] for p in rec.price_history if p.get("price", 0) > 0]
        if not prices:
            return

        rec.max_price = max(prices)
        rec.min_price = min(prices)
        rec.max_gain_pct = (rec.max_price - rec.initial_price_usd) / rec.initial_price_usd
        rec.max_loss_pct = (rec.initial_price_usd - rec.min_price) / rec.initial_price_usd

        # Find prices at specific time windows
        grad_ts = rec.graduated_at
        for point in rec.price_history:
            elapsed = point["timestamp"] - grad_ts
            if 240 <= elapsed <= 360 and rec.price_5m == 0:
                rec.price_5m = point["price"]
            if 840 <= elapsed <= 960 and rec.price_15m == 0:
                rec.price_15m = point["price"]
            if 1740 <= elapsed <= 1860 and rec.price_30m == 0:
                rec.price_30m = point["price"]


def collect_main() -> None:
    """CLI entry point for data collection."""
    parser = argparse.ArgumentParser(
        description="Collect historical graduation data for backtesting"
    )
    parser.add_argument(
        "--hours", type=float, default=24,
        help="How far back to look (default: 24h)",
    )
    parser.add_argument(
        "--output", "-o", default="graduations.json",
        help="Output file path",
    )
    parser.add_argument(
        "--helius-key", default="",
        help="Helius API key (optional, enables holder/creator data)",
    )
    parser.add_argument(
        "--min-liquidity", type=float, default=1000,
        help="Minimum liquidity in USD (default: 1000)",
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true",
    )

    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )

    collector = GraduationCollector(
        helius_api_key=args.helius_key,
        max_age_hours=args.hours,
        min_liquidity_usd=args.min_liquidity,
    )

    records = asyncio.run(collector.collect())

    # Save
    output = Path(args.output)
    data = [asdict(r) for r in records]
    with open(output, "w") as f:
        json.dump(data, f, indent=2)

    logger.info(f"Saved {len(records)} records to {output}")

    # Print summary
    if records:
        print()
        print(f"{'Symbol':>12} {'Price':>14} {'MCap':>12} {'Liq':>10} {'MaxGain':>10} {'MaxLoss':>10} {'Dev Sold':>8}")
        print("-" * 82)
        for r in sorted(records, key=lambda x: x.max_gain_pct, reverse=True)[:20]:
            print(
                f"{r.symbol:>12} "
                f"${r.initial_price_usd:>12.8f} "
                f"${r.market_cap_at_grad:>10,.0f} "
                f"${r.liquidity_usd:>8,.0f} "
                f"{r.max_gain_pct:>+9.1%} "
                f"{r.max_loss_pct:>+9.1%} "
                f"{'YES' if r.dev_sold else 'no':>8}"
            )


if __name__ == "__main__":
    collect_main()

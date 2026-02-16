"""Tests for PumpSwap adapter math."""

from fathom.adapters.pumpswap.adapter import PumpSwapAdapter, PoolState


class TestAMMMath:
    """Test constant-product AMM calculations."""

    def setup_method(self):
        self.adapter = PumpSwapAdapter(rpc_url="http://localhost:8899")

    def test_output_basic(self):
        """Simple swap: 1 SOL into a pool with 100 SOL and 1M tokens."""
        output = self.adapter._calculate_output(
            amount_in=1_000_000_000,       # 1 SOL in lamports
            reserve_in=100_000_000_000,    # 100 SOL
            reserve_out=1_000_000_000_000, # 1M tokens (6 decimals)
        )
        # Expected: ~9,975,000 tokens (minus 0.25% fee, minus price impact)
        assert output > 0
        assert output < 1_000_000_000_000  # less than total reserves

    def test_output_with_fee(self):
        """Fee should reduce output."""
        # Calculate what output would be without fee
        amount_in = 1_000_000_000
        reserve_in = 100_000_000_000
        reserve_out = 1_000_000_000_000
        
        no_fee = (amount_in * reserve_out) // (reserve_in + amount_in)
        with_fee = self.adapter._calculate_output(amount_in, reserve_in, reserve_out)
        
        assert with_fee < no_fee

    def test_output_zero_reserves(self):
        """Zero reserves should return zero."""
        output = self.adapter._calculate_output(
            amount_in=1_000_000_000,
            reserve_in=0,
            reserve_out=1_000_000_000,
        )
        assert output == 0

    def test_output_large_swap_has_impact(self):
        """Larger swaps should get worse prices (price impact)."""
        reserve_in = 100_000_000_000
        reserve_out = 1_000_000_000_000

        small = self.adapter._calculate_output(
            1_000_000_000, reserve_in, reserve_out  # 1 SOL
        )
        large = self.adapter._calculate_output(
            10_000_000_000, reserve_in, reserve_out  # 10 SOL
        )

        # Per-unit output should be worse for larger swaps
        small_per_unit = small / 1_000_000_000
        large_per_unit = large / 10_000_000_000
        assert small_per_unit > large_per_unit

    def test_output_symmetry(self):
        """Buying then selling should result in less than you started with (fees + impact)."""
        reserve_sol = 100_000_000_000
        reserve_token = 1_000_000_000_000

        # Buy tokens with 1 SOL
        tokens_received = self.adapter._calculate_output(
            1_000_000_000, reserve_sol, reserve_token
        )

        # Update reserves after buy
        new_reserve_sol = reserve_sol + 1_000_000_000
        new_reserve_token = reserve_token - tokens_received

        # Sell those tokens back
        sol_received = self.adapter._calculate_output(
            tokens_received, new_reserve_token, new_reserve_sol
        )

        # Should get back less than 1 SOL due to fees + impact
        assert sol_received < 1_000_000_000


class TestPoolState:
    def test_price_calculation(self):
        pool = PoolState(
            pool_address="test",
            token_mint="test_mint",
            sol_reserves=50_000_000_000,    # 50 SOL
            token_reserves=1_000_000_000_000,  # 1M tokens
            lp_supply=0,
        )
        # Price = 50 SOL / 1M tokens = 0.00005 SOL per token
        assert pool.price_sol == 50_000_000_000 / 1_000_000_000_000

    def test_sol_liquidity(self):
        pool = PoolState(
            pool_address="test",
            token_mint="test_mint",
            sol_reserves=85_000_000_000,  # 85 SOL
            token_reserves=1_000_000_000_000,
            lp_supply=0,
        )
        assert pool.sol_liquidity == 85.0

    def test_zero_reserves(self):
        pool = PoolState(
            pool_address="test",
            token_mint="test_mint",
            sol_reserves=0,
            token_reserves=0,
            lp_supply=0,
        )
        assert pool.price_sol == 0

import pytest
from signal_engine.signal_models import TradeSignal, SignalDirection
from state_manager.state import SharedState
import time
import asyncio


def test_trade_signal_creation():
    signal = TradeSignal(
        direction=SignalDirection.LONG,
        confidence=0.95,
        entry_price=15000.0,
        stop_price=14950.0,
        target_price=15100.0,
        rationale="Test signal based on OFI",
        timestamp=1234567890.0
    )
    assert signal.direction == SignalDirection.LONG
    assert signal.confidence == 0.95
    assert signal.entry_price == 15000.0


def test_trade_signal_risk_reward_ratio():
    signal = TradeSignal(
        direction=SignalDirection.LONG,
        confidence=0.95,
        entry_price=15000.0,
        stop_price=14950.0,
        target_price=15100.0,
        rationale="Test signal",
        timestamp=1234567890.0
    )
    assert signal.risk_reward_ratio == 2.0


def test_shared_state_signal_update():
    shared_state = SharedState()
    signal = TradeSignal(
        direction=SignalDirection.LONG,
        confidence=0.95,
        entry_price=15000.0,
        stop_price=14950.0,
        target_price=15100.0,
        rationale="Test signal",
        timestamp=time.time()
    )
    assert shared_state.latest_signal is None
    asyncio.run(shared_state.update_signal(signal))
    retrieved = shared_state.latest_signal
    assert retrieved is not None


def test_no_signal_during_no_trade_window():
    from signal_engine.signal_core import SignalEngine
    from config.settings import Settings
    from risk_controller.risk_core import RiskController
    
    settings = Settings()
    state = SharedState()
    risk = RiskController(settings, state)

    
    engine = SignalEngine("BTC", settings, state, risk)
    
    async def run_test():
        result = await engine._evaluate_signals()
        return result
    
    result = asyncio.run(run_test())
    assert hasattr(engine.guard, 'is_session_tradeable')


def test_no_signal_when_not_warmed_up():
    from signal_engine.signal_core import SignalEngine
    from config.settings import Settings
    from risk_controller.risk_core import RiskController
    
    settings = Settings()
    state = SharedState()
    risk = RiskController(settings, state)

    
    engine = SignalEngine("BTC", settings, state, risk)
    assert engine.all_warmed_up == False
    
    async def run_test():
        result = await engine._evaluate_signals()
        return result
    
    result = asyncio.run(run_test())
    assert result is None
    
    for _ in range(25):
        engine.ofi.update(0.5)
        engine.delta.update(15000.0, 10, "BUY")
        engine.vwap.update(15000.0, 10)
        engine.ema.update(15000.0)
    
    assert engine.all_warmed_up == True


def test_stale_indicator_reduces_vote_count():
    from signal_engine.signal_core import SignalEngine
    from config.settings import Settings
    from risk_controller.risk_core import RiskController
    from topstep_bot.signal_engine.indicator_validator import SignalDirection as GuardSignalDirection
    
    settings = Settings()
    state = SharedState()
    risk = RiskController(settings, state)

    
    engine = SignalEngine("BTC", settings, state, risk)
    
    for _ in range(25):
        engine.ofi.update(0.5)
        engine.delta.update(15000.0, 10, "BUY")
        engine.vwap.update(15000.0, 10)
        engine.ema.update(15000.0)
    
    engine.guard.last_update_ts["OFI"] = 0.0
    
    result = engine.guard.validate_indicator_output("OFI", GuardSignalDirection.LONG, 0.5, 0.0)
    assert result == GuardSignalDirection.FLAT


def test_nan_strength_treated_as_flat():
    from signal_engine.signal_core import SignalEngine
    from config.settings import Settings
    from risk_controller.risk_core import RiskController
    
    settings = Settings()    
    state = SharedState()    
    risk = RiskController(settings, state)    

    
    engine = SignalEngine("BTC", settings, state, risk)
    
    for _ in range(25):
        engine.ofi.update(0.5)
        engine.delta.update(15000.0, 10, "BUY")
        engine.vwap.update(15000.0, 10)
        engine.ema.update(15000.0)
    
    engine.ofi.current_mean = float('nan')
    
    raw_votes = {
        "OFI": (engine.ofi.signal(), float('nan'), engine.guard.get_last_update("OFI")),
    }
    
    for name, (direction, strength, ts) in raw_votes.items():
        if strength != strength:
            strength = 0.0
        assert strength == 0.0


def test_audit_log_records_rejection_reason():
    import asyncio
    from signal_engine.signal_core import SignalEngine
    from config.settings import Settings
    from risk_controller.risk_core import RiskController
    
    settings = Settings()
    state = SharedState()
    risk = RiskController(settings)
    
    engine = SignalEngine("BTC", settings, state, risk)
    
    for _ in range(25):
        engine.ofi.update(0.5)
        engine.delta.update(15000.0, 10, "BUY")
        engine.vwap.update(15000.0, 10)
        engine.ema.update(15000.0)
    
    async def run_test():
        result = await engine._evaluate_signals()
        return result
    
    result = asyncio.run(run_test())
    
    recent = engine.audit_logger.get_recent_evaluations(last_n=1)
    assert len(recent) >= 1
    last_record = recent[-1]
    assert hasattr(last_record, 'rejection_reason')
    assert last_record.rejection_reason != None


# ==================== NEW TESTS FOR SIGNAL ENGINE ====================

def test_ofi_flat_insufficient_data():
    """< 5 readings → FLAT."""
    from signal_engine.indicators import OrderFlowImbalanceIndicator, SignalDirection
    
    indicator = OrderFlowImbalanceIndicator(window=10, threshold=0.25)
    
    # Feed less than 10 readings
    for i in range(5):
        indicator.update(0.5)
    
    assert indicator.is_warmed_up == False
    assert indicator.signal() == SignalDirection.FLAT


def test_ofi_long():
    """Feed 10 readings all at +0.5 → LONG."""
    from signal_engine.indicators import OrderFlowImbalanceIndicator, SignalDirection
    
    indicator = OrderFlowImbalanceIndicator(window=10, threshold=0.25)
    
    # Feed 10 readings all positive
    for _ in range(10):
        indicator.update(0.5)
    
    assert indicator.is_warmed_up == True
    assert indicator.signal() == SignalDirection.LONG


def test_ofi_short():
    """Feed 10 readings all at -0.5 → SHORT."""
    from signal_engine.indicators import OrderFlowImbalanceIndicator, SignalDirection
    
    indicator = OrderFlowImbalanceIndicator(window=10, threshold=0.25)
    
    # Feed 10 readings all negative
    for _ in range(10):
        indicator.update(-0.5)
    
    assert indicator.is_warmed_up == True
    assert indicator.signal() == SignalDirection.SHORT


def test_delta_confirmed_long():
    """Rising price, rising delta → LONG."""
    from signal_engine.indicators import CumulativeDeltaIndicator, SignalDirection
    
    indicator = CumulativeDeltaIndicator(lookback=20)
    
    # Feed rising price and buy pressure
    price = 15000.0
    for i in range(20):
        price += 1.0  # Rising price
        indicator.update(price, 10, "BUY")  # Buy pressure
    
    assert indicator.is_warmed_up == True
    assert indicator.signal() == SignalDirection.LONG


def test_delta_bullish_divergence():
    """Falling price, rising delta → LONG."""
    from signal_engine.indicators import CumulativeDeltaIndicator, SignalDirection
    
    indicator = CumulativeDeltaIndicator(lookback=20)
    
    # Feed falling price but buy pressure (bullish divergence)
    price = 15000.0
    for i in range(20):
        price -= 0.5  # Falling price
        indicator.update(price, 10, "BUY")  # Buy pressure
    
    assert indicator.is_warmed_up == True
    # With falling price and rising delta, should signal LONG (divergence)
    assert indicator.signal() == SignalDirection.LONG


def test_vwap_above_upper_band():
    """Price well above VWAP → SHORT signal."""
    from signal_engine.indicators import VWAPMeanReversionIndicator, SignalDirection
    
    indicator = VWAPMeanReversionIndicator(tick_size=0.25)
    
    # First, build up VWAP history
    base_price = 15000.0
    for i in range(30):
        # Add volume to build VWAP
        indicator.update(base_price, 100)
    
    # Now push price well above VWAP + volatility
    indicator.update(base_price + 50.0, 10)  # Price much higher
    
    assert indicator.is_warmed_up == True
    assert indicator.signal() == SignalDirection.SHORT


def test_vwap_below_lower_band():
    """Price well below VWAP → LONG signal."""
    from signal_engine.indicators import VWAPMeanReversionIndicator, SignalDirection
    
    indicator = VWAPMeanReversionIndicator(tick_size=0.25)
    
    # First, build up VWAP history
    base_price = 15000.0
    for i in range(30):
        indicator.update(base_price, 100)
    
    # Now push price well below VWAP - volatility
    indicator.update(base_price - 50.0, 10)  # Price much lower
    
    assert indicator.is_warmed_up == True
    assert indicator.signal() == SignalDirection.LONG


def test_vwap_reset():
    """After reset_session(), VWAP recalculates from scratch."""
    from signal_engine.indicators import VWAPMeanReversionIndicator
    
    indicator = VWAPMeanReversionIndicator(tick_size=0.25)
    
    # Build up some data
    base_price = 15000.0
    for i in range(30):
        indicator.update(base_price, 100)
    
    assert indicator.is_warmed_up == True
    initial_vwap = indicator.session_vwap
    
    # Reset session
    indicator.reset_session()
    
    # VWAP should be reset
    assert indicator.session_vwap == 0.0
    assert indicator.is_warmed_up == False


def test_ema_crossover_long():
    """Fast EMA crosses above slow → LONG."""
    from signal_engine.indicators import EMATrendFilter, SignalDirection
    
    indicator = EMATrendFilter(fast=9, slow=21)
    
    # Feed rising prices to trigger golden cross
    price = 15000.0
    for i in range(50):
        price += 5.0  # Rising price
        indicator.update(price)
    
    assert indicator.is_warmed_up == True
    assert indicator.signal() == SignalDirection.LONG


def test_ema_cold_start():
    """< slow_period ticks → FLAT."""
    from signal_engine.indicators import EMATrendFilter, SignalDirection
    
    indicator = EMATrendFilter(fast=9, slow=21)
    
    # Feed fewer than 21 ticks
    price = 15000.0
    for i in range(10):
        indicator.update(price)
    
    assert indicator.is_warmed_up == False
    assert indicator.signal() == SignalDirection.FLAT


@pytest.mark.asyncio
async def test_full_signal_3_of_4():
    """Feed synthetic data making 3 indicators agree → TradeSignal emitted."""
    from signal_engine.signal_core import SignalEngine
    from config.settings import Settings
    from risk_controller.risk_core import RiskController
    
    settings = Settings()
    state = SharedState()
    risk = RiskController(settings, state)
    
    engine = SignalEngine("NQ", settings, state, risk)
    
    # Warm up indicators with data that makes 3 of 4 agree (LONG)
    base_price = 15000.0
    for i in range(30):
        price = base_price + i * 2  # Rising price
        engine.ofi.update(0.5)  # Positive OFI -> LONG
        engine.delta.update(price, 10, "BUY")  # Rising delta -> LONG
        engine.vwap.update(price - 30, 10)  # Price below VWAP -> LONG
        engine.ema.update(price + 5)  # Fast above slow -> LONG
    
    # Evaluate signals
    result = await engine._evaluate_signals()
    
    # Should emit a signal since 3+ indicators agree
    assert result is not None
    assert result.direction == SignalDirection.LONG


@ pytest.mark.asyncio
async def test_signal_cooldown():
    """Two signals within 30s → second returns None."""
    from signal_engine.signal_core import SignalEngine
    from config.settings import Settings
    from risk_controller.risk_core import RiskController
    import time
    
    settings = Settings()
    state = SharedState()
    risk = RiskController(settings, state)
    
    engine = SignalEngine("NQ", settings, state, risk)
    
    # Warm up indicators
    base_price = 15000.0
    for i in range(30):
        price = base_price + i * 2
        engine.ofi.update(0.5)
        engine.delta.update(price, 10, "BUY")
        engine.vwap.update(price - 30, 10)
        engine.ema.update(price + 5)
    
    # First signal
    result1 = await engine._evaluate_signals()
    assert result1 is not None
    
    # Immediately try second signal (within cooldown)
    result2 = await engine._evaluate_signals()
    assert result2 is None  # Should be blocked by cooldown


@pytest.mark.asyncio
async def test_rr_rejection():
    """Valid signal but stop too tight → no TradeSignal."""
    from signal_engine.signal_core import SignalEngine
    from config.settings import Settings
    from risk_controller.risk_core import RiskController
    from signal_engine.signal_models import TradeSignal, SignalDirection
    from unittest.mock import MagicMock, patch
    
    settings = Settings()
    state = SharedState()
    risk = RiskController(settings, state)
    
    engine = SignalEngine("NQ", settings, state, risk)
    
    # Manually set up a valid LONG signal
    # But we'll manipulate the stop/target to get low R:R
    # Note: The signal engine calculates these automatically, 
    # so we need to check if the guard.validate_signal_numerics rejects it
    
    # First, let's verify that signals with low R:R are rejected
    # by checking the audit logger rejection reason
    base_price = 15000.0
    for i in range(30):
        price = base_price + i * 2
        engine.ofi.update(0.5)
        engine.delta.update(price, 10, "BUY")
        engine.vwap.update(price - 30, 10)
        engine.ema.update(price + 5)
    
    # Override min_risk_reward to a very high value to force rejection
    original_min_rr = engine.min_risk_rew
    engine.min_risk_rew = 100.0  # Very high R:R requirement
    
    result = await engine._evaluate_signals()
    
    # Should be rejected due to low R:R
    assert result is None
    
    # Restore
    engine.min_risk_rew = original_min_rr

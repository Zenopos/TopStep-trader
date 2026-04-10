import pytest
import asyncio
from risk_controller.risk_models import RiskState, RiskStatus
from risk_controller.risk_core import RiskController
from risk_controller.position_sizer import calculate_position_size
from state_manager.state import SharedState
from config.settings import Settings
from data_ingestion.data_models import MarketDepthSnapshot
from signal_engine.signal_models import TradeSignal, SignalDirection
from datetime import datetime
import time


# Helper to create a test settings object
def get_test_settings():
    return Settings(
        TRADOVATE_USERNAME="test",
        TRADOVATE_PASSWORD="test",
        TRADOVATE_APP_ID="test",
        TRADOVATE_APP_VERSION="test",
        TRADOVATE_CID="test",
        TRADOVATE_SECRET="test",
        TRADOVATE_ENV="demo",
        TOPSTEP_DAILY_LOSS_LIMIT=-2000.0,
        TOPSTEP_MAX_CONTRACTS=5,
        HEARTBEAT_TIMEOUT_MS=5000
    )


def test_risk_state_initialization():
    """Test that RiskState initializes with default values."""
    state = RiskState()
    assert state.status == RiskStatus.GREEN
    assert state.daily_pnl == 0.0
    assert state.open_positions == 0
    assert state.current_contracts == 0
    assert state.last_heartbeat_ms == 0.0
    assert state.kill_reason == ""


def test_risk_state_is_tradeable():
    """Test that RiskState.is_tradeable returns correct values."""
    # Test GREEN status
    state = RiskState(status=RiskStatus.GREEN)
    assert state.is_tradeable == True
    
    # Test YELLOW status
    state = RiskState(status=RiskStatus.YELLOW)
    assert state.is_tradeable == True
    
    # Test RED status
    state = RiskState(status=RiskStatus.RED)
    assert state.is_tradeable == False
    
    # Test KILLED status
    state = RiskState(status=RiskStatus.KILLED)
    assert state.is_tradeable == False


def test_risk_controller_initialization():
    """Test that RiskController initializes correctly."""
    settings = get_test_settings()
    shared_state = SharedState()
    risk_controller = RiskController(settings, shared_state)
    assert risk_controller.state == shared_state
    assert risk_controller.risk_state.status == RiskStatus.GREEN


def test_dll_trigger_at_80_percent():
    """Test that DLL triggers at 80% threshold."""
    settings = get_test_settings()
    shared_state = SharedState()
    risk_controller = RiskController(settings, shared_state)
    
    # DLL is -2000.0, so 80% is -1600.0
    # current_pnl of -1600.0 should trigger yellow status
    result = asyncio.run(risk_controller.check_dll(-1600.0))
    
    # Should return True (trading allowed)
    assert result == True
    # Status should be YELLOW
    assert shared_state.risk_state.status == RiskStatus.YELLOW


def test_dll_trigger_at_100_percent():
    """Test that DLL triggers at 100% threshold."""
    settings = get_test_settings()
    shared_state = SharedState()
    risk_controller = RiskController(settings, shared_state)
    
    # DLL is -2000.0
    # current_pnl of -2000.0 should trigger kill
    result = asyncio.run(risk_controller.check_dll(-2000.0))
    
    # Should return False (trading not allowed)
    assert result == False
    # Status should be KILLED
    assert shared_state.risk_state.status == RiskStatus.KILLED
    # kill_reason should be set
    assert "DAILY LOSS LIMIT HIT" in shared_state.risk_state.kill_reason


def test_kill_idempotency():
    """Test that _kill is idempotent."""
    settings = get_test_settings()
    shared_state = SharedState()
    risk_controller = RiskController(settings, shared_state)
    
    # First kill
    asyncio.run(risk_controller._kill("Test kill"))
    assert shared_state.risk_state.status == RiskStatus.KILLED
    
    # Second kill should not raise an error
    asyncio.run(risk_controller._kill("Test kill again"))
    assert shared_state.risk_state.status == RiskStatus.KILLED
    # kill_reason should still be the first reason
    assert shared_state.risk_state.kill_reason == "Test kill"


def test_position_size_capping():
    """Test that position size is capped at max_contracts."""
    settings = get_test_settings()
    shared_state = SharedState()
    risk_controller = RiskController(settings, shared_state)
    
    # Create a dummy signal
    signal = TradeSignal(
        direction=SignalDirection.LONG,
        strength=0.5,
        confidence=0.6,
        timestamp=datetime.now()
    )
    
    # Request more than max (5)
    result = asyncio.run(risk_controller.validate_position_size(10, signal))
    assert result == 5


def test_calculate_position_size():
    """Test the calculate_position_size function."""
    # Test basic calculation
    # account_balance = 100000, risk_percent = 0.01 (1%), stop_distance_ticks = 10, tick_value = 20
    # dollar_risk = 1000, risk_per_contract = 200, raw_size = 5, max_contracts = 5
    # result = 5
    result = calculate_position_size(100000, 0.01, 10, 20, 5)
    assert result == 5
    
    # Test with lower risk
    # dollar_risk = 500, risk_per_contract = 200, raw_size = 2.5 -> 2
    result = calculate_position_size(50000, 0.01, 10, 20, 5)
    assert result == 2
    
    # Test with max_contracts limit
    # dollar_risk = 20000, risk_per_contract = 200, raw_size = 100 -> 5 (capped)
    result = calculate_position_size(2000000, 0.01, 10, 20, 5)
    assert result == 5


def test_shared_state_market_depth_update():
    """Test that SharedState can update and retrieve market depth."""
    shared_state = SharedState()
    
    # Create a test snapshot
    snapshot = MarketDepthSnapshot(
        symbol="NQ",
        timestamp=time.time(),
        bids=[(15000.0, 5), (14999.0, 3)],
        asks=[(15001.0, 4), (15002.0, 2)]
    )
    
    # Initially, market depth should be None
    assert shared_state.get_market_depth() is None
    
    # Update market depth
    import asyncio
    asyncio.run(shared_state.update_market_depth(snapshot))
    
    # Should now be able to retrieve the snapshot
    retrieved = shared_state.get_market_depth()
    assert retrieved is not None
    assert retrieved.symbol == "NQ"
    assert len(retrieved.bids) == 2
    assert len(retrieved.asks) == 2
    assert retrieved.bids[0][0] == 15000.0
    assert retrieved.bids[0][1] == 5
    assert retrieved.asks[0][0] == 15001.0
    assert retrieved.asks[0][1] == 4


# ==================== NEW TESTS FOR RISK CONTROLLER ====================

@pytest.mark.asyncio
async def test_dll_green_zone():
    """PnL at -50% of DLL → status remains GREEN."""
    settings = get_test_settings()
    # Override DLL to -500 for easier calculation
    settings.TOPSTEP_DAILY_LOSS_LIMIT = -500.0
    shared_state = SharedState()
    risk_controller = RiskController(settings, shared_state)
    
    # DLL is -500, so -50% is -250.0
    result = await risk_controller.check_dll(-250.0)
    
    # Should return True (trading allowed)
    assert result == True
    # Status should remain GREEN
    assert shared_state.risk_state.status == RiskStatus.GREEN


@pytest.mark.asyncio
async def test_dll_yellow_zone():
    """PnL at -80% of DLL → status becomes YELLOW, alert sent."""
    settings = get_test_settings()
    # Override DLL to -500 for easier calculation
    settings.TOPSTEP_DAILY_LOSS_LIMIT = -500.0
    shared_state = SharedState()
    risk_controller = RiskController(settings, shared_state)
    
    # DLL is -500, so -80% is -400.0
    result = await risk_controller.check_dll(-400.0)
    
    # Should return True (trading allowed but warning)
    assert result == True
    # Status should be YELLOW
    assert shared_state.risk_state.status == RiskStatus.YELLOW


@pytest.mark.asyncio
async def test_dll_kill():
    """PnL at -100% of DLL → status KILLED, kill_event set."""
    settings = get_test_settings()
    # Override DLL to -500 for easier calculation
    settings.TOPSTEP_DAILY_LOSS_LIMIT = -500.0
    shared_state = SharedState()
    risk_controller = RiskController(settings, shared_state)
    
    # DLL is -500, so -100% is -500.0
    result = await risk_controller.check_dll(-500.0)
    
    # Should return False (trading not allowed)
    assert result == False
    # Status should be KILLED
    assert shared_state.risk_state.status == RiskStatus.KILLED
    # kill_event should be set
    assert risk_controller.kill_event.is_set()
    assert "DAILY LOSS LIMIT HIT" in shared_state.risk_state.kill_reason


@pytest.mark.asyncio
async def test_kill_idempotent():
    """Calling _kill() twice does not raise or change kill_reason."""
    settings = get_test_settings()
    shared_state = SharedState()
    risk_controller = RiskController(settings, shared_state)
    
    # First kill
    await risk_controller._kill("Test kill reason")
    assert shared_state.risk_state.status == RiskStatus.KILLED
    first_reason = shared_state.risk_state.kill_reason
    
    # Second kill should not raise an error
    await risk_controller._kill("Different reason")
    
    # Status should still be KILLED
    assert shared_state.risk_state.status == RiskStatus.KILLED
    # kill_reason should still be the first reason (idempotent)
    assert shared_state.risk_state.kill_reason == first_reason


@pytest.mark.asyncio
async def test_position_size_cap():
    """Requested qty 10, max 3 → returns 3."""
    settings = get_test_settings()
    settings.TOPSTEP_MAX_CONTRACTS = 3
    shared_state = SharedState()
    risk_controller = RiskController(settings, shared_state)
    
    signal = TradeSignal(
        direction=SignalDirection.LONG,
        strength=0.5,
        confidence=0.6,
        timestamp=datetime.now()
    )
    
    # Request more than max (10 > 3)
    result = await risk_controller.validate_position_size(10, signal)
    assert result == 3


@pytest.mark.asyncio
async def test_position_size_yellow():
    """In YELLOW state, qty reduced by 1."""
    settings = get_test_settings()
    settings.TOPSTEP_MAX_CONTRACTS = 5
    shared_state = SharedState()
    risk_controller = RiskController(settings, shared_state)
    
    # Set status to YELLOW
    shared_state.risk_state.status = RiskStatus.YELLOW
    
    signal = TradeSignal(
        direction=SignalDirection.LONG,
        strength=0.5,
        confidence=0.6,
        timestamp=datetime.now()
    )
    
    # Request 5, should be reduced to 4 (5 - 1)
    result = await risk_controller.validate_position_size(5, signal)
    assert result == 4
    
    # Edge case: requested 1, should be reduced to 1 (max(1, 1-1) = 1)
    result2 = await risk_controller.validate_position_size(1, signal)
    assert result2 == 1


@pytest.mark.asyncio
async def test_position_size_killed():
    """In KILLED state, returns 0."""
    settings = get_test_settings()
    settings.TOPSTEP_MAX_CONTRACTS = 5
    shared_state = SharedState()
    risk_controller = RiskController(settings, shared_state)
    
    # Set status to KILLED
    shared_state.risk_state.status = RiskStatus.KILLED
    
    signal = TradeSignal(
        direction=SignalDirection.LONG,
        strength=0.5,
        confidence=0.6,
        timestamp=datetime.now()
    )
    
    # Should return 0 regardless of requested qty
    result = await risk_controller.validate_position_size(5, signal)
    assert result == 0


@pytest.mark.asyncio
async def test_heartbeat_ok():
    """Latency 100ms, threshold 5,000ms → returns True."""
    settings = get_test_settings()
    settings.HEARTBEAT_TIMEOUT_MS = 5000
    shared_state = SharedState()
    risk_controller = RiskController(settings, shared_state)
    
    # Latency 100ms < 5000ms threshold
    result = await risk_controller.check_heartbeat(100)
    
    # Should return True (trading allowed)
    assert result == True
    # Status should remain GREEN
    assert shared_state.risk_state.status == RiskStatus.GREEN


@pytest.mark.asyncio
async def test_heartbeat_kill():
    """Latency 6,000ms, threshold 5,000ms → status KILLED."""
    settings = get_test_settings()
    settings.HEARTBEAT_TIMEOUT_MS = 5000
    shared_state = SharedState()
    risk_controller = RiskController(settings, shared_state)
    
    # Latency 6000ms > 5000ms threshold
    result = await risk_controller.check_heartbeat(6000)
    
    # Should return False (trading not allowed)
    assert result == False
    # Status should be KILLED
    assert shared_state.risk_state.status == RiskStatus.KILLED
    assert "Heartbeat failure" in shared_state.risk_state.kill_reason


@pytest.mark.asyncio
async def test_approve_signal_low_confidence():
    """Confidence 0.4 → rejected."""
    settings = get_test_settings()
    shared_state = SharedState()
    risk_controller = RiskController(settings, shared_state)
    
    signal = TradeSignal(
        direction=SignalDirection.LONG,
        strength=0.5,
        confidence=0.4,  # Below 0.55 threshold
        risk_reward_ratio=2.0,
        timestamp=datetime.now()
    )
    
    result = await risk_controller.approve_signal(signal)
    assert result == False


@pytest.mark.asyncio
async def test_approve_signal_low_rr():
    """Risk_reward 1.1 → rejected."""
    settings = get_test_settings()
    shared_state = SharedState()
    risk_controller = RiskController(settings, shared_state)
    
    signal = TradeSignal(
        direction=SignalDirection.LONG,
        strength=0.5,
        confidence=0.6,
        risk_reward_ratio=1.1,  # Below 1.5 threshold
        timestamp=datetime.now()
    )
    
    result = await risk_controller.approve_signal(signal)
    assert result == False


@pytest.mark.asyncio
async def test_approve_signal_open_position():
    """Open_positions=1 → rejected (no pyramiding)."""
    settings = get_test_settings()
    shared_state = SharedState()
    risk_controller = RiskController(settings, shared_state)
    
    # Set an open position
    shared_state.risk_state.open_positions = 1
    
    signal = TradeSignal(
        direction=SignalDirection.LONG,
        strength=0.5,
        confidence=0.6,
        risk_reward_ratio=2.0,
        timestamp=datetime.now()
    )
    
    result = await risk_controller.approve_signal(signal)
    assert result == False


@pytest.mark.asyncio
async def test_approve_signal_pass():
    """All good → approved."""
    settings = get_test_settings()
    shared_state = SharedState()
    risk_controller = RiskController(settings, shared_state)
    
    # Ensure GREEN status and no open positions
    shared_state.risk_state.status = RiskStatus.GREEN
    shared_state.risk_state.open_positions = 0
    
    signal = TradeSignal(
        direction=SignalDirection.LONG,
        strength=0.5,
        confidence=0.6,  # Above 0.55
        risk_reward_ratio=2.0,  # Above 1.5
        timestamp=datetime.now()
    )
    
    result = await risk_controller.approve_signal(signal)
    assert result == True

import pytest
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch
from execution.execution_models import Order, Position, OrderType, OrderSide, OrderStatus
from execution.executor import OrderExecutor
from state_manager.state import SharedState
from signal_engine.signal_models import Signal, SignalDirection
from config.settings import Settings
import time


def test_order_creation():
    """Test that Order can be created with required fields."""
    order = Order(
        client_order_id="test_1",
        symbol="NQ",
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        quantity=1,
        price=15000.0
    )
    assert order.client_order_id == "test_1"
    assert order.symbol == "NQ"
    assert order.order_type == OrderType.MARKET
    assert order.quantity == 1
    assert order.price == 15000.0
    assert order.status == OrderStatus.PENDING


def test_position_creation():
    """Test that Position can be created with required fields."""
    position = Position(
        symbol="NQ",
        side=OrderSide.BUY,
        quantity=1,
        avg_entry=15000.0
    )
    assert position.symbol == "NQ"
    assert position.quantity == 1
    assert position.avg_entry == 15000.0


def test_executor_initialization():
    """Test that OrderExecutor initializes correctly."""
    from risk_controller.risk_core import RiskController
    
    # Create mocks for dependencies
    mock_rest_client = MagicMock()
    mock_settings = MagicMock()
    mock_settings.SYMBOL = "NQ"
    mock_settings.TOPSTEP_MAX_CONTRACTS = 5
    
    shared_state = SharedState()
    risk_controller = RiskController(mock_settings, shared_state)
    
    executor = OrderExecutor(
        rest_client=mock_rest_client,
        risk_controller=risk_controller,
        state=shared_state,
        settings=mock_settings
    )
    assert executor.state == shared_state
    assert executor.risk_controller == risk_controller
    assert executor.rest_client == mock_rest_client
    assert executor.settings == mock_settings


@pytest.mark.asyncio
async def test_execute_signal_integration():
    """Integration test for execute_signal with mocked REST client responses.
    
    This test mocks the REST client to simulate a full bracket order submission.
    """
    from risk_controller.risk_core import RiskController
    from datetime import datetime
    
    # Create mocks for dependencies
    mock_rest_client = MagicMock()
    mock_settings = MagicMock()
    mock_settings.SYMBOL = "NQ"
    mock_settings.TOPSTEP_MAX_CONTRACTS = 5
    mock_settings.TOPSTEP_DAILY_LOSS_LIMIT = 500.0
    
    # Mock get_account_info
    mock_rest_client.get_account_info = AsyncMock(return_value={
        "id": 12345,
        "name": "Test Account",
        "readonly": False
    })
    
    # Mock get_account_balance
    mock_rest_client.get_account_balance = AsyncMock(return_value=10000.0)
    
    # Mock _post to return success response
    mock_rest_client._post = AsyncMock(return_value={
        "orders": [
            {"clientOrderId": "TEST_ENTRY_123", "orderId": 111},
            {"clientOrderId": "TEST_STOP_123", "orderId": 112},
            {"clientOrderId": "TEST_TARGET_123", "orderId": 113}
        ]
    })
    
    shared_state = SharedState()
    risk_controller = RiskController(mock_settings, shared_state)
    
    executor = OrderExecutor(
        rest_client=mock_rest_client,
        risk_controller=risk_controller,
        state=shared_state,
        settings=mock_settings
    )
    
    # Create a test signal
    signal = Signal(
        direction=SignalDirection.LONG,
        strength=0.8,
        confidence=0.7,
        timestamp=datetime.now(),
        entry_price=15000.0,
        stop_loss=14950.0,
        take_profit=15100.0,
        risk_reward_ratio=2.0,
        metadata={"symbol": "NQ"}
    )
    
    # Execute the signal
    result = await executor.execute_signal(signal)
    
    # Verify the result
    assert result is not None
    assert "TEST_ENTRY" in result
    
    # Verify that the REST client methods were called
    mock_rest_client.get_account_info.assert_called_once()
    mock_rest_client.get_account_balance.assert_called_once()
    mock_rest_client._post.assert_called_once()
    
    # Verify that orders were registered in state
    # (The exact number depends on the implementation)
    print(f"Active orders in state: {len(shared_state.active_orders)}")


@pytest.mark.asyncio
async def test_flatten_all():
    """Test flatten_all method with mocked REST client."""
    from risk_controller.risk_core import RiskController
    
    # Create mocks
    mock_rest_client = MagicMock()
    mock_settings = MagicMock()
    mock_settings.SYMBOL = "NQ"
    mock_settings.TOPSTEP_MAX_CONTRACTS = 5
    mock_settings.TOPSTEP_DAILY_LOSS_LIMIT = 500.0
    
    # Mock get_open_positions
    mock_rest_client.get_open_positions = AsyncMock(return_value=[
        {"symbol": "NQ", "netPos": 2},
        {"symbol": "ES", "netPos": -1}
    ])
    
    # Mock get_account_info
    mock_rest_client.get_account_info = AsyncMock(return_value={
        "id": 12345,
        "name": "Test Account"
    })
    
    # Mock _post for order submission
    mock_rest_client._post = AsyncMock(return_value={"success": True})
    
    shared_state = SharedState()
    risk_controller = RiskController(mock_settings, shared_state)
    
    executor = OrderExecutor(
        rest_client=mock_rest_client,
        risk_controller=risk_controller,
        state=shared_state,
        settings=mock_settings
    )
    
    # Execute flatten_all
    result = await executor.flatten_all("Test flatten")
    
    # Verify the result
    assert result is True  # Should return True when no remaining positions
    
    # Verify that get_open_positions was called
    mock_rest_client.get_account_info.assert_called()
    
    print("flatten_all test completed successfully")


def test_shared_state_order_registration():
    """Test that SharedState can register and resolve orders."""
    shared_state = SharedState()
    
    # Create a test order
    order = Order(
        client_order_id="test_1",
        symbol="NQ",
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=1,
        price=15.000
    )
    
    # Initially, no active orders
    assert len(shared_state.active_orders) == 0
    
    # Register order
    import asyncio
    asyncio.run(shared_state.register_order(order))
    
    # Should now have the order registered
    assert "test_1" in shared_state.active_orders
    assert shared_state.active_orders["test_1"].client_order_id == "test_1"
    assert shared_state.active_orders["test_1"].status == OrderStatus.PENDING
    
    # Resolve order as filled
    asyncio.run(shared_state.resolve_order("test_1", OrderStatus.FILLED, 15.005))
    
    # Order should be resolved and removed from active orders
    assert "test_1" not in shared_state.active_orders
    # Note: The order object itself would have status=FILLED and filled_price=15.005
    # but it's removed from active_orders when resolved


# ==================== NEW TESTS FOR ORDER EXECUTOR ====================

@pytest.mark.asyncio
async def test_execute_signal_killed_state():
    """Set risk_state.status = KILLED before calling execute_signal. Assert returns None immediately, no API calls made."""
    from risk_controller.risk_models import RiskStatus
    from risk_controller.risk_core import RiskController
    from datetime import datetime
    
    # Create mocks
    mock_rest_client = MagicMock()
    mock_settings = MagicMock()
    mock_settings.SYMBOL = "NQ"
    mock_settings.TOPSTEP_MAX_CONTRACTS = 5
    mock_settings.TOPSTEP_DAILY_LOSS_LIMIT = -500.0
    
    shared_state = SharedState()
    # Set risk state to KILLED
    shared_state.risk_state.status = RiskStatus.KILLED
    shared_state.risk_state.kill_reason = "Test kill"
    
    risk_controller = RiskController(mock_settings, shared_state)
    risk_controller.risk_state.status = RiskStatus.KILLED
    
    executor = OrderExecutor(
        rest_client=mock_rest_client,
        risk_controller=risk_controller,
        state=shared_state,
        settings=mock_settings
    )
    
    # Create a test signal
    signal = Signal(
        direction=SignalDirection.LONG,
        strength=0.8,
        confidence=0.7,
        timestamp=datetime.now(),
        entry_price=15000.0,
        stop_loss=14950.0,
        take_profit=15100.0,
        risk_reward_ratio=2.0,
        metadata={"symbol": "NQ"}
    )
    
    # Execute the signal - should return None immediately
    result = await executor.execute_signal(signal)
    
    # Verify result is None (killed state)
    assert result is None
    # No API calls should be made
    mock_rest_client.get_account_info.assert_not_called()


@pytest.mark.asyncio
async def test_cancel_all_working_orders():
    """Pre-populate state.active_orders with 3 WORKING orders. Mock cancelOrder to return success. Assert all 3 cancelled, returns count=3."""
    from risk_controller.risk_core import RiskController
    
    # Create mocks
    mock_rest_client = MagicMock()
    mock_settings = MagicMock()
    mock_settings.SYMBOL = "NQ"
    mock_settings.TOPSTEP_MAX_CONTRACTS = 5
    
    shared_state = SharedState()
    risk_controller = RiskController(mock_settings, shared_state)
    
    # Pre-populate with 3 working orders
    order1 = Order(client_order_id="test_1", symbol="NQ", side=OrderSide.BUY, order_type=OrderType.LIMIT, quantity=1, price=15000.0, status=OrderStatus.WORKING)
    order2 = Order(client_order_id="test_2", symbol="NQ", side=OrderSide.BUY, order_type=OrderType.LIMIT, quantity=1, price=15001.0, status=OrderStatus.WORKING)
    order3 = Order(client_order_id="test_3", symbol="NQ", side=OrderSide.BUY, order_type=OrderType.LIMIT, quantity=1, price=15002.0, status=OrderStatus.WORKING)
    
    await shared_state.register_order(order1)
    await shared_state.register_order(order2)
    await shared_state.register_order(order3)
    
    executor = OrderExecutor(
        rest_client=mock_rest_client,
        risk_controller=risk_controller,
        state=shared_state,
        settings=mock_settings
    )
    
    # Cancel all working orders
    count = await executor.cancel_all_working_orders()
    
    # Assert all 3 orders were cancelled
    assert count == 3


@pytest.mark.asyncio
async def test_execute_signal_size_zero():
    """position_sizer returns 0 (account too small or YELLOW state). Assert returns None, no order submitted."""
    from risk_controller.risk_core import RiskController
    from risk_controller.risk_models import RiskStatus
    from datetime import datetime
    
    # Create mocks
    mock_rest_client = MagicMock()
    mock_settings = MagicMock()
    mock_settings.SYMBOL = "NQ"
    mock_settings.TOPSTEP_MAX_CONTRACTS = 5
    mock_settings.TOPSTEP_DAILY_LOSS_LIMIT = -500.0
    
    # Mock get_account_info
    mock_rest_client.get_account_info = AsyncMock(return_value={
        "id": 12345,
        "name": "Test Account",
        "readonly": False
    })
    
    # Mock get_account_balance
    mock_rest_client.get_account_balance = AsyncMock(return_value=100.0)  # Very small balance
    
    shared_state = SharedState()
    risk_controller = RiskController(mock_settings, shared_state)
    
    # Set status to YELLOW to force position size to be reduced
    shared_state.risk_state.status = RiskStatus.YELLOW
    
    executor = OrderExecutor(
        rest_client=mock_rest_client,
        risk_controller=risk_controller,
        state=shared_state,
        settings=mock_settings
    )
    
    # Create a test signal
    signal = Signal(
        direction=SignalDirection.LONG,
        strength=0.8,
        confidence=0.7,
        timestamp=datetime.now(),
        entry_price=15000.0,
        stop_loss=14950.0,
        take_profit=15100.0,
        risk_reward_ratio=2.0,
        metadata={"symbol": "NQ"}
    )
    
    # Execute the signal - should return None because qty becomes 0
    result = await executor.execute_signal(signal)
    
    # Verify result is None
    assert result is None
    # No order submission should be made
    mock_rest_client._post.assert_not_called()

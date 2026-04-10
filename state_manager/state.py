import asyncio
from typing import Dict, Optional, List
from dataclasses import dataclass
from loguru import logger
from collections import deque

from execution.execution_models import Order, Position, OrderStatus
from risk_controller.risk_models import RiskState
from signal_engine.signal_models import TradeSignal
from data_ingestion.data_models import MarketDepthSnapshot, TimeAndSalesTick


class SharedState:
    """Singleton shared state for the trading bot with asyncio.Lock protection."""
    
    _instance: Optional['SharedState'] = None
    _lock: asyncio.Lock = asyncio.Lock()
    
    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance
    
    def __init__(self):
        # Only initialize once
        if not hasattr(self, '_initialized'):
            self._initialized = True
            self.current_position: Optional[Position] = None
            self.risk_state: RiskState = RiskState()
            self.latest_depth: Optional[MarketDepthSnapshot] = None
            self.market_depth: Optional[MarketDepthSnapshot] = None
            self.latest_signal: Optional[TradeSignal] = None
            self.session_start_pnl: float = 0.0
            self.active_orders: Dict[str, Order] = {}
            self.time_and_sales: deque = deque(maxlen=1000)  # Keep last 1000 ticks
    
    async def update_position(self, pos: Optional[Position]) -> None:
        """Update current position."""
        async with self._lock:
            self.current_position = pos
            logger.debug(f"Updated position: {pos}")
    
    async def update_risk(self, state: RiskState) -> None:
        """Update risk state."""
        async with self._lock:
            self.risk_state = state
            logger.debug(f"Updated risk state: {state.status}")
    
    async def update_signal(self, signal: TradeSignal) -> None:
        """Update latest signal."""
        async with self._lock:
            self.latest_signal = signal
            logger.debug(f"Updated signal: {signal.direction} {signal.entry_price}")
    
    async def register_order(self, order: Order) -> None:
        """Register an active order."""
        async with self._lock:
            self.active_orders[order.client_order_id] = order
            logger.debug(f"Registered order: {order.client_order_id}")
    
    async def update_market_depth(self, snapshot: MarketDepthSnapshot) -> None:
        """Update market depth snapshot."""
        async with self._lock:
            self.market_depth = snapshot
            self.latest_depth = snapshot  # For backward compatibility
            logger.debug(f"Updated market depth for {snapshot.symbol}")

    async def add_time_and_sale(self, tick: TimeAndSalesTick) -> None:
        """Add a time and sale tick to the history."""
        async with self._lock:
            self.time_and_sales.append(tick)
            logger.debug(f"Added time and sale tick: {tick.price} @ {tick.timestamp}")

    def get_market_depth(self) -> Optional[MarketDepthSnapshot]:
        """Get the latest market depth snapshot."""
        return self.market_depth

    def get_recent_ticks(self, count: int = 100) -> List[TimeAndSalesTick]:
        """Get recent time and sales ticks."""
        # Note: This method is synchronous but accesses shared state.
        # For simplicity in this context, we're accessing the deque directly.
        # In a high-frequency trading system, you might want to make this async
        # or use a different synchronization mechanism.
        return list(self.time_and_sales)[-count:]

    async def resolve_order(self, client_order_id: str, status: OrderStatus, filled_price: float) -> None:
        """Resolve an order with status and filled price."""
        async with self._lock:
            if client_order_id in self.active_orders:
                order = self.active_orders[client_order_id]
                order.status = status
                order.filled_price = filled_price
                # Remove from active orders if filled or cancelled
                if status in (OrderStatus.FILLED, OrderStatus.CANCELLED, OrderStatus.REJECTED):
                    del self.active_orders[client_order_id]
                logger.debug(f"Resolved order {client_order_id}: {status} @ {filled_price}")

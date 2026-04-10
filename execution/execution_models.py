from dataclasses import dataclass, field
from enum import Enum
from typing import Optional
import time
from loguru import logger


class OrderSide(Enum):
    BUY = "Buy"
    SELL = "Sell"


class OrderType(Enum):
    LIMIT = "Limit"
    MARKET = "Market"
    STOP = "Stop"
    STOP_LIMIT = "StopLimit"


class OrderStatus(Enum):
    PENDING = "PENDING"
    WORKING = "WORKING"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"


@dataclass
class Fill:
    """Represents a trade fill."""
    fill_id: str
    order_id: str
    symbol: str
    side: OrderSide
    quantity: int
    price: float
    commission: float = 0.0
    timestamp: float = field(default_factory=time.time)


@dataclass
class Order:
    client_order_id: str
    symbol: str
    side: OrderSide
    order_type: OrderType
    quantity: int
    price: Optional[float] = None
    stop_price: Optional[float] = None
    status: OrderStatus = OrderStatus.PENDING
    filled_price: Optional[float] = None
    timestamp: float = field(default_factory=time.time)
    fills: list[Fill] = field(default_factory=list)


@dataclass
class Position:
    symbol: str
    side: OrderSide
    quantity: int
    avg_entry: float
    unrealized_pnl: float = 0.0

    def update_pnl(self, current_price: float, tick_value: float, tick_size: float) -> None:
        """Update unrealized P&L based on current price."""
        if self.quantity == 0:
            self.unrealized_pnl = 0.0
            return
            
        # Calculate price difference in ticks
        price_diff = current_price - self.avg_entry
        if self.side == OrderSide.SELL:
            price_diff = -price_diff  # Invert for short positions
            
        # Convert to dollars and calculate P&L
        ticks = price_diff / tick_size
        self.unrealized_pnl = ticks * tick_value * self.quantity

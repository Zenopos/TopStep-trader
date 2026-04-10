from collections import deque
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass, field
import time
from loguru import logger
from .data_models import MarketDepthSnapshot, TimeAndSalesTick


@dataclass
class OrderBookLevel:
    """Represents a single level in the order book."""
    price: float
    size: int
    
    def __post_init__(self):
        self.price = float(self.price)
        self.size = int(self.size)


class OrderBook:
    """Manages the order book for a symbol."""
    
    def __init__(self, symbol: str, max_depth: int = 10):
        self.symbol = symbol
        self.max_depth = max_depth
        # Using deque for efficient operations on both ends
        self.bids: Dict[float, int] = {}  # price -> size
        self.asks: Dict[float, int] = {}  # price -> size
        self.last_update_time: float = field(default_factory=time.time)
        self.sequence_number: int = 0
        logger.debug(f"Initialized order book for {symbol}")
    
    def update_bid(self, price: float, size: int) -> None:
        """Update a bid level."""
        price = float(price)
        size = int(size)
        
        if size <= 0:
            # Remove level if size is zero or negative
            self.bids.pop(price, None)
        else:
            self.bids[price] = size
        
        self.last_update_time = time.time()
        logger.debug(f"Updated bid {self.symbol} {price} @ {size}")
    
    def update_ask(self, price: float, size: int) -> None:
        """Update an ask level."""
        price = float(price)
        size = int(size)
        
        if size <= 0:
            # Remove level if size is zero or negative
            self.asks.pop(price, None)
        else:
            self.asks[price] = size
        
        self.last_update_time = time.time()
        logger.debug(f"Updated ask {self.symbol} {price} @ {size}")
    
    def get_snapshot(self) -> MarketDepthSnapshot:
        """Get a snapshot of the current order book."""
        # Convert to sorted lists
        bid_levels = sorted(
            [(price, size) for price, size in self.bids.items()],
            key=lambda x: x[0],
            reverse=True  # Bids descending (highest first)
        )[:self.max_depth]
        
        ask_levels = sorted(
            [(price, size) for price, size in self.asks.items()],
            key=lambda x: x[0]  # Asks ascending (lowest first)
        )[:self.max_depth]
        
        snapshot = MarketDepthSnapshot(
            symbol=self.symbol,
            timestamp=self.last_update_time,
            bids=bid_levels,
            asks=ask_levels
        )
        
        return snapshot
    
    def get_best_bid(self) -> Optional[Tuple[float, int]]:
        """Get the best bid price and size."""
        if not self.bids:
            return None
        best_price = max(self.bids.keys())
        return best_price, self.bids[best_price]
    
    def get_best_ask(self) -> Optional[Tuple[float, int]]:
        """Get the best ask price and size."""
        if not self.asks:
            return None
        best_price = min(self.asks.keys())
        return best_price, self.asks[best_price]
    
    def get_spread(self) -> Optional[float]:
        """Get the bid-ask spread."""
        best_bid = self.get_best_bid()
        best_ask = self.get_best_ask()
        
        if best_bid is None or best_ask is None:
            return None
            
        return best_ask[0] - best_bid[0]
    
    def get_mid_price(self) -> Optional[float]:
        """Get the mid price."""
        best_bid = self.get_best_bid()
        best_ask = self.get_best_ask()
        
        if best_bid is None or best_ask is None:
            return None
            
        return (best_bid[0] + best_ask[0]) / 2
    
    def clear(self) -> None:
        """Clear the order book."""
        self.bids.clear()
        self.asks.clear()
        self.last_update_time = time.time()
        logger.debug(f"Cleared order book for {self.symbol}")
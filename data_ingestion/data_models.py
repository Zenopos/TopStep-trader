from dataclasses import dataclass
from typing import List, Tuple, Literal
from loguru import logger


@dataclass(slots=True)
class MarketDepthSnapshot:
    timestamp: float
    symbol: str
    bids: List[Tuple[float, int]]
    asks: List[Tuple[float, int]]
    imbalance_ratio: float = 0.0

    def compute_imbalance(self) -> float:
        """Calculate order book imbalance ratio.
        
        Formula: (sum of top-5 bid sizes - sum of top-5 ask sizes) /
                 (sum of top-5 bid sizes + sum of top-5 ask sizes)
        
        Returns:
            float: Imbalance ratio between -1 and 1, or 0.0 if denominator is zero
        """
        # Take top 5 levels (or fewer if not available)
        bid_sizes = [size for _, size in self.bids[:5]]
        ask_sizes = [size for _, size in self.asks[:5]]
        
        total_bid_size = sum(bid_sizes)
        total_ask_size = sum(ask_sizes)
        
        denominator = total_bid_size + total_ask_size
        if denominator == 0:
            return 0.0
            
        imbalance = (total_bid_size - total_ask_size) / denominator
        self.imbalance_ratio = imbalance
        return imbalance


@dataclass(slots=True)
class TimeAndSalesTick:
    timestamp: float
    price: float
    size: int
    aggressor: Literal["BUY", "SELL"]
    exchange: str
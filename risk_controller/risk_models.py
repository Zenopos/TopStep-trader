from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, Any


class RiskStatus(Enum):
    GREEN = 0
    YELLOW = 1
    RED = 2
    KILLED = 3


@dataclass
class RiskState:
    status: RiskStatus = RiskStatus.GREEN
    daily_pnl: float = 0.0
    open_positions: int = 0
    current_contracts: int = 0
    last_heartbeat_ms: float = 0.0
    kill_reason: str = ""
    positions: Dict[str, Any] = field(default_factory=dict)
    max_contracts: int = 5

    @property
    def is_tradeable(self) -> bool:
        return self.status in (RiskStatus.GREEN, RiskStatus.YELLOW)
    
    @property
    def is_trading_allowed(self) -> bool:
        return self.is_tradeable
        
    def can_open_position(self, symbol: str, quantity: int) -> bool:
        """Check if a position can be opened based on risk limits."""
        if not self.is_tradeable:
            return False
        if quantity > self.max_contracts:
            return False
        return True
        
    def update_daily_pnl(self, pnl: float) -> None:
        """Update daily P&L."""
        self.daily_pnl = pnl
        
    def update_trade_result(self, pnl: float) -> None:
        """Update trade result statistics."""
        # In a real implementation, this would update more statistics
        self.daily_pnl += pnl

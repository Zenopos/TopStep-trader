from typing import Dict, Any
from loguru import logger
from .risk_models import RiskState
from config.constants import CONTRACT_SPECS


class PositionSizer:
    """Calculates appropriate position sizes based on risk parameters."""
    
    def __init__(self, risk_state: RiskState):
        self.risk_state = risk_state
        
    def calculate_position_size(self, symbol: str, risk_amount: float, 
                              stop_loss_ticks: int) -> int:
        """Calculate position size based on risk amount and stop loss.
        
        Args:
            symbol: Trading symbol (e.g., 'NQ', 'ES')
            risk_amount: Dollar amount to risk on the trade
            stop_loss_ticks: Stop loss distance in ticks
            
        Returns:
            Number of contracts to trade
        """
        if symbol not in CONTRACT_SPECS:
            logger.warning(f"Unknown symbol {symbol}, defaulting to 1 contract")
            return 1
            
        contract_spec = CONTRACT_SPECS[symbol]
        tick_size = contract_spec['tick_size']
        tick_value = contract_spec['tick_value']
        
        # Calculate dollar value of stop loss
        stop_loss_dollars = stop_loss_ticks * tick_size * tick_value
        
        if stop_loss_dollars <= 0:
            logger.warning("Invalid stop loss dollars, defaulting to 1 contract")
            return 1
            
        # Calculate raw position size
        raw_size = risk_amount / stop_loss_dollars
        
        # Round down to nearest whole contract
        position_size = int(raw_size)
        
        # Apply maximum contract limit
        max_contracts = self.risk_state.max_contracts
        position_size = min(position_size, max_contracts)
        
        # Ensure minimum of 0 contracts
        position_size = max(0, position_size)
        
        logger.debug(f"Calculated position size: {position_size} contracts for {symbol} "
                    f"with risk ${risk_amount} and SL {stop_loss_ticks} ticks")
        
        return position_size
    
    def calculate_risk_amount(self, symbol: str, quantity: int, 
                            stop_loss_ticks: int) -> float:
        """Calculate the dollar amount risked for a given position size.
        
        Args:
            symbol: Trading symbol (e.g., 'NQ', 'ES')
            quantity: Number of contracts
            stop_loss_ticks: Stop loss distance in ticks
            
        Returns:
            Dollar amount risked
        """
        if symbol not in CONTRACT_SPECS:
            logger.warning(f"Unknown symbol {symbol}, returning 0")
            return 0.0
            
        contract_spec = CONTRACT_SPECS[symbol]
        tick_size = contract_spec['tick_size']
        tick_value = contract_spec['tick_value']
        
        # Calculate dollar value of stop loss per contract
        stop_loss_dollars_per_contract = stop_loss_ticks * tick_size * tick_value
        
        # Total risk amount
        risk_amount = stop_loss_dollars_per_contract * quantity
        
        return risk_amount
    
    def get_max_risk_amount(self, symbol: str, stop_loss_ticks: int) -> float:
        """Get the maximum risk amount allowed based on position limits.
        
        Args:
            symbol: Trading symbol (e.g., 'NQ', 'ES')
            stop_loss_ticks: Stop loss distance in ticks
            
        Returns:
            Maximum dollar amount that can be risked
        """
        max_contracts = self.risk_state.max_contracts
        return self.calculate_risk_amount(symbol, max_contracts, stop_loss_ticks)


def calculate_position_size(
    account_balance: float,
    risk_percent: float,      # e.g. 0.01 for 1%
    stop_distance_ticks: int,
    tick_value: float,
    max_contracts: int
) -> int:
    """Kelly-inspired fixed fractional position sizing.
    
    Args:
        account_balance: Total account balance
        risk_percent: Percentage of account to risk (e.g. 0.01 for 1%)
        stop_distance_ticks: Stop loss distance in ticks
        tick_value: Dollar value per tick
        max_contracts: Maximum number of contracts allowed
        
    Returns:
        Number of contracts to trade
    """
    import math
    
    dollar_risk = account_balance * risk_percent
    risk_per_contract = stop_distance_ticks * tick_value
    
    if risk_per_contract <= 0:
        logger.warning("Invalid risk per contract, returning 0")
        return 0
        
    raw_size = dollar_risk / risk_per_contract
    raw_size = math.floor(raw_size)
    
    return min(raw_size, max_contracts)

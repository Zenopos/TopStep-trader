from typing import Dict, Any, Optional
from dataclasses import dataclass
from loguru import logger
from uuid import uuid4

from .execution_models import Order, OrderType, OrderStatus, OrderSide, Position
from config.constants import CONTRACT_SPECS


def _round_to_tick(price: float, symbol: str) -> float:
    """Round price to the correct tick size for the given symbol.
    
    Args:
        price: The price to round.
        symbol: The symbol to look up the tick size for.
        
    Returns:
        The price rounded to the correct tick size.
    """
    tick_size = CONTRACT_SPECS.get(symbol, {}).get("tick_size", 0.25)
    return round(price / tick_size) * tick_size


def build_market_order(
    account_id: int, symbol: str, side: OrderSide, qty: int
) -> dict:
    """Return Tradovate-formatted placeOrder payload for a market order.
    
    Args:
        account_id: The account ID.
        symbol: The symbol to trade.
        side: The order side (BUY or SELL).
        qty: The quantity to trade.
        
    Returns:
        A dictionary containing the Tradovate-formatted market order payload.
    """
    return {
        "accountId": account_id,
        "symbol": symbol,
        "side": side.value,
        "quantity": qty,
        "orderType": OrderType.MARKET.value,
        "clientOrderId": generate_client_order_id()
    }


def build_limit_order(
    account_id: int, symbol: str, side: OrderSide,
    qty: int, price: float
) -> dict:
    """Return Tradovate-formatted placeOrder payload for a limit order.
    
    Args:
        account_id: The account ID.
        symbol: The symbol to trade.
        side: The order side (BUY or SELL).
        qty: The quantity to trade.
        price: The limit price.
        
    Returns:
        A dictionary containing the Tradovate-formatted limit order payload.
    """
    rounded_price = _round_to_tick(price, symbol)
    return {
        "accountId": account_id,
        "symbol": symbol,
        "side": side.value,
        "quantity": qty,
        "orderType": OrderType.LIMIT.value,
        "price": rounded_price,
        "clientOrderId": generate_client_order_id()
    }


def build_bracket_order(
    account_id: int,
    symbol: str,
    side: OrderSide,
    qty: int,
    entry_price: float,       # None = market entry
    stop_price: float,
    target_price: float,
    entry_type: OrderType = OrderType.LIMIT
) -> dict:
    """Build a Tradovate OSO bracket: entry + stop loss + profit target.
    
    Args:
        account_id: The account ID.
        symbol: The symbol to trade.
        side: The order side (BUY or SELL).
        qty: The quantity to trade.
        entry_price: The entry price (None for market entry).
        stop_price: The stop loss price.
        target_price: The profit target price.
        entry_type: The type of entry order (default: LIMIT).
        
    Returns:
        A dictionary containing the Tradovate-formatted OSO bracket payload.
        
    Raises:
        ValueError: If the bracket validation fails.
    """
    # Validate the bracket
    validate_bracket(entry_price, stop_price, target_price, side)
    
    # Determine the opposite side for the brackets
    opposite_side = OrderSide.SELL if side == OrderSide.BUY else OrderSide.BUY
    
    # Build the entry order
    if entry_type == OrderType.MARKET or entry_price is None:
        entry_order = build_market_order(account_id, symbol, side, qty)
    else:
        entry_order = build_limit_order(account_id, symbol, side, qty, entry_price)
    
    # Build the stop loss order (bracket1)
    stop_loss_order = {
        "accountId": account_id,
        "symbol": symbol,
        "side": opposite_side.value,
        "quantity": qty,
        "orderType": OrderType.STOP.value,
        "stopPrice": _round_to_tick(stop_price, symbol),
        "clientOrderId": generate_client_order_id()
    }
    
    # Build the profit target order (bracket2)
    profit_target_order = {
        "accountId": account_id,
        "symbol": symbol,
        "side": opposite_side.value,
        "quantity": qty,
        "orderType": OrderType.LIMIT.value,
        "price": _round_to_tick(target_price, symbol),
        "clientOrderId": generate_client_order_id()
    }
    
    return {
        "entryOrder": entry_order,
        "bracket1": stop_loss_order,
        "bracket2": profit_target_order
    }


def build_flatten_order(
    account_id: int, symbol: str,
    current_position: Position
) -> dict:
    """Build a market order to close the current position entirely.
    
    Args:
        account_id: The account ID.
        symbol: The symbol to trade.
        current_position: The current position to close.
        
    Returns:
        A dictionary containing the Tradovate-formatted market order payload.
    """
    # Determine the opposite side to close the position
    flatten_side = OrderSide.SELL if current_position.side == OrderSide.BUY else OrderSide.BUY
    
    return build_market_order(account_id, symbol, flatten_side, current_position.quantity)


def generate_client_order_id(prefix: str = "TSB") -> str:
    """Generate a unique client order ID.
    
    Args:
        prefix: The prefix for the client order ID (default: TSB).
        
    Returns:
        A unique client order ID.
    """
    return f"{prefix}_{uuid4().hex[:12].upper()}"


def validate_bracket(entry: Optional[float], stop: float, target: float, side: OrderSide) -> bool:
    """Validate a bracket order.
    
    Args:
        entry: The entry price (None for market entry).
        stop: The stop loss price.
        target: The profit target price.
        side: The order side (BUY or SELL).
        
    Returns:
        True if the bracket is valid.
        
    Raises:
        ValueError: If the stop is on the wrong side of the entry,
                    if the target is on the wrong side of the entry,
                    or if the risk/reward ratio is less than 1.0.
    """
    if entry is not None:
        if side == OrderSide.BUY:
            # For BUY orders: stop must be below entry, target must be above entry
            if stop >= entry:
                raise ValueError(f"Stop price ({stop}) must be below entry price ({entry}) for BUY orders.")
            if target <= entry:
                raise ValueError(f"Target price ({target}) must be above entry price ({entry}) for BUY orders.")
            
            # Check risk/reward ratio
            risk = entry - stop
            reward = target - entry
            if reward < risk:
                raise ValueError(f"Risk/reward ratio must be at least 1.0. Risk: {risk}, Reward: {reward}")
        else:
            # For SELL orders: stop must be above entry, target must be below entry
            if stop <= entry:
                raise ValueError(f"Stop price ({stop}) must be above entry price ({entry}) for SELL orders.")
            if target >= entry:
                raise ValueError(f"Target price ({target}) must be below entry price ({entry}) for SELL orders.")
            
            # Check risk/reward ratio
            risk = stop - entry
            reward = entry - target
            if reward < risk:
                raise ValueError(f"Risk/reward ratio must be at least 1.0. Risk: {risk}, Reward: {reward}")
    else:
        # For market entry, we only check stop and target
        if side == OrderSide.BUY:
            if target <= stop:
                raise ValueError(f"Target price ({target}) must be above stop price ({stop}) for BUY orders.")
        else:
            if target >= stop:
                raise ValueError(f"Target price ({target}) must be below stop price ({stop}) for SELL orders.")
    
    return True


class OrderBuilder:
    """Constructs various types of trading orders."""
    
    @staticmethod
    def market_order(symbol: str, quantity: int) -> Order:
        """Create a market order."""
        return Order(
            order_id=f"market_{symbol}_{int(logger._core.now().timestamp() * 1_000)}",
            symbol=symbol,
            order_type=OrderType.MARKET,
            quantity=quantity
        )
    
    @staticmethod
    def limit_order(symbol: str, quantity: int, price: float) -> Order:
        """Create a limit order."""
        return Order(
            order_id=f"limit_{symbol}_{int(logger._core.now().timestamp() * 1_000)}",
            symbol=symbol,
            order_type=OrderType.LIMIT,
            quantity=quantity,
            price=price
        )
    
    @staticmethod
    def stop_order(symbol: str, quantity: int, stop_price: float) -> Order:
        """Create a stop order."""
        return Order(
            order_id=f"stop_{symbol}_{int(logger._core.now().timestamp() * 1_000)}",
            symbol=symbol,
            order_type=OrderType.STOP,
            quantity=quantity,
            stop_price=stop_price
        )
    
    @staticmethod
    def stop_limit_order(symbol: str, quantity: int, price: float, stop_price: float) -> Order:
        """Create a stop-limit order."""
        return Order(
            order_id=f"stop_limit_{symbol}_{int(logger._core.now().timestamp() * 1_000)}",
            symbol=symbol,
            order_type=OrderType.STOP_LIMIT,
            quantity=quantity,
            price=price,
            stop_price=stop_price
        )
    
    @staticmethod
    def bracket_order(symbol: str, quantity: int, entry_price: float, 
                     stop_loss: float, take_profit: float) -> Dict[str, Order]:
        """Create a bracket order (entry + stop loss + take profit).
        
        Returns:
            Dict with keys: 'entry', 'stop_loss', 'take_profit'
        """
        # Determine order side
        is_buy = quantity > 0
        abs_quantity = abs(quantity)
        
        # Entry order (limit order at entry price)
        entry_order = OrderBuilder.limit_order(
            symbol, abs_quantity if is_buy else -abs_quantity, entry_price
        )
        
        # Stop loss order (stop order)
        stop_loss_order = OrderBuilder.stop_order(
            symbol, -abs_quantity if is_buy else abs_quantity, stop_loss
        )
        
        # Take profit order (limit order)
        take_profit_order = OrderBuilder.limit_order(
            symbol, -abs_quantity if is_buy else abs_quantity, take_profit
        )
        
        return {
            'entry': entry_order,
            'stop_loss': stop_loss_order,
            'take_profit': take_profit_order
        }
    
    @staticmethod
    def oco_order(symbol: str, quantity: int, price1: float, price2: float) -> Dict[str, Order]:
        """Create an OCO (One-Cancels-Other) order.
        
        Returns:
            Dict with keys: 'order1', 'order2'
        """
        # Determine order side
        is_buy = quantity > 0
        abs_quantity = abs(quantity)
        
        # Two limit orders at different prices
        order1 = OrderBuilder.limit_order(
            symbol, abs_quantity if is_buy else -abs_quantity, price1
        )
        
        order2 = OrderBuilder.limit_order(
            symbol, abs_quantity if is_buy else -abs_quantity, price2
        )
        
        return {
            'order1': order1,
            'order2': order2
        }

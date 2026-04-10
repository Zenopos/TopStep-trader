import asyncio
from typing import Dict, Any, Optional, Callable
from loguru import logger

from .execution_models import Order, Fill, Position, OrderType, OrderStatus, OrderSide
from .order_builder import build_bracket_order, build_flatten_order
from state_manager.state import SharedState
from risk_controller.risk_core import RiskController
from risk_controller.position_sizer import calculate_position_size as calc_pos_size
from data_ingestion.rest_client import TradovateRESTClient
from config.settings import Settings
from config.constants import CONTRACT_SPECS
from signal_engine.signal_models import TradeSignal


class OrderExecutor:
    """Final execution layer for the trading bot.
    
    This class receives approved TradeSignals, calls order_builder.py to construct orders,
    submits them via rest_client.py, and tracks order state via SharedState.
    The RiskController has already approved the signal before this class sees it.
    """
    
    def __init__(
        self,
        rest_client: TradovateRESTClient,
        risk_controller: RiskController,
        state: SharedState,
        settings: Settings
    ):
        self.rest_client = rest_client
        self.risk_controller = risk_controller
        self.state = state
        self.settings = settings
        
        # Re-entrancy protection
        self._execution_lock = asyncio.Lock()
        
        # Simulated order management (in reality, this would connect to Tradovate API)
        self.orders: Dict[str, Order] = {}
        self.fills: Dict[str, Fill] = {}
        self.positions: Dict[str, Position] = {}
        self.next_order_id = 1
        self.next_fill_id = 1

    async def execute_signal(self, signal: TradeSignal) -> Optional[str]:
        """Full execution pipeline for a TradeSignal.
        
        Returns client_order_id on success, None on failure.

        Steps:
        1. Re-validate with risk_controller.approve_signal(signal) — double check
        2. Get account_id from rest_client.get_account_info()
        3. Calculate qty via position_sizer.calculate_position_size(...)
           - account_balance from rest_client.get_account_balance()
           - risk_percent = 0.01 (1% per trade)
           - stop_distance_ticks = abs(signal.entry_price - signal.stop_price) / tick_size
        4. Validate qty > 0 after risk_controller.validate_position_size()
        5. Build bracket order via order_builder.build_bracket_order(...)
        6. Submit via rest_client._post("/order/placeOSO", payload)
        7. Register all resulting orders in state via state.register_order()
        8. Log fill details at INFO level
        9. Return client_order_id
        """
        async with self._execution_lock:
            # --- Guard: killed state ---
            if self.risk_controller.is_killed:
                logger.warning("execute_signal called but bot is in KILLED state. Aborting.")
                return None

            # --- Step 1: Re-validate with risk_controller ---
            if not await self.risk_controller.approve_signal(signal):
                logger.warning(f"Signal failed re-validation: {signal}")
                return None

            # --- Step 2: Get account_id ---
            try:
                account_info = await self.rest_client.get_account_info()
            except Exception as e:
                logger.error(f"Failed to get account info: {e}")
                return None

            account_id: Optional[int] = account_info.get("id")
            if not account_id:
                logger.error(f"Could not resolve account_id from account info response: {account_info}")
                return None

            if account_info.get("readonly", False):
                logger.critical("Account is marked readonly — cannot place orders.")
                await self.risk_controller._kill("Account is readonly")
                return None

            logger.debug(f"Executing signal on account_id={account_id} ({account_info.get('name')})")

            # Use symbol from signal metadata or fallback to settings
            symbol = signal.metadata.get("symbol", self.settings.SYMBOL)

            # --- Step 3: Calculate qty via position_sizer ---
            try:
                account_balance = await self.rest_client.get_account_balance()
            except Exception as e:
                logger.error(f"Failed to get account balance: {e}")
                return None

            # Handle optional price values
            entry_price = signal.entry_price
            stop_loss = signal.stop_loss
            take_profit = signal.take_profit
            
            if entry_price is None or stop_loss is None or take_profit is None:
                logger.error(f"Signal missing required price values: entry={entry_price}, stop={stop_loss}, target={take_profit}")
                return None

            risk_percent = 0.01  # 1% per trade
            tick_size = CONTRACT_SPECS.get(symbol, {}).get("tick_size", 0.25)
            stop_distance_ticks = int(abs(entry_price - stop_loss) / tick_size)
            tick_value = CONTRACT_SPECS.get(symbol, {}).get("tick_value", 0.0)
            
            if tick_value == 0:
                logger.error(f"Could not find tick_value for symbol: {symbol}")
                return None

            qty = calc_pos_size(
                account_balance=account_balance,
                risk_percent=risk_percent,
                stop_distance_ticks=stop_distance_ticks,
                tick_value=tick_value,
                max_contracts=self.settings.TOPSTEP_MAX_CONTRACTS
            )

            # --- Step 4: Validate qty > 0 ---
            qty = await self.risk_controller.validate_position_size(qty, signal)
            if qty <= 0:
                logger.warning(f"Quantity is 0 after validation. Signal: {signal}")
                return None

            # --- Step 5: Build bracket order ---
            # Handle direction - can be string or SignalDirection enum
            signal_direction = signal.direction
            if isinstance(signal_direction, str):
                is_long = signal_direction.lower() == "long"
            else:
                # It's an enum
                from signal_engine.signal_models import SignalDirection
                is_long = signal_direction == SignalDirection.LONG
                
            side = OrderSide.BUY if is_long else OrderSide.SELL
            
            try:
                bracket_payload = build_bracket_order(
                    account_id=account_id,
                    symbol=symbol,
                    side=side,
                    qty=qty,
                    entry_price=entry_price,
                    stop_price=stop_loss,
                    target_price=take_profit,
                    entry_type=OrderType.LIMIT if entry_price else OrderType.MARKET
                )
            except ValueError as e:
                logger.error(f"Failed to build bracket order: {e}")
                return None

            # --- Step 6: Submit via rest_client ---
            try:
                response = await self._submit_order(bracket_payload, "/order/placeOSO")
            except Exception as e:
                logger.error(f"Failed to submit bracket order: {e}")
                return None

            if not response:
                logger.error("Empty response from order submission")
                return None

            # --- Step 7: Register all resulting orders in state ---
            # Extract client_order_id from the response
            # The response format from Tradovate is typically a list of orders
            # We need to handle the response format properly
            
            # For now, we assume the entry order's clientOrderId is the primary one
            entry_client_order_id = bracket_payload.get("entryOrder", {}).get("clientOrderId")
            
            # Register entry order
            entry_order = Order(
                client_order_id=entry_client_order_id,
                symbol=symbol,
                side=side,
                order_type=OrderType.LIMIT if entry_price else OrderType.MARKET,
                quantity=qty,
                price=entry_price,
                status=OrderStatus.WORKING
            )
            await self.state.register_order(entry_order)

            # Also register bracket orders (stop loss and take profit)
            bracket1_client_order_id = bracket_payload.get("bracket1", {}).get("clientOrderId")
            bracket2_client_order_id = bracket_payload.get("bracket2", {}).get("clientOrderId")
            
            # Determine opposite side for brackets
            opposite_side = OrderSide.SELL if side == OrderSide.BUY else OrderSide.BUY
            
            if bracket1_client_order_id:
                stop_order = Order(
                    client_order_id=bracket1_client_order_id,
                    symbol=symbol,
                    side=opposite_side,
                    order_type=OrderType.STOP,
                    quantity=qty,
                    stop_price=stop_loss,
                    status=OrderStatus.WORKING
                )
                await self.state.register_order(stop_order)
                
            if bracket2_client_order_id:
                target_order = Order(
                    client_order_id=bracket2_client_order_id,
                    symbol=symbol,
                    side=opposite_side,
                    order_type=OrderType.LIMIT,
                    quantity=qty,
                    price=take_profit,
                    status=OrderStatus.WORKING
                )
                await self.state.register_order(target_order)

            # --- Step 8: Log fill details at INFO level ---
            logger.info(
                f"Bracket order submitted: symbol={symbol}, "
                f"side={side.value}, qty={qty}, "
                f"entry={entry_price:.2f}, "
                f"stop={stop_loss:.2f}, "
                f"target={take_profit:.2f}"
            )

            # --- Step 9: Return client_order_id ---
            return entry_client_order_id

    async def flatten_all(self, reason: str = "Manual flatten") -> bool:
        """Emergency flatten procedure.
        
        1. Get current open positions from rest_client.get_open_positions()
        2. For each open position: build and submit flatten order immediately
        3. Cancel all working orders via cancel_all_working_orders()
        4. Verify positions are zero after 3s delay
        5. Log outcome at WARNING level with reason
        6. Return True if all positions confirmed flat
        """
        logger.warning(f"flatten_all initiated: {reason}")
        
        try:
            # --- Step 1: Get open positions ---
            open_positions = await self.rest_client.get_open_positions()
        except Exception as e:
            logger.error(f"Failed to get open positions: {e}")
            return False
            
        if not open_positions:
            logger.info("No open positions to flatten")
            return True
            
        # --- Step 2: Build and submit flatten orders ---
        try:
            account_info = await self.rest_client.get_account_info()
        except Exception as e:
            logger.error(f"Failed to get account info: {e}")
            return False
            
        account_id = account_info.get("id")
        if not account_id:
            logger.error("Could not get account_id for flatten")
            return False
            
        flatten_tasks = []
        for pos in open_positions:
            symbol = pos.get("symbol")
            if not symbol:
                logger.warning(f"Skipping position with no symbol: {pos}")
                continue
                
            net_pos = pos.get("netPos", 0)
            
            if net_pos == 0:
                continue
                
            # Determine side based on net position
            side = OrderSide.BUY if net_pos < 0 else OrderSide.SELL
            qty = abs(net_pos)
            
            # Build flatten order payload - use order_builder's format (dict) instead of Position object
            # Since build_flatten_order expects Position but we can just pass the dict format
            from .order_builder import build_market_order
            flatten_payload = build_market_order(
                account_id=account_id,
                symbol=symbol,
                side=side,
                qty=qty
            )
            
            # Submit flatten order
            try:
                flatten_tasks.append(self._submit_order(flatten_payload, "/order/place"))
            except Exception as e:
                logger.error(f"Failed to submit flatten order for {symbol}: {e}")
                
        # Wait for all flatten orders to complete
        if flatten_tasks:
            await asyncio.gather(*flatten_tasks, return_exceptions=True)
            
        # --- Step 3: Cancel all working orders ---
        await self.cancel_all_working_orders()
        
        # --- Step 4: Verify positions are zero after 3s delay ---
        await asyncio.sleep(3)
        
        try:
            final_positions = await self.rest_client.get_open_positions()
            open_positions_after = [p for p in final_positions if p.get("netPos", 0) != 0]
        except Exception as e:
            logger.error(f"Failed to verify flatten: {e}")
            return False
            
        # --- Step 5: Log outcome ---
        if not open_positions_after:
            logger.warning(f"flatten_all completed successfully: {reason}")
            return True
        else:
            logger.warning(f"flatten_all completed with remaining positions: {[p.get('symbol') for p in open_positions_after]}")
            return False

    async def cancel_all_working_orders(self) -> int:
        """Cancel all orders with status WORKING in state.active_orders.
        
        Submit cancel requests concurrently using asyncio.gather().
        Return count of successfully cancelled orders.
        """
        # Get all active orders
        active_orders = list(self.state.active_orders.values())
        working_orders = [o for o in active_orders if o.status == OrderStatus.WORKING]
        
        if not working_orders:
            logger.debug("No working orders to cancel")
            return 0
            
        logger.info(f"Canceling {len(working_orders)} working orders")
        
        # Create cancel tasks
        cancel_tasks = []
        for order in working_orders:
            # In a real implementation, we'd call rest_client._post with cancel endpoint
            # For now, we simulate it
            try:
                # Simulate cancel request
                cancel_tasks.append(self._cancel_order(order.client_order_id))
            except Exception as e:
                logger.error(f"Failed to create cancel task for {order.client_order_id}: {e}")
                
        # Execute all cancels concurrently
        results = await asyncio.gather(*cancel_tasks, return_exceptions=True)
        
        # Count successful cancellations
        success_count = sum(1 for r in results if r is True)
        
        logger.info(f"Canceled {success_count}/{len(working_orders)} orders")
        
        return success_count

    async def _cancel_order(self, order_id: str) -> bool:
        """Internal method to cancel a single order."""
        # In a real implementation, this would call the Tradovate API
        # For now, we just simulate a successful cancel
        logger.debug(f"Canceling order: {order_id}")
        
        # Update order status in state if possible
        if order_id in self.state.active_orders:
            order = self.state.active_orders[order_id]
            order.status = OrderStatus.CANCELLED
            await self.state.resolve_order(order_id, OrderStatus.CANCELLED, 0.0)
            
        return True

    async def _submit_order(self, payload: dict, endpoint: str) -> dict:
        """Internal: submit order with retry logic.
        
        Max 2 retries on connection error only (never retry on 4xx).
        1s delay between retries.
        On final failure: log CRITICAL and return empty dict.
        """
        max_retries = 2
        retry_delay = 1
        
        for attempt in range(max_retries + 1):
            try:
                response = await self.rest_client._post(endpoint, payload)
                return response
            except Exception as e:
                error_str = str(e)
                
                # Check if it's a 4xx error (don't retry)
                if "400" in error_str or "401" in error_str or "403" in error_str or "404" in error_str:
                    logger.error(f"Order submission failed with 4xx error: {error_str}")
                    return {}
                
                # Check if it's a connection error (retry)
                if attempt < max_retries:
                    logger.warning(f"Order submission failed (attempt {attempt + 1}/{max_retries + 1}): {e}. Retrying in {retry_delay}s...")
                    await asyncio.sleep(retry_delay)
                else:
                    logger.critical(f"Order submission failed after {max_retries + 1} attempts: {e}")
                    return {}
                    
        return {}

    async def on_fill_event(self, filled_order: Order) -> None:
        """Callback registered with ws_client for fill events.
        
        1. Resolve order in state via state.resolve_order(...)
        2. Update position in state
        3. Recalculate and update unrealized PnL
        4. Check DLL via risk_controller.check_dll(updated_pnl)
        """
        # --- Step 1: Resolve order in state ---
        await self.state.resolve_order(
            filled_order.client_order_id,
            OrderStatus.FILLED,
            filled_order.filled_price or 0.0
        )
        
        # --- Step 2: Update position in state ---
        symbol = filled_order.symbol
        
        # Get current position or create new one
        if symbol not in self.positions:
            # Handle case where filled_order.side might be None
            order_side = filled_order.side if filled_order.side is not None else OrderSide.BUY
            self.positions[symbol] = Position(
                symbol=symbol,
                side=order_side,
                quantity=0,
                avg_entry=0.0
            )
            
        position = self.positions[symbol]
        
        # Get filled price - handle None case
        filled_price = filled_order.filled_price if filled_order.filled_price is not None else 0.0
        
        # Update position based on fill
        # If same side, add to position; if opposite side, reduce position
        if filled_order.side == position.side:
            # Add to position
            total_qty = position.quantity + filled_order.quantity
            if total_qty != 0:
                new_avg = ((position.avg_entry * position.quantity) + 
                          (filled_price * filled_order.quantity)) / total_qty
                position.avg_entry = new_avg
                position.quantity = total_qty
            else:
                position.avg_entry = 0.0
                position.quantity = 0
        else:
            # Reduce position
            position.quantity -= filled_order.quantity
            if position.quantity == 0:
                position.avg_entry = 0.0
                
        # Update state
        await self.state.update_position(position)
        
        # --- Step 3: Recalculate and update unrealized PnL ---
        # Get current market price (would come from data feed in real implementation)
        # For now, use filled price as approximation
        current_price = filled_price
        
        tick_size = CONTRACT_SPECS.get(symbol, {}).get("tick_size", 0.25)
        tick_value = CONTRACT_SPECS.get(symbol, {}).get("tick_value", 0.0)
        
        position.update_pnl(current_price, tick_value, tick_size)
        
        # Update state with PnL
        await self.state.update_position(position)
        
        # --- Step 4: Check DLL ---
        # Calculate updated PnL (simplified - would need to track properly in real implementation)
        updated_pnl = position.unrealized_pnl
        
        dll_ok = await self.risk_controller.check_dll(updated_pnl)
        
        if not dll_ok:
            logger.warning(f"DLL check failed after fill. PnL: {updated_pnl:.2f}")

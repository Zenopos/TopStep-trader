import asyncio
from typing import Dict, Any, Optional, Callable
from loguru import logger
from .risk_models import RiskState, RiskStatus
from state_manager.state import SharedState
from config.settings import Settings
from signal_engine.signal_models import TradeSignal
from comms.notifier import Notifier


class RiskController:
    """Hard firewall for risk management."""
    
    def __init__(self, settings: Settings, state: SharedState):
        self.settings = settings
        self.state = state
        self.risk_state = RiskState()
        self.running = False
        self.risk_task: Optional[asyncio.Task] = None
        self.risk_callbacks: list = []
        self.kill_event = asyncio.Event()
        self._notifier = Notifier()
        
    async def start(self) -> None:
        """Start the risk controller."""
        self.running = True
        self.risk_task = asyncio.create_task(self._risk_loop())
        await self._notifier.start()
        logger.info("Risk controller started")
        
    async def stop(self) -> None:
        """Stop the risk controller."""
        self.running = False
        if self.risk_task:
            self.risk_task.cancel()
            try:
                await self.risk_task
            except asyncio.CancelledError:
                pass
        await self._notifier.stop()
        logger.info("Risk controller stopped")
        
    def add_risk_callback(self, callback: Callable[[bool], None]) -> None:
        """Add a callback to be called when risk status changes."""
        self.risk_callbacks.append(callback)
        
    async def _risk_loop(self) -> None:
        """Main risk monitoring loop."""
        while self.running:
            try:
                # Update risk state from shared state
                await self._update_risk_state()
                
                # Check if trading is allowed
                trading_allowed = self.risk_state.is_trading_allowed
                
                # Notify callbacks of risk status change
                for callback in self.risk_callbacks:
                    try:
                        callback(trading_allowed)
                    except Exception as e:
                        logger.error(f"Error in risk callback: {e}")
                
                # Sleep to avoid excessive CPU usage
                await asyncio.sleep(0.5)
                
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in risk loop: {e}")
                await asyncio.sleep(1)
                
    async def _update_risk_state(self) -> None:
        """Update risk state from shared state."""
        # Update daily P&L from account info
        # In a real implementation, you'd calculate this from fills
        # For now, we'll use a placeholder
        account_balance = getattr(self.state, 'account_balance', 0.0)
        # This is simplified - real P&L calculation would be more complex
        
        # Update positions
        positions = getattr(self.state, 'positions', {})
        self.risk_state.positions = positions.copy()
        
        # Check for daily reset (simplified - in reality you'd check date change)
        # For now, we'll just assume it's handled elsewhere
        
    def can_open_position(self, symbol: str, quantity: int) -> bool:
        """Check if a position can be opened based on risk limits."""
        return self.risk_state.can_open_position(symbol, quantity)
    
    def can_close_position(self, symbol: str) -> bool:
        """Check if a position can be closed (always allowed for risk management)."""
        return True
    
    def update_daily_pnl(self, pnl: float) -> None:
        """Update daily P&L."""
        self.risk_state.update_daily_pnl(pnl)
        
    def update_trade_result(self, pnl: float) -> None:
        """Update trade result statistics."""
        self.risk_state.update_trade_result(pnl)
        
    def get_risk_state(self) -> RiskState:
        """Get current risk state."""
        return self.risk_state

    # -------------------- RiskController Implementation --------------------

    @property
    def is_killed(self) -> bool:
        return self.state.risk_state.status == RiskStatus.KILLED

    async def check_dll(self, current_pnl: float) -> bool:
        """Daily Loss Limit Check.
        
        Args:
            current_pnl: Current daily P&L
            
        Returns:
            True if trading is allowed, False otherwise
        """
        dll = self.settings.TOPSTEP_DAILY_LOSS_LIMIT
        
        # 80% threshold = WARNING
        if current_pnl <= dll * 0.80:
            await self._set_yellow(f"DLL 80% threshold reached: {current_pnl} <= {dll * 0.80}")
            
        # 100% = HARD KILL
        if current_pnl <= dll:
            await self._kill(f"DAILY LOSS LIMIT HIT — BOT TERMINATED: {current_pnl} <= {dll}")
            return False
            
        return True

    async def validate_position_size(self, requested_qty: int,
                                     signal: TradeSignal) -> int:
        """Position Sizer Throttle.
        
        Args:
            requested_qty: Requested number of contracts
            signal: Trade signal
            
        Returns:
            Adjusted number of contracts to trade
        """
        max_qty = self.settings.TOPSTEP_MAX_CONTRACTS
        
        if requested_qty > max_qty:
            logger.warning(f"Requested quantity {requested_qty} exceeds max {max_qty}, capping to max")
            return max_qty
            
        if self.state.risk_state.status == RiskStatus.YELLOW:
            # Reduce size in yellow zone
            return max(1, requested_qty - 1)
            
        if self.state.risk_state.status in (RiskStatus.RED, RiskStatus.KILLED):
            return 0
            
        return requested_qty

    async def check_heartbeat(self, latency_ms: float) -> bool:
        """Heartbeat Latency Fail-Safe.
        
        Args:
            latency_ms: Latency in milliseconds
            
        Returns:
            True if trading is allowed, False otherwise
        """
        threshold = self.settings.HEARTBEAT_TIMEOUT_MS
        
        if latency_ms > threshold:
            await self._kill(f"Heartbeat failure: {latency_ms:.0f}ms > {threshold}ms")
            return False
            
        return True

    async def enforce_flat_at_close(self, minutes_to_close: int) -> None:
        """Market Close Flat Check.
        
        Args:
            minutes_to_close: Minutes until market close
        """
        if minutes_to_close <= 5 and self.state.current_position is not None:
            await self._kill("Market close approaching — force flat signal issued")
            # Caller is responsible for submitting the flatten order

    async def approve_signal(self, signal: TradeSignal) -> bool:
        """Signal Quality Gate.
        
        Args:
            signal: Trade signal to validate
            
        Returns:
            True if signal is approved, False otherwise
        """
        # Check confidence
        if signal.confidence < 0.55:
            logger.warning(f"Signal rejected: confidence {signal.confidence} < 0.55")
            return False
            
        # Check risk/reward ratio
        if signal.risk_reward_ratio is not None and signal.risk_reward_ratio < 1.5:
            logger.warning(f"Signal rejected: risk_reward_ratio {signal.risk_reward_ratio} < 1.5")
            return False
            
        # Check risk state
        if self.state.risk_state.status in (RiskStatus.RED, RiskStatus.KILLED):
            logger.warning(f"Signal rejected: risk state is {self.state.risk_state.status.name}")
            return False
            
        # Check for open positions (no pyramiding)
        if self.state.risk_state.open_positions > 0:
            logger.warning(f"Signal rejected: open positions > 0 (no pyramiding)")
            return False
            
        return True

    async def _kill(self, reason: str) -> None:
        """Core Kill Switch.
        
        Args:
            reason: Reason for the kill
        """
        # Idempotency check
        if self.state.risk_state.status == RiskStatus.KILLED:
            logger.warning(f"Kill already triggered, ignoring: {reason}")
            return
            
        # 1. Set state.risk_state.status = KILLED
        self.state.risk_state.status = RiskStatus.KILLED
        
        # 2. Set state.risk_state.kill_reason = reason
        self.state.risk_state.kill_reason = reason
        
        # 3. Log at CRITICAL level with full reason
        logger.critical(f"KILL SWITCH TRIGGERED: {reason}")
        
        # 4. Emit alert via comms/notifier.py (fire and forget)
        try:
            asyncio.create_task(self._notifier.send_alert("CRITICAL", reason))
        except Exception as e:
            logger.error(f"Failed to send kill alert: {e}")
            
        # 5. Set an internal asyncio.Event: self.kill_event
        self.kill_event.set()

    async def _set_yellow(self, reason: str) -> None:
        """Set risk state to YELLOW.
        
        Args:
            reason: Reason for the yellow status
        """
        # If not already YELLOW or worse
        if self.state.risk_state.status not in (RiskStatus.YELLOW, RiskStatus.RED, RiskStatus.KILLED):
            self.state.risk_state.status = RiskStatus.YELLOW
            logger.warning(f"Risk status set to YELLOW: {reason}")
            
            # Notify
            try:
                asyncio.create_task(self._notifier.send_warning(reason))
            except Exception as e:
                logger.error(f"Failed to send yellow alert: {e}")

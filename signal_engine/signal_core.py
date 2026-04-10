import asyncio
import time
import logging
from typing import Dict, Any, Optional, Callable, List
from loguru import logger
from .signal_models import TradeSignal, SignalDirection
from .indicators import OrderFlowImbalanceIndicator, CumulativeDeltaIndicator, VWAPMeanReversionIndicator, EMATrendFilter
from state_manager.state import SharedState
from risk_controller.risk_core import RiskController
from config.settings import Settings

# Import the new modules
try:
    from topstep_bot.signal_engine.indicator_validator import IndicatorHealthGuard, SignalDirection as GuardSignalDirection
    from topstep_bot.signal_engine.indicator_audit import IndicatorAuditLogger, IndicatorVoteRecord, SignalEvaluationRecord
except ImportError:
    # Fallback if running from topstep_bot directory
    from topstep_bot.signal_engine.indicator_validator import IndicatorHealthGuard, SignalDirection as GuardSignalDirection
    from topstep_bot.signal_engine.indicator_audit import IndicatorAuditLogger, IndicatorVoteRecord, SignalEvaluationRecord


class SignalEngine:
    """Core signal generation engine with indicator health verification."""

    def __init__(self, symbol: str, settings: Settings, state: SharedState, risk: RiskController):
        self.symbol = symbol
        self.settings = settings
        self.state = state
        self.risk = risk
        
        # Initialize all 4 indicators
        self.ofi = OrderFlowImbalanceIndicator(window=10, threshold=0.25)
        self.delta = CumulativeDeltaIndicator(lookback=20)
        self.vwap = VWAPMeanReversionIndicator(tick_size=0.01)
        self.ema = EMATrendFilter(fast=9, slow=21)
        
        # Instantiate IndicatorHealthGuard and IndicatorAuditLogger
        self.guard = IndicatorHealthGuard(settings)
        self.audit_logger = IndicatorAuditLogger()
        
        # Configuration
        self.min_signals_required = 3
        self.min_risk_reward = 1.5
        self.min_confidence = 0.55
        
        # State
        self._last_signal_ts = 0.0
        self._eval_lock = asyncio.Lock()
        self._on_signal_callback: Optional[Callable] = None
        self._last_signal: Optional[TradeSignal] = None
        
        # Dictionary to access indicators by name
        self.indicators: Dict[str, Any] = {
            "OFI": self.ofi,
            "DELTA": self.delta,
            "VWAP": self.vwap,
            "EMA": self.ema
        }

    def register_signal_callback(self, cb: Callable) -> None:
        """Store cb. Called when a valid TradeSignal passes ALL verification."""
        self._on_signal_callback = cb
        
    async def on_depth(self, snapshot: Any) -> None:
        """Handle market depth update."""
        self.guard.record_update("OFI")
        self.ofi.update(snapshot.imbalance_ratio if hasattr(snapshot, 'imbalance_ratio') else 0.0)
        await self._evaluate_signals()
        
    async def on_tick(self, tick: Any) -> None:
        """Handle time and sales tick update."""
        self.guard.record_update("DELTA")
        self.guard.record_update("VWAP")
        self.guard.record_update("EMA")
        
        self.delta.update(tick.price, tick.size, tick.aggressor)
        self.vwap.update(tick.price, tick.size)
        self.ema.update(tick.price)
        
        await self._evaluate_signals()
        
    @property
    def all_warmed_up(self) -> bool:
        """True ONLY if ALL 4 indicators have is_warmed_up == True"""
        return (
            self.ofi.is_warmed_up and
            self.delta.is_warmed_up and
            self.vwap.is_warmed_up and
            self.ema.is_warmed_up
        )
        
    @property
    def last_signal(self) -> Optional[TradeSignal]:
        return self._last_signal
        
    def get_indicator_dashboard(self) -> str:
        """Return audit logger dashboard."""
        return self.audit_logger.print_dashboard()
        
    def reset_session(self) -> None:
        """Reset indicators for a new session."""
        self.ofi.reset()
        self.delta.reset()
        self.vwap.reset_session()
        self.ema.reset()
        # Note: guard internal timestamps are NOT reset
        
    async def _evaluate_signals(self) -> Optional[TradeSignal]:
        """Evaluate signals based on current indicator values."""
        # Gate 0: Session Filter (before acquiring lock)
        tradeable, reason = self.guard.is_session_tradeable()
        if not tradeable:
            logger.debug(f"Signal suppressed: {reason}")
            # Log to audit
            await self.audit_logger.log_evaluation(SignalEvaluationRecord(
                evaluation_id=f"{self.symbol}_{int(time.time() * 1000)}",
                timestamp=time.time(),
                votes=[],
                final_direction="FLAT",
                final_confidence=0.0,
                signal_emitted=False,
                rejection_reason=reason
            ))
            return None

        # Gate 1: Warm-Up Gate (before acquiring lock)
        if not self.all_warmed_up:
            counts = {name: ind.is_warmed_up for name, ind in self.indicators.items()}
            logger.debug(f"Indicators not warmed up yet: {counts}")
            # Log to audit
            await self.audit_logger.log_evaluation(SignalEvaluationRecord(
                evaluation_id=f"{self.symbol}_{int(time.time() * 1000)}",
                timestamp=time.time(),
                votes=[],
                final_direction="FLAT",
                final_confidence=0.0,
                signal_emitted=False,
                rejection_reason="Indicators not warmed up"
            ))
            return None

        async with self._eval_lock:
            evaluation_id = f"{self.symbol}_{int(time.time() * 1000)}"
            current_time = time.time()
            rejection_reason = ""
            votes_records: List[IndicatorVoteRecord] = []
            
            # Gate 2: Cooldown Check
            if (current_time - self._last_signal_ts) < 30.0:
                # Log to audit
                await self.audit_logger.log_evaluation(SignalEvaluationRecord(
                    evaluation_id=evaluation_id,
                    timestamp=current_time,
                    votes=[],
                    final_direction="FLAT",
                    final_confidence=0.0,
                    signal_emitted=False,
                    rejection_reason="Cooldown period active"
                ))
                return None
                
            # Gate 3: Collect Raw Votes
            raw_votes = {
                "OFI": (self.ofi.signal(), self.ofi.current_mean, self.guard.get_last_update("OFI")),
                "DELTA": (self.delta.signal(), getattr(self.delta, 'trend_strength', 0.0), self.guard.get_last_update("DELTA")),
                "VWAP": (self.vwap.signal(), abs(getattr(self.vwap, 'volatility', 0.0)) / 20.0, self.guard.get_last_update("VWAP")),
                "EMA": (self.ema.signal(), min(getattr(self.ema, 'trend_strength', 0.0), 1.0), self.guard.get_last_update("EMA")),
            }
            
            # Gate 4: Per-Indicator Validation
            validated_vote_dict = {}
            
            # Validate each indicator
            for name, (direction, strength, ts) in raw_votes.items():
                # Handle NaN strength
                if strength != strength:  # NaN check
                    strength = 0.0
                
                # Convert SignalDirection to GuardSignalDirection if needed
                guard_direction = direction
                if hasattr(direction, 'value'):
                    if direction.value == 1:
                        guard_direction = GuardSignalDirection.LONG
                    elif direction.value == -1:
                        guard_direction = GuardSignalDirection.SHORT
                    else:
                        guard_direction = GuardSignalDirection.FLAT
                
                validated_dir = self.guard.validate_indicator_output(
                    name, guard_direction, strength, ts
                )
                
                # Create vote record
                raw_dir_str = direction.name if hasattr(direction, 'name') else str(direction)
                validated_dir_str = validated_dir.name if hasattr(validated_dir, 'name') else str(validated_dir)
                
                vote_record = IndicatorVoteRecord(
                    timestamp=current_time,
                    evaluation_id=evaluation_id,
                    indicator_name=name,
                    raw_direction=raw_dir_str,
                    validated_direction=validated_dir_str,
                    raw_strength=strength,
                    rejection_reason="",
                    is_warmed_up=self.indicators[name].is_warmed_up
                )
                votes_records.append(vote_record)
                
                # Add to validated dict
                # Convert back to SignalDirection
                final_dir = SignalDirection.LONG if validated_dir == GuardSignalDirection.LONG else (SignalDirection.SHORT if validated_dir == GuardSignalDirection.SHORT else SignalDirection.FLAT)
                validated_vote_dict[name] = (final_dir, strength)
                
            # Gate 5: Vote Aggregation
            final_dir, confidence, agreeing = self.guard.validate_vote_set(validated_vote_dict)
            
            if final_dir == SignalDirection.FLAT:
                logger.debug("Vote aggregation resulted in FLAT")
                await self.audit_logger.log_evaluation(SignalEvaluationRecord(
                    evaluation_id=evaluation_id,
                    timestamp=current_time,
                    votes=votes_records,
                    final_direction="FLAT",
                    final_confidence=0.0,
                    signal_emitted=False,
                    rejection_reason="Vote aggregation resulted in FLAT"
                ))
                return None
                
            # Gate 6: Build Signal
            # Get entry price (simplified, use last price from state)
            entry_price = 15000.0  # Placeholder
            
            # Calculate stop and target
            if final_dir == SignalDirection.LONG:
                stop_price = entry_price - 50.0  # Example stop
                target_price = entry_price + 100.0  # Example target
            else:
                stop_price = entry_price + 50.0
                target_price = entry_price - 100.0
                
            signal = TradeSignal(
                direction=final_dir,
                confidence=confidence,
                entry_price=entry_price,
                stop_price=stop_price,
                target_price=target_price,
                rationale=f"Signal from {', '.join(agreeing)}",
                timestamp=current_time
            )
            
            # Validate signal numerics
            signal_dict = {
                "entry_price": signal.entry_price,
                "stop_price": signal.stop_price,
                "target_price": signal.target_price,
                "direction": final_dir,
                "confidence": signal.confidence,
                "risk_reward_ratio": signal.risk_reward_ratio,
                "timestamp": signal.timestamp
            }
            
            is_valid, validation_reason = self.guard.validate_signal_numerics(signal_dict)
            if not is_valid:
                logger.debug(f"Signal validation failed: {validation_reason}")
                await self.audit_logger.log_evaluation(SignalEvaluationRecord(
                    evaluation_id=evaluation_id,
                    timestamp=current_time,
                    votes=votes_records,
                    final_direction=final_dir.name,
                    final_confidence=confidence,
                    signal_emitted=False,
                    rejection_reason=f"Signal validation failed: {validation_reason}"
                ))
                return None
                
            # Gate 7: Risk/Reward Check
            if signal.risk_reward_ratio < self.min_risk_reward:
                logger.debug(f"R:R too low: {signal.risk_reward_ratio:.2f}")
                await self.audit_logger.log_evaluation(SignalEvaluationRecord(
                    evaluation_id=evaluation_id,
                    timestamp=current_time,
                    votes=votes_records,
                    final_direction=final_dir.name,
                    final_confidence=confidence,
                    signal_emitted=False,
                    rejection_reason=f"R:R too low: {signal.risk_reward_ratio:.2f}"
                ))
                return None
                
            # Gate 8: Final Confidence Check
            if confidence < self.min_confidence:
                logger.debug(f"Confidence too low: {confidence:.3f}")
                await self.audit_logger.log_evaluation(SignalEvaluationRecord(
                    evaluation_id=evaluation_id,
                    timestamp=current_time,
                    votes=votes_records,
                    final_direction=final_dir.name,
                    final_confidence=confidence,
                    signal_emitted=False,
                    rejection_reason=f"Confidence too low: {confidence:.3f}"
                ))
                return None
                
            # EMIT
            self._last_signal_ts = current_time
            self._last_signal = signal
            
            # Log evaluation with signal emitted
            await self.audit_logger.log_evaluation(SignalEvaluationRecord(
                evaluation_id=evaluation_id,
                timestamp=current_time,
                votes=votes_records,
                final_direction=final_dir.name,
                final_confidence=confidence,
                signal_emitted=True,
                rejection_reason=""
            ))
            
            await self.state.update_signal(signal)
            
            if self._on_signal_callback:
                await self._on_signal_callback(signal)
                
            return signal

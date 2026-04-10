"""Signal models for the trading signal engine."""

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional, Dict, Any, List


class SignalDirection(Enum):
    """Trading signal direction."""
    LONG = "long"
    SHORT = "short"
    FLAT = "flat"


class SignalStrength(Enum):
    """Signal strength classification."""
    WEAK = 0.25
    MODERATE = 0.5
    STRONG = 0.75
    VERY_STRONG = 1.0


@dataclass
class Signal:
    """Trading signal with metadata."""
    direction: SignalDirection
    strength: float
    confidence: float
    timestamp: datetime
    indicators: Dict[str, Any] = field(default_factory=dict)
    entry_price: Optional[float] = None
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None
    risk_reward_ratio: Optional[float] = None
    metadata: Dict[str, Any] = field(default_factory=dict)
    # Backward compatibility fields (mapped to new names in __post_init__)
    stop_price: Optional[float] = field(default=None, repr=False)
    target_price: Optional[float] = field(default=None, repr=False)
    rationale: str = field(default="", repr=False)

    def __post_init__(self):
        """Validate signal data after initialization."""
        # Map backward compatibility fields to new names
        if self.stop_price is not None and self.stop_loss is None:
            self.stop_loss = self.stop_price
        if self.target_price is not None and self.take_profit is None:
            self.take_profit = self.target_price
        if self.rationale and "rationale" not in self.metadata:
            self.metadata["rationale"] = self.rationale
        
        # Validate signal data
        if not 0.0 <= self.strength <= 1.0:
            raise ValueError(f"Strength must be between 0 and 1, got {self.strength}")
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError(f"Confidence must be between 0 and 1, got {self.confidence}")
        if self.direction not in SignalDirection:
            raise ValueError(f"Invalid signal direction: {self.direction}")

    @property
    def is_long(self) -> bool:
        """Check if signal is long."""
        return self.direction == SignalDirection.LONG

    @property
    def is_short(self) -> bool:
        """Check if signal is short."""
        return self.direction == SignalDirection.SHORT

    @property
    def is_flat(self) -> bool:
        """Check if signal is flat/neutral."""
        return self.direction == SignalDirection.FLAT

    def to_dict(self) -> Dict[str, Any]:
        """Convert signal to dictionary."""
        return {
            "direction": self.direction.value,
            "strength": self.strength,
            "confidence": self.confidence,
            "timestamp": self.timestamp.isoformat(),
            "indicators": self.indicators,
            "entry_price": self.entry_price,
            "stop_loss": self.stop_loss,
            "take_profit": self.take_profit,
            "risk_reward_ratio": self.risk_reward_ratio,
            "metadata": self.metadata,
        }


@dataclass
class SignalVote:
    """Individual indicator vote for a signal."""
    indicator_name: str
    direction: SignalDirection
    strength: float
    is_stale: bool = False
    is_nan: bool = False

    def effective_strength(self) -> float:
        """Calculate effective strength considering staleness and NaN."""
        if self.is_nan:
            return 0.0
        strength = self.strength
        if self.is_stale:
            strength *= 0.5  # Reduce strength for stale indicators
        return strength


@dataclass
class SignalEvaluation:
    """Signal evaluation result with votes and metadata."""
    votes: List[SignalVote] = field(default_factory=list)
    consensus_direction: Optional[SignalDirection] = None
    total_votes: int = 0
    affirmative_votes: int = 0
    rejection_reason: Optional[str] = None
    evaluation_timestamp: datetime = field(default_factory=datetime.utcnow)

    @property
    def vote_count(self) -> int:
        """Get total vote count."""
        return self.total_votes

    @property
    def is_approved(self) -> bool:
        """Check if signal was approved."""
        return self.consensus_direction is not None and self.rejection_reason is None

    def add_vote(self, vote: SignalVote):
        """Add a vote to the evaluation."""
        self.votes.append(vote)
        self.total_votes += 1
        if not vote.is_nan:
            self.affirmative_votes += 1

    def to_dict(self) -> Dict[str, Any]:
        """Convert evaluation to dictionary."""
        return {
            "votes": [
                {
                    "indicator_name": v.indicator_name,
                    "direction": v.direction.value,
                    "strength": v.strength,
                    "is_stale": v.is_stale,
                    "is_nan": v.is_nan,
                    "effective_strength": v.effective_strength(),
                }
                for v in self.votes
            ],
            "consensus_direction": self.consensus_direction.value if self.consensus_direction else None,
            "total_votes": self.total_votes,
            "affirmative_votes": self.affirmative_votes,
            "rejection_reason": self.rejection_reason,
            "evaluation_timestamp": self.evaluation_timestamp.isoformat(),
        }


@dataclass
class SignalConfiguration:
    """Configuration for signal generation."""
    min_signals_required: int = 3
    min_risk_reward: float = 1.5
    min_confidence: float = 0.55
    max_stale_age_seconds: float = 60.0
    enable_audit_logging: bool = True

    def validate(self) -> bool:
        """Validate configuration values."""
        if self.min_signals_required < 1:
            raise ValueError("min_signals_required must be at least 1")
        if self.min_risk_reward < 0:
            raise ValueError("min_risk_reward must be non-negative")
        if not 0.0 <= self.min_confidence <= 1.0:
            raise ValueError("min_confidence must be between 0 and 1")
        return True


class SignalRegistry:
    """Registry for tracking signal history."""

    def __init__(self, max_history: int = 100):
        self.max_history = max_history
        self._history: List[Signal] = []
        self._last_signal: Optional[Signal] = None

    def add_signal(self, signal: Signal):
        """Add a signal to the registry."""
        self._history.append(signal)
        self._last_signal = signal
        if len(self._history) > self.max_history:
            self._history.pop(0)

    @property
    def last_signal(self) -> Optional[Signal]:
        """Get the last signal."""
        return self._last_signal

    @property
    def history(self) -> List[Signal]:
        """Get signal history."""
        return self._history.copy()

    def clear(self):
        """Clear the registry."""
        self._history.clear()
        self._last_signal = None


# Backward compatibility alias - must be after Signal class definition
TradeSignal = Signal

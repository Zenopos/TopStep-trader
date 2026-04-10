import collections  
import numpy as np  
from typing import List, Optional, Union  
from enum import Enum  

class SignalDirection(Enum):  
    FLAT = 0  
    LONG = 1  
    SHORT = -1  

class OrderFlowImbalanceIndicator:  
    """Predicts market direction based on order flow imbalance."""  
    def __init__(self, window: int = 10, threshold: float = 0.25):  
        self.window = window  
        self.threshold = threshold  
        self.deque = collections.deque(maxlen=window)  
        self.is_warmed_up = False  
        self.current_mean = 0.0  

    def update(self, imbalance: float) -> None:  
        self.deque.append(imbalance)  
        if len(self.deque) < self.window:  
            self.current_mean = 0.0  
            self.is_warmed_up = False  
        else:  
            self.current_mean = np.mean(self.deque)  
            self.is_warmed_up = True  

    def signal(self) -> SignalDirection:  
        if not self.is_warmed_up:  
            return SignalDirection.FLAT  
        if self.current_mean > self.threshold:  
            return SignalDirection.LONG  
        if self.current_mean < -self.threshold:  
            return SignalDirection.SHORT  
        return SignalDirection.FLAT  

    def reset(self) -> None:  
        self.deque.clear()  
        self.is_warmed_up = False  
        self.current_mean = 0.0  

class CumulativeDeltaIndicator:
    """Detects price-divergence patterns from order flow."""
    def __init__(self, lookback: int = 20):
        self.lookback = lookback
        self.price_history = np.zeros(lookback)
        self.delta_history = np.zeros(lookback)
        self.trend_strength = 0.0
        self.divergence_strength = 0.0
        self.is_warmed_up = False
        self._last_direction = SignalDirection.FLAT
        self.reset()

    def update(self, price: float, size: int, aggressor: str) -> None:
        delta = size if aggressor == "BUY" else -size
        self.price_history[:-1] = self.price_history[-self.lookback+1:]
        self.delta_history[:-1] = self.delta_history[-self.lookback+1:]
        self.price_history[-1] = price
        self.delta_history[-1] = delta
        if len(self.price_history) >= self.lookback:
            self.is_warmed_up = True
            x = np.arange(self.lookback)
            price_slope = np.polyfit(x, self.price_history, 1)[0]
            delta_slope = np.polyfit(x, self.delta_history, 1)[0]
            price_trend = price_slope
            delta_trend = delta_slope
            divergence = abs(price_trend - delta_trend)
            max_trend = max(abs(price_trend), abs(delta_trend), 0.001)
            self.trend_strength = divergence / max_trend
            self.divergence_strength = self.trend_strength
            if price_trend > 0 and delta_trend > 0:
                self._last_direction = SignalDirection.LONG
            elif price_trend < 0 and delta_trend < 0:
                self._last_direction = SignalDirection.SHORT
            elif price_trend < 0 and delta_trend > 0:
                self._last_direction = SignalDirection.LONG
            elif price_trend > 0 and delta_trend < 0:
                self._last_direction = SignalDirection.SHORT
            else:
                self._last_direction = SignalDirection.FLAT
        else:
            self.is_warmed_up = False
            self._last_direction = SignalDirection.FLAT

    def signal(self) -> SignalDirection:
        return self._last_direction

    @property
    def cumulative_delta(self) -> float:
        return np.sum(self.delta_history)

    def reset(self) -> None:
        self.price_history = np.zeros(self.lookback)
        self.delta_history = np.zeros(self.lookback)
        self.trend_strength = 0.0
        self.divergence_strength = 0.0
        self.is_warmed_up = False
        self._last_direction = SignalDirection.FLAT

class VWAPMeanReversionIndicator:  
    """VWAP-based mean reversion strategy with volatility filtering."""  
    def __init__(self, tick_size: float, std_dev_bands: float = 1.5):  
        self._tick_size = tick_size  
        self.std_dev_bands = std_dev_bands  
        self.is_warmed_up = False
        self.distance_from_vwap_ticks = 0.0
        self._last_price = 0.0
        self.reset_session()  

    def reset_session(self) -> None:  
        self.session_vwap = 0.0  
        self.session_volume = 0  
        self.vwap_history = []  
        self.price_history = []  
        self.volatility = 0.0  
        self.is_warmed_up = False

    def update(self, price: float, volume: int) -> None:
        self._last_price = price
        self.session_volume += volume  
        self.vwap_history.append(price * volume)  
        self.price_history.append(price)  
        if len(self.price_history) > 20:  
            self.price_history.pop(0)  
            self.vwap_history.pop(0)  
        if len(self.price_history) >= 1:  
            total_volume = sum(self.vwap_history) if self.vwap_history else volume  
            self.session_vwap = self._calculate_vwap(total_volume)  
        if len(self.price_history) >= 20:  
            std_dev = np.std(self.price_history, ddof=1)  
            if std_dev > 0:  
                self.volatility = std_dev * self.std_dev_bands  
            self.is_warmed_up = True
            self.distance_from_vwap_ticks = (price - self.session_vwap) / self._tick_size if self._tick_size > 0 else 0.0

    def _calculate_vwap(self, total_volume: float) -> float:  
        if total_volume == 0:  
            return 0.0  
        return np.sum(np.array(self.vwap_history)) / total_volume  

    def signal(self) -> SignalDirection:  
        if len(self.vwap_history) < 1 or not self.is_warmed_up:
            return SignalDirection.FLAT
        if self._last_price == 0:
            return SignalDirection.FLAT
        current_ticks = self._last_price / self._tick_size  
        vwap_ticks = self.session_vwap / self._tick_size  
        upper = (self.session_vwap + self.volatility) / self._tick_size  
        lower = (self.session_vwap - self.volatility) / self._tick_size  
        if current_ticks > upper:  
            return SignalDirection.SHORT  
        if current_ticks < lower:  
            return SignalDirection.LONG  
        return SignalDirection.FLAT  

    def reset(self) -> None:  
        self.reset_session()  

class EMATrendFilter:  
    """Filters unwanted price action using dual EMA trending."""  
    def __init__(self, fast: int = 9, slow: int = 21):  
        self.fast_period = fast  
        self.slow_period = slow  
        self.fast_ema = np.zeros(self.slow_period)  
        self.slow_ema = np.zeros(self.slow_period)  
        self.trend_strength = 0.0  
        self.valid_signals = False  
        self.is_warmed_up = False
        self._last_direction = SignalDirection.FLAT
        self.reset()  

    def update(self, price: float) -> None:
        alpha_fast = 2 / (self.fast_period + 1)  
        alpha_slow = 2 / (self.slow_period + 1)  
        if not self.valid_signals:  
            self.fast_ema.fill(price)  
            self.slow_ema.fill(price)  
            self.valid_signals = len(self.fast_ema) == self.fast_period
            self.is_warmed_up = False
            self._last_direction = SignalDirection.FLAT
            return
        
        for i in range(self.fast_period-1):  
            self.fast_ema[i] = self.fast_ema[i+1]  
        self.fast_ema[self.fast_period-1] = (price * alpha_fast) + self.fast_ema[self.fast_period-2] * (1 - alpha_fast)  
        for i in range(self.slow_period-1):  
            self.slow_ema[i] = self.slow_ema[i+1]  
        self.slow_ema[self.slow_period-1] = (price * alpha_slow) + self.slow_ema[self.slow_period-2] * (1 - alpha_slow)  
        emadiff = abs(self.fast_ema[-1] - self.slow_ema[-1])  
        self.trend_strength = emadiff / self.slow_ema[-1] if self.slow_ema[-1] != 0 else 0.0
        
        if len(self.fast_ema) >= self.slow_period:
            self.is_warmed_up = True
            self._last_direction = SignalDirection.LONG if self.fast_ema[-1] > self.slow_ema[-1] else SignalDirection.SHORT
        else:
            self.is_warmed_up = False
            self._last_direction = SignalDirection.FLAT

    def signal(self) -> SignalDirection:
        return self._last_direction

    def reset(self) -> None:  
        self.fast_ema.fill(0)  
        self.slow_ema.fill(0)  
        self.trend_strength = 0.0
        self.is_warmed_up = False
        self._last_direction = SignalDirection.FLAT

class Indicators:  
    _instance = None  

    def __new__(cls):  
        if cls._instance is None:  
            cls._instance = super().__new__(cls)  
            cls._instance._initialize_indicators()  
        return cls._instance  

    def _initialize_indicators(self):  
        self._of = OrderFlowImbalanceIndicator(window=10, threshold=0.25)  
        self._cd = CumulativeDeltaIndicator(lookback=20)  
        self._vma = VWAPMeanReversionIndicator(tick_size=0.01)  
        self._ef = EMATrendFilter(fast=9, slow=21)  

    @property  
    def ofi(self):  
        return self._of  

    @property  
    def cum_delta(self):  
        return self._cd  

    @property  
    def vwap_mr(self):  
        return self._vma  

    @property  
    def ema_filter(self):  
        return self._ef
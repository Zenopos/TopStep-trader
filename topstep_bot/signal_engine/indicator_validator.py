import logging import time  
from enum import Enum  
from typing import Dict, Union, Tuple, Optional  
import time from datetime import datetime, timezone  
from zoneinfo import ZoneInfo  
import time from zoneinfo import ZoneInfo  
import time  

class SignalDirection(Enum):  
    LONG = "LONG"  
    SHORT = "SHORT"  
    FLAT = "FLAT"  

class IndicatorHealthGuard:  
    def __init__(self, settings: Union[dict, object]):  
        self.min_strength_per_indicator = 0.30  
        self.max_staleness_seconds = 3.0  
        self.session_filters = {  
            "NO_TRADE_WINDOWS": [  
                ("09:25", "09:35"),  # CME open volatility window  
                ("11:55", "12:05"),  # Lunch dead zone boundary  
                ("15:45", "16:00")  # Market close — positions must be flat  
            ]  
        }  
        self.last_update_ts: Dict[str, float] = {}  # Track last update timestamps  

    def validate_indicator_output(self, indicator_name: str, direction: SignalDirection, strength: float, last_update_ts: float) -> SignalDirection:  
        """Validate single indicator output with multiple rejection conditions."""  
        # type checks  
        try:  
            strength = float(strength)  
            last_update_ts = float(last_update_ts)  
        except (ValueError, TypeError) as e:  
            logging.warning(f"Invalid type for strength or timestamp: {e}")  
            return SignalDirection.FLAT  

        # 1. Range check  
        if not (0.0 <= strength <= 1.0):  
            logging.warning(f"Out-of-range strength: {strength} for {indicator_name}")  
            return SignalDirection.FLAT  

        # 2. Minimum strength check  
        if abs(strength) < self.min_strength_per_indicator:  
            logging.warning(f"Too weak signal on {indicator_name}, strength: {strength:.3f}")  
            return SignalDirection.FLAT  

        # 3. Staleness check  
        age_seconds = time.time() - last_update_ts  
        if age_seconds > self.max_staleness_seconds:  
            logging.warning(f"Stale indicator {indicator_name}, {age_seconds:.2f}s old")  
            return SignalDirection.FLAT  

        # Valid case  
        return direction  

    def record_update(self, indicator_name: str):  
        """Update last timestamp for indicator health check"""  
        self.last_update_ts[indicator_name] = time.time()  

    def get_last_update(self, indicator_name: str) -> float:  
        """Get last update timestamp for indicator validation"""  
        return self.last_update_ts.get(indicator_name, 0.0)  

    def is_session_tradeable(self) -> Tuple[bool, str]:  
        """Determine if current time is in a valid trading session using CT timezone."""  
        # Get current UTC time then convert to CT (Chicago timezone)  
        now_utc = datetime.now(timezone.utc)  
        ct_zone = ZoneInfo("America/Chicago")  
        ct_time = now_utc.astimezone(ct_zone)  

        # Check for weekends  
        if ct_time.weekday() >= 5:  # Saturday=5, Sunday=6  
            return False, "Weekend detected in CT time"  

        # Convert time to HH:MM string  
        time_str = ct_time.strftime("%H:%M")  

        # Check against NO_TRADE_WINDOWS  
        for start, end in self.session_filters.get("NO_TRADE_WINDOWS", []):  
            # Convert window times to time objects  
            try:  
                start_ts = datetime.strptime(start, "%H:%M").time()  
                end_ts = datetime.strptime(end, "%H:%M").time()  
            except ValueError:  
                continue  # Skip invalid time windows  

            # Check if current time falls within window  
            if start_ts <= ct_time.time() < end_ts:  
                return False, f"Currently in blocked session ({start}-{end} CT)"  

        return True, ""  

    def validate_signal_numerics(self, signal: Dict[str, Optional[float]]) -> Tuple[bool, str]:  
        """Validate TradeSignal numeric components against all risk constraints."""  
        # Basic numeric checks  
        if signal.get("entry_price", 0.0) <= 0 or signal.get("stop_price", 0.0) <= 0 or signal.get("target_price", 0.0) <= 0:  
            return False, "All prices must be positive"  

        # Stop distance check  
        if abs(signal["entry_price"] - signal["stop_price"]) < 2 * 0.0001:  # Example tick size  
            return False, "Stop distance must be at least 2 ticks"  

        # Price sequence validation  
        if signal["direction"] == SignalDirection.LONG:  
            if signal["stop_price"] > signal["entry_price"] or signal["target_price"] <= signal["entry_price"]:  
                return False, "Invalid LONG signal: Stop above entry or target not above entry"  
        elif signal["direction"] == SignalDirection.SHORT:  
            if signal["target_price"] > signal["entry_price"] or signal["stop_price"] < signal["entry_price"]:  
                return False, "Invalid SHORT signal: Target above entry or stop below entry"  

        # Risk-reward ratio check  
        stop_distance = abs(signal["entry_price"] - signal["stop_price"])  
        target_distance = abs(signal["entry_price"] - signal["target_price"])  
        risk_reward = target_distance / stop_distance if stop_distance > 0 else float('inf')  
        if signal.get("confidence", float('nan')) < SignalDirection.FLAT:  
            return False, "Confidence must be between 0.0 and 1.0"  
        if signal.get("risk_reward_ratio", float('inf')) < 1.5:  
            return False, "Risk-reward ratio must be ≥1.5"  

        # Timestamp check  
        signal_ts = signal.get("timestamp", 0.0)  
        if signal_ts > time.time() + 1.0:  
            return False, "Future timestamp detected"  

        return True, ""  

    def validate_vote_set(self, votes: Dict[str, Tuple[SignalDirection, float]]) -> Tuple[SignalDirection, float, list[str]]:  
        """Aggregate validated votes into a final direction decision.  
        Steps:  
        1. For each vote, call validate_indicator_output() to clean it  
        2. Count cleaned LONG votes with their strengths  
        3. Count cleaned SHORT votes with their strengths  
        4. If winning side has < 3 cleaned votes: return (FLAT, 0.0, [])  
        5. Compute weighted confidence: confidence = sum(strengths of agreeing votes) / 4 (denominator is always 4 — non-voters reduce confidence)  
        6. Return (winning_direction, confidence, list_of_agreeing_indicator_names)  
        """  
        # Validate individual votes  
        valid_votes = {name: self.validate_indicator_output(name, dir, strength, self.get_last_update(name))  
                       for name, (dir, strength) in votes.items()}  

        # Count valid votes  
        long_votes = [vote for vote in valid_votes.values() if vote != SignalDirection.FLAT and vote == SignalDirection.LONG]  
        short_votes = [vote for vote in valid_votes.values() if vote != SignalDirection.FLAT and vote == SignalDirection.SHORT]  

        # Check vote count  
        if len(long_votes) + len(short_votes) < 3:  
            return SignalDirection.FLAT, 0.0, []  

        # Determine winning direction  
        long_weight = sum(strength for vote, strength in valid_votes.items() if vote == SignalDirection.LONG)  
        short_weight = sum(strength for vote, strength in valid_votes.items() if vote == SignalDirection.SHORT)  

        confidence = (long_weight + short_weight) / 4  
        winning_direction = SignalDirection.LONG if len(long_votes) > len(short_votes) else SignalDirection.SHORT  

        return winning_direction, confidence, [name for name, vote in valid_votes.items() if vote != SignalDirection.FLAT]  

# Unit Tests  
import unittest  
import time  

class TestIndicatorValidator(unittest.TestCase):  
    def setUp(self):  
        self.guard = IndicatorHealthGuard(None)  

    def test_validate_signal_numerics_pass(self):  
        signal = {  
            "entry_price": 100.0,  
            "stop_price": 99.99,  
            "target_price": 100.5,  
            "direction": SignalDirection.LONG,  
            "confidence": 0.8,  
            "risk_reward_ratio": 2.0,  
            "timestamp": time.time() - 1.0  
        }  
        valid, reason = self.guard.validate_signal_numerics(signal)  
        self.assertTrue(valid)  
        self.assertEqual(reason, "")  

    def test_validate_signal_numerics_negative_prices(self):  
        signal = {  
            "entry_price": -1.0,  
            "stop_price": -2.0,  
            "target_price": -3.0,  
            "direction": SignalDirection.LONG  
        }  
        valid, reason = self.guard.validate_signal_numerics(signal)  
        self.assertFalse(valid)  
        self.assertTrue("All prices must be positive" in reason)  

    def test_validate_signal_numerics_weak_strength(self):  
        signal = {  
            "entry_price": 100.0,  
            "stop_price": 99.99,  
            "target_price": 100.5,  
            "direction": SignalDirection.LONG,  
            "confidence": 0.8,  
            "risk_reward_ratio": 2.0,  
            "timestamp": time.time() - 1.0  
        }  
        self.guard.last_update_ts = {"test_indicator": time.time() - 4.0}  # Make stale  
        valid, reason = self.guard.validate_signal_numerics(signal)  
        self.assertFalse(valid)  
        self.assertTrue("Stale indicator" in reason)  

    def test_validate_vote_set(self):  
        votes = {  
            "TEST1": (SignalDirection.LONG, 0.6),  
            "TEST2": (SignalDirection.LONG, 0.7),  
            "TEST3": (SignalDirection.FLAT, 0.5),  # Weak signal  
            "TEST4": (SignalDirection.LONG, 0.8)  
        }  
        # Update timestamps to avoid staleness  
        for name in votes.keys():  
            self.guard.record_update(name)  

        result = self.guard.validate_vote_set(votes)  
        self.assertEqual(result[0], SignalDirection.LONG)  
        self.assertAlmostEqual(result[1], (0.6 + 0.7 + 0.8) / 4, places=2)  

    def test_validate_vote_set_stale_indicators(self):  
        votes = {  
            "TEST1": (SignalDirection.LONG, 0.6),  
            "TEST2": (SignalDirection.LONG, 0.7)  
        }  
        # Make indicators stale  
        self.guard.last_update_ts["TEST1"] = time.time() - 4.0  
        self.guard.last_update_ts["TEST2"] = time.time() - 4.0  

        valid_votes = {name: self.guard.validate_indicator_output(name, dir, strength, ts)  
                       for name, (dir, strength) in votes.items()}  

        result = self.guard.validate_vote_set(valid_votes)  
        self.assertEqual(result[0], SignalDirection.FLAT)  
        self.assertEqual(result[1], 0.0)  

if __name__ == "__main__":  
    unittest.main()
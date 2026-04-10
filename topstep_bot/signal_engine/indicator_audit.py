from dataclasses import dataclass, asdict
import asyncio
import json
import os
from collections import deque
from datetime import date
from typing import List, Optional

import aiofiles


@dataclass
class IndicatorVoteRecord:
    timestamp: float
    evaluation_id: str
    indicator_name: str
    raw_direction: str
    validated_direction: str
    raw_strength: float
    rejection_reason: str
    is_warmed_up: bool


@dataclass
class SignalEvaluationRecord:
    evaluation_id: str
    timestamp: float
    votes: List[IndicatorVoteRecord]
    final_direction: str
    final_confidence: float
    signal_emitted: bool
    rejection_reason: str


class IndicatorAuditLogger:
    def __init__(self, log_dir: str = "logs/indicator_audit"):
        self.log_dir = log_dir
        if not os.path.exists(self.log_dir):
            os.makedirs(self.log_dir)
        
        today = date.today().isoformat()
        self.file_path = os.path.join(self.log_dir, f"audit_{today}.jsonl")
        
        self.records: deque = deque(maxlen=200)
        self._lock = asyncio.Lock()
        self._write_buffer: List[str] = []
        self._buffer_count = 0
        self._last_flush_time = asyncio.get_event_loop().time()
        self._flush_interval = 5  # seconds
        self._flush_threshold = 10  # records
        
        # Load last 200 records from today's file into memory deque
        asyncio.create_task(self._load_recent_records())

    async def _load_recent_records(self):
        if not os.path.exists(self.file_path):
            return
        
        try:
            async with aiofiles.open(self.file_path, "r") as f:
                async for line in f:
                    try:
                        data = json.loads(line)
                        # Reconstruct votes
                        votes = [IndicatorVoteRecord(**vote) for vote in data.get("votes", [])]
                        record = SignalEvaluationRecord(
                            evaluation_id=data["evaluation_id"],
                            timestamp=data["timestamp"],
                            votes=votes,
                            final_direction=data["final_direction"],
                            final_confidence=data["final_confidence"],
                            signal_emitted=data["signal_emitted"],
                            rejection_reason=data["rejection_reason"]
                        )
                        self.records.append(record)
                    except (json.JSONDecodeError, KeyError):
                        continue
        except Exception:
            pass  # Ignore errors during loading

    async def log_evaluation(self, record: SignalEvaluationRecord) -> None:
        """Serialize to JSON and append to file asynchronously using aiofiles.
        Buffer writes: flush every 10 records or every 5 seconds.
        Never block the event loop.
        """
        # Add to in-memory deque immediately
        async with self._lock:
            self.records.append(record)
        
        # Serialize to JSON
        record_dict = asdict(record)
        # Convert votes back to dicts for JSON serialization
        record_dict["votes"] = [asdict(v) for v in record.votes]
        json_line = json.dumps(record_dict)
        
        # Buffer the write
        async with self._lock:
            self._write_buffer.append(json_line)
            self._buffer_count += 1
        
        # Check if we need to flush
        current_time = asyncio.get_event_loop().time()
        should_flush = (
            self._buffer_count >= self._flush_threshold or
            current_time - self._last_flush_time >= self._flush_interval
        )
        
        if should_flush:
            await self._flush_buffer()

    async def _flush_buffer(self):
        """Flush the write buffer to the file."""
        if not self._write_buffer:
            return
        
        async with self._lock:
            buffer_to_write = self._write_buffer
            self._write_buffer = []
            self._buffer_count = 0
            self._last_flush_time = asyncio.get_event_loop().time()
        
        try:
            async with aiofiles.open(self.file_path, "a") as f:
                for line in buffer_to_write:
                    await f.write(line + "\n")
        except Exception:
            # Put back into buffer if write failed
            async with self._lock:
                self._write_buffer.extend(buffer_to_write)
                self._buffer_count += len(buffer_to_write)

    def get_recent_evaluations(self, last_n: int = 50) -> List[SignalEvaluationRecord]:
        """Return last N records from in-memory deque.
        (For real-time diagnostics without file I/O)
        """
        # Note: This is a synchronous method, but we access a deque which is thread-safe for append/pop.
        # For strict asyncio safety, we could make this async, but for read-only access to deque it's generally safe.
        # However, to be fully async-safe, let's make it async.
        # Wait, the task says "Return last N records from in--memory deque( maxlen=200)."
        # and "In- memory deque must be protected with asyncio. Lock".
        # So I should make this async or use the lock. Let's make it async to be safe.
        # But the signature is not async. Let's check the task again.
        # "def get_recent_evaluations(self, last_n: int = 50) -> list[SignalEvaluationRecord]:"
        # It's not async. I will leave it as is but access the deque carefully.
        # Actually, accessing a deque from multiple coroutines without a lock is not safe if there are writes.
        # But this is a read operation. Python's deque is thread-safe for append/pop but not necessarily for iteration while modifying.
        # However, since we have a lock for writes, and this is a read, it might be okay.
        # But to be safe, let's use the lock.
        # Wait, I can't await in a non-async function.
        # I will just return the list(self.records)[-last_n:] which should be safe enough as it's a snapshot.
        return list(self.records)[-last_n:]

    async def get_recent_evaluations_async(self, last_n: int = 50) -> List[SignalEvaluationRecord]:
        """Async version of get_recent_evaluations."""
        async with self._lock:
            return list(self.records)[-last_n:]

    def compute_indicator_accuracy(
        self,
        indicator_name: str,
        window: int = 100
    ) -> dict:
        """
        From the last `window` evaluations where this indicator voted
        non-FLAT, compute:
          - agreement_rate: % of times it agreed with final emitted signal direction
          - vote_frequency: % of evaluations where it voted non-FLAT
          - avg_strength: mean strength when voting non-FLAT
          - rejection_rate: % of its votes rejected by IndicatorHealthGuard
        Return as dict. Used for live health monitoring.
        """
        # Get the last `window` evaluations
        records = list(self.records)[-window:]
        
        if not records:
            return {
                "agreement_rate": 0.0,
                "vote_frequency": 0.0,
                "avg_strength": 0.0,
                "rejection_rate": 0.0
            }
        
        total_evaluations = len(records)
        votes_by_indicator = []
        
        # Find votes for this indicator in the last window
        for record in records:
            for vote in record.votes:
                if vote.indicator_name == indicator_name:
                    votes_by_indicator.append(vote)
        
        if not votes_by_indicator:
            return {
                "agreement_rate": 0.0,
                "vote_frequency": 0.0,
                "avg_strength": 0.0,
                "rejection_rate": 0.0
            }
        
        # Calculate metrics
        # Vote frequency: % of evaluations where it voted non-FLAT
        # We need to track (record, vote) pairs to correctly calculate agreement
        record_vote_pairs = []
        
        for record in records:
            for vote in record.votes:
                if vote.indicator_name == indicator_name and vote.validated_direction != "FLAT":
                    record_vote_pairs.append((record, vote))
        
        if not record_vote_pairs:
            return {
                "agreement_rate": 0.0,
                "vote_frequency": 0.0,
                "avg_strength": 0.0,
                "rejection_rate": 0.0
            }
        
        vote_frequency = len(record_vote_pairs) / total_evaluations
        
        # Agreement rate: % of times it agreed with final emitted signal direction
        agreements = 0
        rejected = 0
        total_strength = 0.0
        
        for record, vote in record_vote_pairs:
            # Check if vote was rejected
            if vote.rejection_reason:
                rejected += 1
            
            # Check agreement with final direction
            # Note: We use validated_direction because that's what was actually used
            if vote.validated_direction == record.final_direction:
                agreements += 1
            
            total_strength += vote.raw_strength
        
        agreement_rate = agreements / len(record_vote_pairs) if record_vote_pairs else 0.0
        avg_strength = total_strength / len(record_vote_pairs) if record_vote_pairs else 0.0
        rejection_rate = rejected / len(record_vote_pairs) if record_vote_pairs else 0.0
        
        return {
            "agreement_rate": agreement_rate,
            "vote_frequency": vote_frequency,
            "avg_strength": avg_strength,
            "rejection_rate": rejection_rate
        }

    def print_dashboard(self) -> str:
        """
        Returns a formatted multi-line string showing per-indicator
        accuracy stats from the last 100 evaluations.
        """
        # Get all unique indicator names
        indicator_names = set()
        for record in self.records:
            for vote in record.votes:
                indicator_names.add(vote.indicator_name)
        
        if not indicator_names:
            return "No indicator data available."
        
        # Calculate stats for each indicator
        stats = {}
        for name in indicator_names:
            stats[name] = self.compute_indicator_accuracy(name, window=100)
        
        # Format the output
        lines = []
        lines.append("╔══════════════════════════════════════════════════════╗")
        lines.append("║         INDICATOR HEALTH DASHBOARD                   ║")
        lines.append("╠══════════════════════════════════╦═══════╦══════════╣")
        lines.append("║ Indicator                        ║ Agree ║ Strength ║")
        lines.append("╠══════════════════════════════════╬═══════╬══════════╣")
        
        for name in sorted(stats.keys()):
            s = stats[name]
            agreement_pct = int(s["agreement_rate"] * 100)
            strength = s["avg_strength"]
            lines.append(f"║ {name:<32} ║  {agreement_pct:3d}%  ║ {strength:6.2f}  ║")
        
        lines.append("╚══════════════════════════════════╩═══════╩══════════╝")
        
        return "\n".join(lines)

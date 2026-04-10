"""Backtesting engine for validating SignalEngine and RiskController against historical data."""

import pandas as pd
import numpy as np
from typing import List, Literal, Optional
from dataclasses import dataclass, field
import asyncio
import argparse
import sys
from loguru import logger

from config.settings import Settings
from signal_engine.signal_core import SignalEngine
from risk_controller.risk_core import RiskController
from state_manager.state import SharedState
from data_ingestion.data_models import MarketDepthSnapshot, TimeAndSalesTick
from signal_engine.signal_models import TradeSignal, SignalDirection


# Configure logging
logger.remove()
logger.add(
    sys.stderr,
    level="INFO",
    colorize=True,
    format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> | <level>{level: <8}</level> | <cyan>{message}</cyan>"
)


@dataclass
class TradeResult:
    """Result of a single trade in the backtest."""
    entry_price: float
    exit_price: float
    side: Literal["LONG", "SHORT"]
    pnl_ticks: float
    pnl_dollars: float
    outcome: Literal["WIN", "LOSS", "TIMEOUT"]
    duration_ticks: int


@dataclass
class BacktestResult:
    """Result of the backtest run."""
    total_trades: int
    win_rate: float
    avg_win_ticks: float
    avg_loss_ticks: float
    profit_factor: float
    max_drawdown_dollars: float
    sharpe_ratio: float
    expectancy_per_trade: float
    trades: List[TradeResult] = field(default_factory=list)


class BacktestEngine:
    """Offline backtesting harness for trading strategies.
    
    This engine replays historical market data to validate the SignalEngine
    and RiskController without making any network calls.
    """
    
    # Contract multiplier for NQ (points to dollars)
    NQ_MULTIPLIER = 20.0
    # Tick size for NQ
    TICK_SIZE = 0.25
    
    def __init__(self, settings: Settings, data_path: str):
        """Initialize the backtest engine.
        
        Args:
            settings: Application settings.
            data_path: Path to the CSV file containing historical data.
        """
        self.settings = settings
        self.data_path = data_path
        
        # Load CSV data
        self._load_data()
        
        # Initialize SharedState (mock)
        self.state = SharedState()
        
        # Initialize RiskController with mock state
        self.risk_controller = RiskController(settings, self.state)
        
        # Initialize SignalEngine with RiskController
        self.signal_engine = SignalEngine(
            symbol=settings.SYMBOL,
            settings=settings,
            state=self.state,
            risk=self.risk_controller
        )
        
        # Trade log
        self.trade_log: List[TradeResult] = []
        
        # Current position state
        self.current_position: Optional[TradeSignal] = None
        
    def _load_data(self) -> None:
        """Load historical data from CSV file."""
        logger.info(f"Loading data from {self.data_path}")
        self.data = pd.read_csv(self.data_path)
        
        # Parse timestamp as float (Unix timestamp)
        if 'timestamp' in self.data.columns:
            self.data['timestamp'] = self.data['timestamp'].astype(float)
        else:
            raise ValueError("CSV must contain 'timestamp' column")
            
        # Sort by timestamp
        self.data = self.data.sort_values('timestamp').reset_index(drop=True)
        logger.info(f"Loaded {len(self.data)} rows of data")
        
    def _reconstruct_depth_snapshot(self, row: pd.Series) -> MarketDepthSnapshot:
        """Reconstruct MarketDepthSnapshot from a DataFrame row.
        
        Args:
            row: A row from the DataFrame.
            
        Returns:
            MarketDepthSnapshot object.
        """
        bids = []
        asks = []
        
        # Extract bid prices and sizes (1-5 levels)
        for i in range(1, 6):
            bid_price_col = f'bid_price_{i}'
            bid_size_col = f'bid_size_{i}'
            if bid_price_col in row and bid_size_col in row:
                bids.append((float(row[bid_price_col]), int(row[bid_size_col])))
                
        # Extract ask prices and sizes (1-5 levels)
        for i in range(1, 6):
            ask_price_col = f'ask_price_{i}'
            ask_size_col = f'ask_size_{i}'
            if ask_price_col in row and ask_size_col in row:
                asks.append((float(row[ask_price_col]), int(row[ask_size_col])))
                
        snapshot = MarketDepthSnapshot(
            timestamp=float(row['timestamp']),
            symbol=self.settings.SYMBOL,
            bids=bids,
            asks=asks
        )
        
        # Compute imbalance
        snapshot.compute_imbalance()
        
        return snapshot
    
    def _reconstruct_tick(self, row: pd.Series) -> TimeAndSalesTick:
        """Reconstruct TimeAndSalesTick from a DataFrame row.
        
        Args:
            row: A row from the DataFrame.
            
        Returns:
            TimeAndSalesTick object.
        """
        return TimeAndSalesTick(
            timestamp=float(row['timestamp']),
            price=float(row['price']),
            size=int(row['size']),
            aggressor=row['aggressor'] if 'aggressor' in row else "BUY",
            exchange="SIM"  # Simulation exchange
        )
        
    async def run(self) -> BacktestResult:
        """Run the backtest.
        
        Iterates through the data, feeds it to the SignalEngine, and simulates
        trade execution.
        
        Returns:
            BacktestResult containing all metrics.
        """
        logger.info("Starting backtest...")
        
        # Reset signal engine session
        self.signal_engine.reset_session()
        
        # Iterate through data
        for i in range(len(self.data)):
            row = self.data.iloc[i]
            
            # Reconstruct market depth snapshot
            depth_snapshot = self._reconstruct_depth_snapshot(row)
            
            # Reconstruct tick
            tick = self._reconstruct_tick(row)
            
            # Feed to signal engine
            await self.signal_engine.on_depth(depth_snapshot)
            await self.signal_engine.on_tick(tick)
            
            # Check if a signal was generated
            signal = self.signal_engine.last_signal
            if signal and signal.direction != SignalDirection.FLAT:
                # Check if we already have a position
                if self.current_position is None:
                    # Simulate trade execution
                    logger.info(f"Signal generated: {signal.direction} at {tick.price}")
                    self.current_position = signal
                    
                    # Simulate fill
                    future_rows = self.data.iloc[i+1:] if i + 1 < len(self.data) else pd.DataFrame()
                    trade_result = self.simulate_fill(signal, future_rows)
                    
                    # Record trade
                    self.trade_log.append(trade_result)
                    logger.info(f"Trade completed: {trade_result.outcome} - PnL: ${trade_result.pnl_dollars:.2f}")
                    
                    # Clear current position
                    self.current_position = None
                    
        logger.info(f"Backtest completed. Total trades: {len(self.trade_log)}")
        
        # Compute metrics
        return self.compute_metrics(self.trade_log)
        
    def simulate_fill(self, signal: TradeSignal, future_rows: pd.DataFrame) -> TradeResult:
        """Simulate trade fill based on stop/target prices.
        
        Scans forward through future rows to determine if stop or target is hit.
        
        Args:
            signal: The trade signal.
            future_rows: Future DataFrame rows to scan.
            
        Returns:
            TradeResult with entry, exit, PnL, and outcome.
        """
        # Entry price (use signal entry price or current price from last row)
        entry_price = signal.entry_price if signal.entry_price else 0.0
        
        # Determine stop and target prices
        if signal.direction == SignalDirection.LONG:
            stop_price = signal.stop_loss if signal.stop_loss else entry_price - 50 * self.TICK_SIZE
            target_price = signal.take_profit if signal.take_profit else entry_price + 100 * self.TICK_SIZE
        else:  # SHORT
            stop_price = signal.stop_loss if signal.stop_loss else entry_price + 50 * self.TICK_SIZE
            target_price = signal.take_profit if signal.take_profit else entry_price - 100 * self.TICK_SIZE
            
        side = "LONG" if signal.direction == SignalDirection.LONG else "SHORT"
        
        # Scan forward (max 500 rows)
        max_rows = min(500, len(future_rows))
        exit_price = None
        outcome = "TIMEOUT"
        
        for j in range(max_rows):
            future_row = future_rows.iloc[j]
            current_price = float(future_row['price'])
            
            # Check if stop or target is hit
            if side == "LONG":
                if current_price <= stop_price:
                    exit_price = stop_price
                    outcome = "LOSS"
                    break
                elif current_price >= target_price:
                    exit_price = target_price
                    outcome = "WIN"
                    break
            else:  # SHORT
                if current_price >= stop_price:
                    exit_price = stop_price
                    outcome = "LOSS"
                    break
                elif current_price <= target_price:
                    exit_price = target_price
                    outcome = "WIN"
                    break
                
        # If neither hit, exit at last available price
        if exit_price is None:
            if len(future_rows) > 0:
                exit_price = float(future_rows.iloc[-1]['price'])
            else:
                exit_price = entry_price  # No future data
                
        # Calculate PnL
        if side == "LONG":
            pnl_ticks = (exit_price - entry_price) / self.TICK_SIZE
        else:
            pnl_ticks = (entry_price - exit_price) / self.TICK_SIZE
            
        pnl_dollars = pnl_ticks * self.NQ_MULTIPLIER
        
        return TradeResult(
            entry_price=entry_price,
            exit_price=exit_price,
            side=side,
            pnl_ticks=pnl_ticks,
            pnl_dollars=pnl_dollars,
            outcome=outcome,
            duration_ticks=max_rows
        )
        
    def compute_metrics(self, trade_log: List[TradeResult]) -> BacktestResult:
        """Compute backtest metrics.
        
        Args:
            trade_log: List of trade results.
            
        Returns:
            BacktestResult with computed metrics.
        """
        if not trade_log:
            return BacktestResult(
                total_trades=0,
                win_rate=0.0,
                avg_win_ticks=0.0,
                avg_loss_ticks=0.0,
                profit_factor=0.0,
                max_drawdown_dollars=0.0,
                sharpe_ratio=0.0,
                expectancy_per_trade=0.0,
                trades=[]
            )
            
        total_trades = len(trade_log)
        
        # Win rate
        wins = [t for t in trade_log if t.outcome == "WIN"]
        losses = [t for t in trade_log if t.outcome == "LOSS"]
        timeouts = [t for t in trade_log if t.outcome == "TIMEOUT"]
        
        win_rate = len(wins) / total_trades if total_trades > 0 else 0.0
        
        # Average win/loss in ticks
        avg_win_ticks = np.mean([t.pnl_ticks for t in wins]) if wins else 0.0
        avg_loss_ticks = np.mean([t.pnl_ticks for t in losses]) if losses else 0.0
        
        # Profit factor
        total_wins = sum(t.pnl_dollars for t in wins)
        total_losses = abs(sum(t.pnl_dollars for t in losses))
        profit_factor = total_wins / total_losses if total_losses > 0 else 0.0
        
        # Max drawdown
        equity = 0.0
        peak_equity = 0.0
        max_drawdown_dollars = 0.0
        
        for trade in trade_log:
            equity += trade.pnl_dollars
            if equity > peak_equity:
                peak_equity = equity
            drawdown = peak_equity - equity
            if drawdown > max_drawdown_dollars:
                max_drawdown_dollars = drawdown
                
        # Sharpe ratio
        # Group PnL by day (simplified: assume each trade is a separate day for now)
        daily_returns = [t.pnl_dollars for t in trade_log]
        
        if len(daily_returns) > 1:
            mean_return = np.mean(daily_returns)
            std_return = np.std(daily_returns)
            if std_return > 0:
                sharpe_ratio = (mean_return / std_return) * np.sqrt(252)
            else:
                sharpe_ratio = 0.0
        else:
            sharpe_ratio = 0.0
            
        # Expectancy per trade
        expectancy_per_trade = np.mean([t.pnl_dollars for t in trade_log])
        
        return BacktestResult(
            total_trades=total_trades,
            win_rate=win_rate,
            avg_win_ticks=avg_win_ticks,
            avg_loss_ticks=avg_loss_ticks,
            profit_factor=profit_factor,
            max_drawdown_dollars=max_drawdown_dollars,
            sharpe_ratio=sharpe_ratio,
            expectancy_per_trade=expectancy_per_trade,
            trades=trade_log
        )
        
    def generate_report(self, result: BacktestResult) -> str:
        """Generate a markdown report of the backtest results.
        
        Args:
            result: The backtest result.
            
        Returns:
            Formatted markdown string.
        """
        report_lines = [
            "# Backtest Report",
            "",
            "## Summary Metrics",
            "",
            f"- **Total Trades**: {result.total_trades}",
            f"- **Win Rate**: {result.win_rate * 100:.2f}%",
            f"- **Avg Win (ticks)**: {result.avg_win_ticks:.2f}",
            f"- **Avg Loss (ticks)**: {result.avg_loss_ticks:.2f}",
            f"- **Profit Factor**: {result.profit_factor:.2f}",
            f"- **Max Drawdown ($)**: ${result.max_drawdown_dollars:.2f}",
            f"- **Sharpe Ratio**: {result.sharpe_ratio:.2f}",
            f"- **Expectancy per Trade**: ${result.expectancy_per_trade:.2f}",
            "",
            "## Trade Log",
            ""
        ]
        
        # Trade list summary
        if result.trades:
            report_lines.append("| # | Side | Entry | Exit | PnL ($) | Outcome |")
            report_lines.append("|---|------|-------|------|---------|---------|")
            
            for i, trade in enumerate(result.trades, 1):
                report_lines.append(
                    f"| {i} | {trade.side} | {trade.entry_price:.2f} | {trade.exit_price:.2f} | "
                    f"{trade.pnl_dollars:.2f} | {trade.outcome} |"
                )
                
        return "\n".join(report_lines)


async def main():
    """Main entry point for the backtest engine."""
    parser = argparse.ArgumentParser(description="Run backtest on historical data")
    parser.add_argument("--data", type=str, required=True, help="Path to CSV data file")
    parser.add_argument("--symbol", type=str, default="NQ", help="Trading symbol")
    
    args = parser.parse_args()
    
    # Load settings
    settings = Settings()
    settings.SYMBOL = args.symbol
    
    # Initialize engine
    engine = BacktestEngine(settings, args.data)
    
    # Run backtest
    result = await engine.run()
    
    # Generate and print report
    report = engine.generate_report(result)
    print(report)


if __name__ == "__main__":
    asyncio.run(main())

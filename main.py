"""Topstep Trading Bot - Main Entry Point"""
import asyncio
import sys
from datetime import datetime
from zoneinfo import ZoneInfo
from loguru import logger
from config.settings import Settings
from comms.notifier import Notifier
from data_ingestion.auth_client import TradovateAuthClient
from data_ingestion.rest_client import TradovateRESTClient
from data_ingestion.ws_client import TradovateWSClient
from signal_engine.signal_core import SignalEngine
from risk_controller.risk_core import RiskController
from execution.executor import OrderExecutor
from state_manager.state import SharedState


def _configure_logging():
    """Configure loguru logging with console and file sinks."""
    logger.remove()
    logger.add(
        sys.stderr,
        level="INFO",
        colorize=True,
        format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> | <level>{level: <8}</level> | <cyan>{message}</cyan>"
    )
    logger.add(
        "logs/bot_{time:YYYY-MM-DD}.log",
        level="DEBUG",
        rotation="1 day",
        retention="7 days",
        format="{time:YYYY-MM-DD HH:mm:ss.SSS} | {level: <8} | {name}:{function}:{line} - {message}"
    )


async def _pnl_monitor(rest, risk, state):
    """Monitor PnL and check daily loss limit."""
    while True:
        await asyncio.sleep(30)
        try:
            balance = await rest.get_account_balance()
            session_pnl = balance - state.session_start_pnl
            await risk.check_dll(session_pnl)
        except Exception as e:
            logger.warning(f"PnL monitor error: {e}")


async def _close_monitor(risk, state):
    """Monitor time until market close and enforce flat position at close."""
    while True:
        await asyncio.sleep(60)
        now_ct = datetime.now(ZoneInfo("America/Chicago"))
        close_ct = now_ct.replace(hour=15, minute=59, second=0, microsecond=0)
        minutes_to_close = int((close_ct - now_ct).total_seconds() / 60)
        await risk.enforce_flat_at_close(minutes_to_close)


async def _kill_watcher(risk, ws, executor, notifier):
    """Watch for kill event and perform emergency shutdown."""
    while True:
        await asyncio.sleep(0.1)
        if risk.kill_event.is_set():
            logger.critical(f"Kill event fired: {risk.kill_event}")
            await executor.flatten_all(reason="Kill event")
            await ws.stop()
            await notifier.send_alert(
                f"BOT KILLED: {risk.risk_state.kill_reason}", "CRITICAL"
            )
            sys.exit(1)


async def main():
    """Main async entry point for the trading bot."""
    settings = Settings()
    _configure_logging()
    
    state = SharedState()
    notifier = Notifier(settings)
    auth = TradovateAuthClient(settings)
    token = await auth.authenticate()
    rest = TradovateRESTClient(auth, settings)
    risk = RiskController(settings, state)
    
    account_info = await rest.get_account_info()
    account_id = account_info.get("id")
    if not account_id:
        raise RuntimeError("Cannot resolve account_id")
    
    signal_engine = SignalEngine(settings.SYMBOL, settings, state, risk)
    executor = OrderExecutor(rest, risk, state, settings, account_id)
    signal_engine.register_signal_callback(executor.execute_signal)
    
    ws = TradovateWSClient(
        md_token=await auth.get_md_token(),
        ord_token=token,
        symbol=settings.SYMBOL,
        state=state,
        on_depth_cb=signal_engine.on_depth,
        on_tick_cb=signal_engine.on_tick,
        on_heartbeat_cb=risk.check_heartbeat,
        on_fill_cb=executor.on_fill_event,
        settings=settings
    )
    
    logger.info(
        f"Bot starting | Symbol={settings.SYMBOL} | "
        f"Env={settings.TRADOVATE_ENV} | "
        f"Account={account_id} | "
        f"DLL={settings.TOPSTEP_DAILY_LOSS_LIMIT} | "
        f"MaxContracts={settings.TOPSTEP_MAX_CONTRACTS}"
    )
    
    import signal
    
    async def _shutdown(sig_name):
        logger.warning(f"Received {sig_name}, initiating shutdown...")
        await executor.flatten_all(f"{sig_name} shutdown")
        await ws.stop()
        await notifier.close()
        sys.exit(0)
    
    def sigint_handler(sig, frame):
        asyncio.create_task(_shutdown("SIGINT"))
    
    def sigterm_handler(sig, frame):
        asyncio.create_task(_shutdown("SIGTERM"))
    
    signal.signal(signal.SIGINT, sigint_handler)
    signal.signal(signal.SIGTERM, sigterm_handler)
    
    async with asyncio.TaskGroup() as tg:
        tg.create_task(ws.start(), name="ws_feeds")
        tg.create_task(_pnl_monitor(rest, risk, state), name="pnl_monitor")
        tg.create_task(_close_monitor(risk, state), name="close_monitor")
        tg.create_task(_kill_watcher(risk, ws, executor, notifier), name="kill_watcher")


if __name__ == "__main__":
    asyncio.run(main())

import asyncio
import json
import time
import websockets
from collections import deque
from typing import Optional, Callable, Any, Union
from loguru import logger

from .data_models import MarketDepthSnapshot, TimeAndSalesTick
from .order_book import OrderBook
from state_manager.state import SharedState
from execution.execution_models import Order, OrderSide, OrderStatus
from risk_controller.risk_models import RiskState, RiskStatus
from config.settings import settings
from config.constants import TRADOVATE_WS_URLS


class TradovateWSClient:
    """WebSocket client for Tradovate API real-time data.
    
    Handles both Market Data (MD) and Order feed WebSockets with:
    - Tradovate non-standard frame format parsing
    - Automatic reconnection with exponential backoff
    - Heartbeat monitoring and kill-switch logic
    - DOM and T&S data buffering
    """
    
    HEARTBEAT_INTERVAL_MS = 2500
    HEARTBEAT_TIMEOUT_MS = 30000  # default, overridden by settings
    
    def __init__(
        self,
        md_token: str,
        ord_token: str,
        symbol: str,
        state: SharedState,
        on_depth_cb: Callable[[MarketDepthSnapshot], Any],
        on_tick_cb: Callable[[TimeAndSalesTick], Any],
        on_heartbeat_cb: Callable[[float], Any],
        on_fill_cb: Callable[[Order], Any],
    ):
        self.md_token = md_token
        self.ord_token = ord_token
        self.symbol = symbol
        self.state = state
        self.on_depth_cb = on_depth_cb
        self.on_tick_cb = on_tick_cb
        self.on_heartbeat_cb = on_heartbeat_cb
        self.on_fill_cb = on_fill_cb
        
        # Connection state - using Any to avoid Pylance stubs issues with websockets
        self._md_ws: Any = None
        self._ord_ws: Any = None
        self._md_task: Optional[asyncio.Task] = None
        self._ord_task: Optional[asyncio.Task] = None
        self._running = False
        
        # Request ID counters
        self._md_request_id = 0
        self._ord_request_id = 0
        
        # Heartbeat tracking
        self._last_heartbeat_ts = time.monotonic()
        self._heartbeat_timeout_ms = getattr(settings, 'HEARTBEAT_TIMEOUT_MS', self.HEARTBEAT_TIMEOUT_MS)
        
        # Reconnection state
        self._reconnect_delay = 1.0
        self._max_reconnect_delay = 60.0
        self._consecutive_failures = 0
        self._max_failures = 5
        
        # Buffers
        self._dom_buffer: deque[MarketDepthSnapshot] = deque(maxlen=500)
        self._tns_buffer: deque[TimeAndSalesTick] = deque(maxlen=5000)
        
        # Order book for DOM aggregation
        self._order_book = OrderBook(symbol)
        
        # Track active subscriptions for re-subscribe
        self._active_subscriptions_md: list = []
        self._active_subscriptions_ord: list = []
        
        # URLs
        env = getattr(settings, 'TRADOVATE_ENV', 'demo')
        self._md_url = TRADOVATE_WS_URLS.get(env, TRADOVATE_WS_URLS['demo'])
        self._ord_url = TRADOVATE_WS_URLS.get(env, TRADOVATE_WS_URLS['demo'])
        
        logger.info(f"TradovateWSClient initialized for {symbol} in {env} mode")
    
    # -------------------------------------------------------------------------
    # Internal Framing
    # -------------------------------------------------------------------------
    
    async def _send(self, ws: Any, op: str, args: list) -> None:
        """Send Tradovate-formatted frame: {op}\n{request_id}\n\n{json_body}"""
        if ws is None or getattr(ws, 'close_code', None) is not None:
            logger.warning(f"Cannot send to closed WebSocket: {op}")
            return
            
        # Determine which request ID counter to use
        if ws == self._md_ws:
            self._md_request_id += 1
            request_id = self._md_request_id
        else:
            self._ord_request_id += 1
            request_id = self._ord_request_id
        
        body = json.dumps(args) if not isinstance(args, str) else args
        frame = f"{op}\n{request_id}\n\n{body}"
        
        await ws.send(frame)
        logger.debug(f"Sent frame op={op} rid={request_id}")
    
    def _parse_frame(self, raw: Union[str, bytes]) -> tuple[str, int, dict]:
        """Parse Tradovate frame response into (event_type, request_id, data).
        
        Tradovate sends frames as: {op}\n{request_id}\n\n{json_body}
        Or heartbeat frames as: []
        """
        # Handle bytes/memoryview/bytearray from websocket - ensure we have a string
        decoded_raw: str
        if isinstance(raw, (memoryview, bytearray)):
            decoded_raw = bytes(raw).decode('utf-8')
        elif isinstance
            decoded_raw = raw.decode('utf-8')
        else:
            decoded_raw = raw
        
        if decoded_raw == "[]":
            return ("heartbeat", 0, {})
        
        parts = decoded_raw.split("\n\n", 1)
        if len(parts) != 2:
            logger.warning(f"Malformed frame (no body separator): {decoded_raw[:100]}")
            return ("unknown", 0, {})
        
        header = parts[0]
        body = parts[1]
        
        header_parts = header.split("\n")
        if len(header_parts) < 2:
            logger.warning(f"Malformed frame header: {header}")
            return ("unknown", 0, {})
        
        op: str = header_parts[0]
        try:
            request_id: int = int(header_parts[1])
        except ValueError:
            request_id = 0
        
        try:
            data: dict = json.loads(body) if body else {}
        except json.JSONDecodeError:
            logger.warning(f"Failed to parse JSON body: {body[:100]}")
            data = {}
        
        return (op, request_id, data)
    
    # -------------------------------------------------------------------------
    # Connection Management
    # -------------------------------------------------------------------------
    
    async def _connect_feed(self, url: str, token: str, is_md: bool) -> Any:
        """Connect and authenticate a single feed."""
        headers = {"Authorization": f"Bearer {token}"}
        ws = await websockets.connect(url, extra_headers=headers)
        
        # Send initial connect message
        await self._send(ws, "Connect", [{"name": "WebSocket", "version": "1.0"}])
        
        # Wait for connection confirmation
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=10.0)
            op, rid, data = self._parse_frame(raw)
            if op == "heartbeat" or "WebSocket" in str(data):
                logger.info(f"{'MD' if is_md else 'ORD'} feed connected successfully")
            else:
                logger.warning(f"Unexpected connect response: {op} {data}")
        except asyncio.TimeoutError:
            logger.error(f"Timeout waiting for connect confirmation on {'MD' if is_md else 'ORD'} feed")
        except Exception as e:
            logger.error(f"Error during connect confirmation: {e}")
        
        return ws
    
    async def _subscribe_md(self, ws: Any) -> None:
        """Subscribe to market data channels."""
        # Subscribe to DOM
        await self._send(ws, "md/subscribeDOM", [
            {"symbol": self.symbol, "reset": True}
        ])
        self._active_subscriptions_md.append("md/subscribeDOM")
        
        # Subscribe to trades (T&S)
        await self._send(ws, "md/subscribeTrade", [
            {"symbol": self.symbol}
        ])
        self._active_subscriptions_md.append("md/subscribeTrade")
        
        logger.info(f"MD subscriptions sent for {self.symbol}")
    
    async def _subscribe_ord(self, ws: Any) -> None:
        """Subscribe to order feed channels."""
        # Initial account/position sync
        await self._send(ws, "user/syncrequest", [{}])
        self._active_subscriptions_ord.append("user/syncrequest")
        
        logger.info("Order feed subscriptions sent")
    
    async def _resubscribe(self, ws: Any, is_md: bool) -> None:
        """Re-subscribe to all active subscriptions after reconnect."""
        subs = self._active_subscriptions_md if is_md else self._active_subscriptions_ord
        for sub in subs:
            if sub == "md/subscribeDOM":
                await self._send(ws, sub, [{"symbol": self.symbol, "reset": True}])
            elif sub == "md/subscribeTrade":
                await self._send(ws, sub, [{"symbol": self.symbol}])
            elif sub == "user/syncrequest":
                await self._send(ws, sub, [{}])
            logger.debug(f"Re-subscribed to {sub}")
    
    # -------------------------------------------------------------------------
    # Message Handling
    # -------------------------------------------------------------------------
    
    def _parse_dom(self, data: dict) -> Optional[MarketDepthSnapshot]:
        """Parse Tradovate DOM event into MarketDepthSnapshot.
        
        Event structure: { "bp": [...bids], "ap": [...asks], "bs": [...bid_sizes], "as": [...ask_sizes] }
        """
        try:
            bids_raw = data.get("bp", [])
            asks_raw = data.get("ap", [])
            bid_sizes_raw = data.get("bs", [])
            ask_sizes_raw = data.get("as", [])
            
            # Build (price, size) tuples
            bids: list = []
            for i, price in enumerate(bids_raw):
                size = bid_sizes_raw[i] if i < len(bid_sizes_raw) else 0
                bids.append((float(price), int(size)))
            
            asks: list = []
            for i, price in enumerate(asks_raw):
                size = ask_sizes_raw[i] if i < len(ask_sizes_raw) else 0
                asks.append((float(price), int(size)))
            
            snapshot = MarketDepthSnapshot(
                timestamp=time.time(),
                symbol=self.symbol,
                bids=bids,
                asks=asks
            )
            
            # Compute imbalance before returning
            snapshot.compute_imbalance()
            return snapshot
        except Exception as e:
            logger.error(f"Error parsing DOM: {e}")
            return None
    
    def _parse_trade(self, data: dict) -> Optional[TimeAndSalesTick]:
        """Parse Tradovate trade event into TimeAndSalesTick."""
        try:
            # Extract price and size
            price = float(data.get("p", data.get("price", 0)))
            size = int(data.get("s", data.get("size", 0)))
            
            # Determine aggressor side
            side = data.get("side", data.get("d", "")).upper()
            if side in ("BUY", "B", "1"):
                aggressor = "BUY"
            elif side in ("SELL", "S", "2"):
                aggressor = "SELL"
            else:
                aggressor = "BUY"  # default
                logger.debug(f"Unknown trade side: {side}, defaulting to BUY")
            
            # Extract timestamp
            ts_val = data.get("t", data.get("time", 0))
            if isinstance(ts_val, (int, float)):
                timestamp = float(ts_val) / 1000.0  # ms to seconds
            else:
                timestamp = time.time()
            
            exchange = data.get("ex", data.get("exchange", "CME"))
            
            return TimeAndSalesTick(
                timestamp=timestamp,
                price=price,
                size=size,
                aggressor=aggressor,
                exchange=exchange
            )
        except Exception as e:
            logger.error(f"Error parsing trade: {e}")
            return None
    
    def _parse_fill(self, data: dict) -> Optional[Order]:
        """Parse Tradovate fill event into Order."""
        try:
            order_id = str(data.get("orderId", data.get("id", "")))
            symbol = data.get("symbol", self.symbol)
            
            # Determine side
            side_str = data.get("side", "").upper()
            if side_str in ("BUY", "B"):
                side = OrderSide.BUY
            else:
                side = OrderSide.SELL
            
            # Determine order type
            order_type_str = data.get("orderType", "MARKET").upper()
            from execution.execution_models import OrderType
            if order_type_str == "LIMIT":
                order_type = OrderType.LIMIT
            elif order_type_str == "STOP":
                order_type = OrderType.STOP
            else:
                order_type = OrderType.MARKET
            
            quantity = int(data.get("qty", data.get("quantity", 1)))
            filled_price_val = data.get("fillPrice", data.get("price", 0))
            filled_price = float(filled_price_val) if filled_price_val is not None else 0.0
            
            order = Order(
                client_order_id=order_id,
                symbol=symbol,
                side=side,
                order_type=order_type,
                quantity=quantity,
                filled_price=filled_price,
                status=OrderStatus.FILLED
            )
            
            return order
        except Exception as e:
            logger.error(f"Error parsing fill: {e}")
            return None
    
    async def _handle_md_message(self, ws: Any) -> None:
        """Handle incoming MD WebSocket message."""
        try:
            raw = await ws.recv()
            
            # Handle heartbeat
            if raw == "[]":
                now = time.monotonic()
                latency_ms = (now - self._last_heartbeat_ts) * 1000.0
                self._last_heartbeat_ts = now
                
                if asyncio.iscoroutinefunction(self.on_heartbeat_cb):
                    await self.on_heartbeat_cb(latency_ms)
                else:
                    self.on_heartbeat_cb(latency_ms)
                
                # Check for heartbeat timeout
                if latency_ms > self._heartbeat_timeout_ms:
                    logger.warning(f"Heartbeat timeout detected: {latency_ms:.2f}ms > {self._heartbeat_timeout_ms}ms")
                    risk_state = RiskState(
                        status=RiskStatus.RED,
                        kill_reason="Heartbeat timeout"
                    )
                    await self.state.update_risk(risk_state)
                
                return
            
            op, rid, data = self._parse_frame(raw)
            
            # Handle DOM updates
            if op == "dom" or op == "md/subscribeDOM":
                snapshot = self._parse_dom(data)
                if snapshot:
                    self._dom_buffer.append(snapshot)
                    await self.state.update_market_depth(snapshot)
                    if snapshot.bids:
                        self._order_book.update_bid(snapshot.bids[0][0], snapshot.bids[0][1])
                    if snapshot.asks:
                        self._order_book.update_ask(snapshot.asks[0][0], snapshot.asks[0][1])
                    
                    if asyncio.iscoroutinefunction(self.on_depth_cb):
                        await self.on_depth_cb(snapshot)
                    else:
                        self.on_depth_cb(snapshot)
            
            # Handle T&S updates
            elif op == "trade" or op == "md/subscribeTrade":
                tick = self._parse_trade(data)
                if tick:
                    self._tns_buffer.append(tick)
                    await self.state.add_time_and_sale(tick)
                    
                    if asyncio.iscoroutinefunction(self.on_tick_cb):
                        await self.on_tick_cb(tick)
                    else:
                        self.on_tick_cb(tick)
            
            elif op == "heartbeat":
                # Already handled above
                pass
            
            else:
                logger.debug(f"MD unhandled op: {op}")
        
        except websockets.exceptions.ConnectionClosed as e:
            logger.warning(f"MD connection closed: {e}")
            raise  # Let caller handle reconnection
        except Exception as e:
            logger.error(f"Error handling MD message: {e}")
    
    async def _handle_ord_message(self, ws: Any) -> None:
        """Handle incoming Order WebSocket message."""
        try:
            raw = await ws.recv()
            
            # Handle heartbeat
            if raw == "[]":
                return  # Order feed heartbeats are less critical
            
            op, rid, data = self._parse_frame(raw)
            
            # Handle fill events
            if op == "fill" or op == "order" or "fill" in str(data).lower():
                order = self._parse_fill(data)
                if order:
                    await self.state.resolve_order(
                        order.client_order_id,
                        OrderStatus.FILLED,
                        order.filled_price if order.filled_price is not None else 0.0
                    )
                    
                    if asyncio.iscoroutinefunction(self.on_fill_cb):
                        await self.on_fill_cb(order)
                    else:
                        self.on_fill_cb(order)
            
            # Handle sync responses (account/position updates)
            elif op == "user/syncrequest" or op == "sync":
                logger.debug(f"Order sync data received: {data}")
            
            else:
                logger.debug(f"ORD unhandled op: {op}")
        
        except websockets.exceptions.ConnectionClosed as e:
            logger.warning(f"ORD connection closed: {e}")
            raise  # Let caller handle reconnection
        except Exception as e:
            logger.error(f"Error handling ORD message: {e}")
    
    # -------------------------------------------------------------------------
    # Feed Tasks
    # -------------------------------------------------------------------------
    
    async def _md_feed_loop(self) -> None:
        """Main loop for MD feed."""
        while self._running:
            try:
                ws = await self._connect_feed(self._md_url, self.md_token, is_md=True)
                self._md_ws = ws
                
                await self._subscribe_md(ws)
                self._reconnect_delay = 1.0  # Reset on successful connect
                self._consecutive_failures = 0
                
                while self._running:
                    await self._handle_md_message(ws)
            
            except websockets.exceptions.ConnectionClosed as e:
                logger.warning(f"MD feed disconnected: {e}")
            except Exception as e:
                logger.error(f"MD feed error: {e}")
            
            if self._running:
                self._consecutive_failures += 1
                if self._consecutive_failures >= self._max_failures:
                    logger.critical(f"MD feed: {self._max_failures} consecutive failures - KILLING")
                    risk_state = RiskState(
                        status=RiskStatus.KILLED,
                        kill_reason="MD feed reconnect failure"
                    )
                    await self.state.update_risk(risk_state)
                    self._running = False
                    break
                
                logger.warning(f"MD reconnecting in {self._reconnect_delay:.1f}s (attempt {self._consecutive_failures})")
                await asyncio.sleep(self._reconnect_delay)
                self._reconnect_delay = min(self._reconnect_delay * 2, self._max_reconnect_delay)
                
                # Re-auth before reconnect
                # In production, refresh token here
                logger.info("MD re-authenticating before reconnect...")
    
    async def _ord_feed_loop(self) -> None:
        """Main loop for Order feed."""
        while self._running:
            try:
                ws = await self._connect_feed(self._ord_url, self.ord_token, is_md=False)
                self._ord_ws = ws
                
                await self._subscribe_ord(ws)
                
                while self._running:
                    await self._handle_ord_message(ws)
            
            except websockets.exceptions.ConnectionClosed as e:
                logger.warning(f"ORD feed disconnected: {e}")
            except Exception as e:
                logger.error(f"ORD feed error: {e}")
            
            if self._running:
                self._consecutive_failures += 1
                if self._consecutive_failures >= self._max_failures:
                    logger.critical(f"ORD feed: {self._max_failures} consecutive failures - KILLING")
                    risk_state = RiskState(
                        status=RiskStatus.KILLED,
                        kill_reason="ORD feed reconnect failure"
                    )
                    await self.state.update_risk(risk_state)
                    self._running = False
                    break
                
                logger.warning(f"ORD reconnecting in {self._reconnect_delay:.1f}s (attempt {self._consecutive_failures})")
                await asyncio.sleep(self._reconnect_delay)
                self._reconnect_delay = min(self._reconnect_delay * 2, self._max_reconnect_delay)
    
    # -------------------------------------------------------------------------
    # Public API
    # -------------------------------------------------------------------------
    
    async def start(self) -> None:
        """Launch both MD and Order feeds as concurrent asyncio.Tasks."""
        if self._running:
            logger.warning("TradovateWSClient already running")
            return
        
        self._running = True
        self._last_heartbeat_ts = time.monotonic()
        
        self._md_task = asyncio.create_task(self._md_feed_loop())
        self._ord_task = asyncio.create_task(self._ord_feed_loop())
        
        logger.info("TradovateWSClient started - MD and ORD feeds launching")
    
    async def stop(self) -> None:
        """Cancel both tasks gracefully, send unsubscribe frames before closing."""
        logger.info("Stopping TradovateWSClient...")
        self._running = False
        
        # Send unsubscribe frames if connected
        try:
            if self._md_ws and getattr(self._md_ws, 'close_code', None) is None:
                if "md/subscribeDOM" in self._active_subscriptions_md:
                    await self._send(self._md_ws, "md/unsubscribeDOM", [{"symbol": self.symbol}])
                if "md/subscribeTrade" in self._active_subscriptions_md:
                    await self._send(self._md_ws, "md/unsubscribeTrade", [{"symbol": self.symbol}])
                await self._md_ws.close()
                logger.debug("MD feed closed gracefully")
        except Exception as e:
            logger.error(f"Error closing MD feed: {e}")
        
        try:
            if self._ord_ws and getattr(self._ord_ws, 'close_code', None) is None:
                if "user/syncrequest" in self._active_subscriptions_ord:
                    await self._send(self._ord_ws, "user/unsubscribe", [{}])
                await self._ord_ws.close()
                logger.debug("ORD feed closed gracefully")
        except Exception as e:
            logger.error(f"Error closing ORD feed: {e}")
        
        # Cancel tasks
        if self._md_task and not self._md_task.done():
            self._md_task.cancel()
            try:
                await self._md_task
            except asyncio.CancelledError:
                pass
        
        if self._ord_task and not self._ord_task.done():
            self._ord_task.cancel()
            try:
                await self._ord_task
            except asyncio.CancelledError:
                pass
        
        logger.info("TradovateWSClient stopped")
    
    # -------------------------------------------------------------------------
    # Buffer Access
    # -------------------------------------------------------------------------
    
    def get_dom_buffer(self) -> list[MarketDepthSnapshot]:
        """Get the current DOM buffer contents."""
        return list(self._dom_buffer)
    
    def get_tns_buffer(self) -> list[TimeAndSalesTick]:
        """Get the current T&S buffer contents."""
        return list(self._tns_buffer)

# Contract specifications for NQ and ES futures
CONTRACT_SPECS = {
    "NQ": {
        "tick_size": 0.25,
        "tick_value": 5.0,
        "margin": 15000,  # Example margin, adjust as needed
        "exchange": "CME"
    },
    "ES": {
        "tick_size": 0.25,
        "tick_value": 12.5,
        "margin": 12000,  # Example margin, adjust as needed
        "exchange": "CME"
    }
}

# API URLs
TRADOVATE_API_URLS = {
    "demo": "https://demo.tradovateapi.com/v1",
    "live": "https://live.tradovateapi.com/v1"
}

# WebSocket URLs
TRADOVATE_WS_URLS = {
    "demo": "wss://demo.tradovateapi.com/v1/websocket",
    "live": "wss://live.tradovateapi.com/v1/websocket"
}
"""Environment configuration for Tradovate API.

This module defines the available environments (DEMO, LIVE) and provides
a function to retrieve the corresponding API endpoints.

Usage:
    from config.environments import get_endpoints, Environment

    endpoints = get_endpoints(Environment.DEMO)
"""

from enum import Enum


class Environment(str, Enum):
    """Tradovate environment types."""

    DEMO = "demo"
    LIVE = "live"


API_ENDPOINTS = {
    "demo": {
        "rest_base": "https://demo.tradovateapi.com/v1",
        "ws_md": "wss://md.tradovateapi.com/v1/websocket",
        "ws_order": "wss://demo.tradovateapi.com/v1/websocket"
    },
    "live": {
        "rest_base": "https://live.tradovateapi.com/v1",
        "ws_md": "wss://md.tradovateapi.com/v1/websocket",
        "ws_order": "wss://live.tradovateapi.com/v1/websocket"
    }
}


def get_endpoints(env: Environment) -> dict:
    """Get the API endpoints for the specified environment.

    Args:
        env: The environment to get endpoints for (DEMO or LIVE).

    Returns:
        A dictionary containing 'rest_base', 'ws_md', and 'ws_order' URLs.
    """
    return API_ENDPOINTS[env.value]

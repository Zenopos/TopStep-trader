"""Telegram notification module for TopstepBot.

This module provides async notification capabilities via Telegram bot API.
"""

import aiohttp
from loguru import logger

from config.settings import Settings


class Notifier:
    """Telegram notifier for sending alerts and notifications.

    This class provides async methods to send messages to a Telegram chat
    via a bot token. It uses aiohttp for non-blocking HTTP requests.

    Attributes:
        token: The Telegram bot token from settings.
        chat_id: The target Telegram chat ID from settings.
        _session: Shared aiohttp.ClientSession for HTTP requests.
    """

    def __init__(self, settings: Settings) -> None:
        """Initialize the Notifier with settings.

        Args:
            settings: Application settings containing Telegram credentials.
        """
        self.token = settings.TELEGRAM_BOT_TOKEN
        self.chat_id = settings.TELEGRAM_CHAT_ID
        self._session = aiohttp.ClientSession()

    async def send_alert(self, message: str, level: str = "INFO") -> None:
        """Send an alert message via Telegram and log it.

        If both token and chat_id are configured, the message will be sent
        to Telegram. Regardless of Telegram delivery, the message is always
        logged locally at the appropriate level.

        Args:
            message: The message text to send.
            level: The log level (INFO, WARNING, CRITICAL, etc.). Defaults to "INFO".
        """
        # Always log the message locally at the matching level
        if level == "INFO":
            logger.info(message)
        elif level == "WARNING":
            logger.warning(message)
        elif level == "CRITICAL":
            logger.critical(message)
        else:
            logger.info(message)

        # Send to Telegram if credentials are available
        if self.token and self.chat_id:
            telegram_url = f"https://api.telegram.org/bot{self.token}/sendMessage"
            payload = {"chat_id": self.chat_id, "text": f"[{level}] TopstepBot\n{message}"}

            try:
                await self._session.post(telegram_url, json=payload)
            except Exception as e:
                logger.warning(str(e))

    async def close(self) -> None:
        """Close the aiohttp session."""
        await self._session.close()
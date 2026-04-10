import aiohttp
import asyncio
import time
import uuid
from pathlib import Path
from typing import Optional
from loguru import logger

from config.settings import Settings


class TradovateError(Exception):
    """Base exception for Tradovate API errors."""
    pass


class TradovateAuthError(TradovateError):
    """Exception for Tradovate authentication errors."""
    pass


class TradovateAuthClient:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.access_token: Optional[str] = None
        self.md_access_token: Optional[str] = None
        self.expiration_time: float = 0
        self.device_id: str = self._load_or_create_device_id()
        self.session: Optional[aiohttp.ClientSession] = None

    def _load_or_create_device_id(self) -> str:
        device_id_file = Path(".device_id")
        if device_id_file.exists():
            return device_id_file.read_text().strip()
        else:
            device_id = str(uuid.uuid4())
            device_id_file.write_text(device_id)
            return device_id

    async def authenticate(self) -> str:
        if self.session is None:
            self.session = aiohttp.ClientSession()
        assert self.session is not None

        payload = {
            "name": self.settings.tradovate_username,
            "password": self.settings.tradovate_password,
            "appId": self.settings.tradovate_app_id,
            "appVersion": "1.0",
            "cid": self.settings.tradovate_cid,
            "sec": self.settings.tradovate_sec,
            "deviceId": self.device_id
        }

        logger.debug(f"Authenticating with Tradovate: {payload}")

        try:
            async with self.session.post(
                f"{self.settings.tradovate_base_url}/auth/accesstokenrequest",
                json=payload
            ) as response:
                if response.status == 200:
                    data = await response.json()
                    access_token = data.get("accessToken")
                    md_access_token = data.get("mdAccessToken")
                    if access_token is None:
                        raise TradovateAuthError("accessToken missing in response")
                    self.access_token = access_token
                    if md_access_token is None:
                        raise TradovateAuthError("mdAccessToken missing in response")
                    self.md_access_token = md_access_token
                    expires_in = data.get("expiresIn", 1800)  # Default 30 minutes
                    self.expiration_time = time.time() + expires_in
                    logger.info("Successfully authenticated with Tradovate")
                    return self.access_token
                else:
                    error_text = await response.text()
                    logger.error(f"Authentication failed: {response.status} - {error_text}")
                    raise TradovateAuthError(f"Authentication failed: {response.status} - {error_text}")
        except Exception as e:
            logger.error(f"Error during authentication: {e}")
            raise TradovateAuthError(f"Error during authentication: {e}")

    async def get_md_token(self) -> str:
        if not self.access_token:
            await self.authenticate()
        assert self.md_access_token is not None, "md_access_token should not be None after authentication"
        return self.md_access_token

    async def renew_token(self) -> str:
        if self.session is None:
            self.session = aiohttp.ClientSession()
        assert self.session is not None

        if not self.access_token:
            return await self.authenticate()

        headers = {
            "Authorization": f"Bearer {self.access_token}"
        }

        logger.debug("Renewing Tradovate access token")

        try:
            async with self.session.post(
                f"{self.settings.tradovate_base_url}/auth/renewaccesstoken",
                headers=headers
            ) as response:
                if response.status == 200:
                    data = await response.json()
                    access_token = data.get("accessToken")
                    md_access_token = data.get("mdAccessToken")
                    if access_token is None:
                        raise TradovateAuthError("accessToken missing in response")
                    self.access_token = access_token
                    if md_access_token is None:
                        raise TradovateAuthError("mdAccessToken missing in response")
                    self.md_access_token = md_access_token
                    expires_in = data.get("expiresIn", 1800)
                    self.expiration_time = time.time() + expires_in
                    logger.info("Successfully renewed Tradovate access token")
                    assert self.access_token is not None, "access_token should not be None after renewal"
                    return self.access_token
                else:
                    error_text = await response.text()
                    logger.error(f"Token renewal failed: {response.status} - {error_text}")
                    # If renewal fails, try full authentication
                    return await self.authenticate()
        except Exception as e:
            logger.error(f"Error during token renewal: {e}")
            # If renewal fails, try full authentication
            return await self.authenticate()

    async def is_token_valid(self) -> bool:
        return self.expiration_time - time.time() > 300  # 5 minutes

    async def ensure_valid_token(self) -> str:
        if not await self.is_token_valid():
            await self.renew_token()
        assert self.access_token is not None, "access_token should not be None after ensure_valid_token"
        return self.access_token

    async def get_account_info(self) -> dict:
        if self.session is None:
            self.session = aiohttp.ClientSession()
        assert self.session is not None

        token = await self.ensure_valid_token()
        assert token is not None, "Token should not be None after ensure_valid_token"
        headers = {"Authorization": f"Bearer {token}"}

        logger.debug("Fetching account info from Tradovate")

        try:
            async with self.session.get(
                f"{self.settings.tradovate_base_url}/account/list",
                headers=headers
            ) as response:
                if response.status == 200:
                    data = await response.json()
                    for account in data:
                        if account.get("status") == "Active":
                            logger.info(f"Found active account: {account.get('id')}")
                            return account
                    raise TradovateError("No active account found")
                else:
                    error_text = await response.text()
                    logger.error(f"Failed to get account info: {response.status} - {error_text}")
                    raise TradovateError(f"Failed to get account info: {response.status} - {error_text}")
        except Exception as e:
            logger.error(f"Error getting account info: {e}")
            raise TradovateError(f"Error getting account info: {e}")

    async def close(self):
        if self.session:
            await self.session.close()
            self.session = None
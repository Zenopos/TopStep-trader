import aiohttp
import asyncio
from typing import Dict, List, Optional
from loguru import logger

from data_ingestion.auth_client import TradovateAuthClient, TradovateError
from config.settings import Settings


class TradovateAPIError(TradovateError):
    """Exception for Tradovate API errors."""
    pass


class TradovateRESTClient:
    def __init__(self, auth: TradovateAuthClient, settings: Settings):
        self.auth = auth
        self.settings = settings
        self.session: Optional[aiohttp.ClientSession] = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession()
        return self.session

    async def _get(self, endpoint: str) -> dict:
        session = await self._get_session()
        token = await self.auth.ensure_valid_token()
        headers = {"Authorization": f"Bearer {token}"}

        logger.debug(f"GET request to {endpoint}")

        try:
            async with session.get(
                f"{self.settings.tradovate_base_url}{endpoint}",
                headers=headers
            ) as response:
                if response.status == 200:
                    return await response.json()
                elif response.status == 401:
                    # Token might be expired, re-authenticate and retry once
                    logger.warning("Received 401, re-authenticating")
                    await self.auth.renew_token()
                    token = await self.auth.ensure_valid_token()
                    headers = {"Authorization": f"Bearer {token}"}
                    async with session.get(
                        f"{self.settings.tradovate_base_url}{endpoint}",
                        headers=headers
                    ) as retry_response:
                        if retry_response.status == 200:
                            return await retry_response.json()
                        else:
                            error_text = await retry_response.text()
                            logger.error(f"GET request failed after retry: {retry_response.status} - {error_text}")
                            raise TradovateAPIError(f"GET request failed: {retry_response.status} - {error_text}")
                else:
                    error_text = await response.text()
                    logger.error(f"GET request failed: {response.status} - {error_text}")
                    raise TradovateAPIError(f"GET request failed: {response.status} - {error_text}")
        except Exception as e:
            logger.error(f"Error during GET request: {e}")
            raise TradovateAPIError(f"Error during GET request: {e}")

    async def _post(self, endpoint: str, payload: dict) -> dict:
        session = await self._get_session()
        token = await self.auth.ensure_valid_token()
        headers = {"Authorization": f"Bearer {token}"}

        logger.debug(f"POST request to {endpoint} with payload size: {len(str(payload))}")

        try:
            async with session.post(
                f"{self.settings.tradovate_base_url}{endpoint}",
                json=payload,
                headers=headers
            ) as response:
                if response.status == 200:
                    return await response.json()
                elif response.status == 401:
                    # Token might be expired, re-authenticate and retry once
                    logger.warning("Received 401, re-authenticating")
                    await self.auth.renew_token()
                    token = await self.auth.ensure_valid_token()
                    headers = {"Authorization": f"Bearer {token}"}
                    async with session.post(
                        f"{self.settings.tradovate_base_url}{endpoint}",
                        json=payload,
                        headers=headers
                    ) as retry_response:
                        if retry_response.status == 200:
                            return await retry_response.json()
                        else:
                            error_text = await retry_response.text()
                            logger.error(f"POST request failed after retry: {retry_response.status} - {error_text}")
                            raise TradovateAPIError(f"POST request failed: {retry_response.status} - {error_text}")
                else:
                    error_text = await response.text()
                    logger.error(f"POST request failed: {response.status} - {error_text}")
                    raise TradovateAPIError(f"POST request failed: {response.status} - {error_text}")
        except Exception as e:
            logger.error(f"Error during POST request: {e}")
            raise TradovateAPIError(f"Error during POST request: {e}")

    async def get_contract_details(self, symbol: str) -> dict:
        endpoint = f"/contract/find?name={symbol}"
        return await self._get(endpoint)

    async def get_account_info(self) -> dict:
        """Get account information.
        
        Returns:
            A dictionary containing account information including:
            - id: Account ID
            - name: Account name
            - readonly: Whether the account is readonly
        """
        endpoint = "/account/list"
        data = await self._get(endpoint)
        # Return the first account in the list
        if isinstance(data, list) and len(data) > 0:
            return data[0]
        return {}

    async def get_account_balance(self) -> float:
        endpoint = "/cashBalance/getCashBalanceSnapshot"
        data = await self._get(endpoint)
        realized_pnl = data.get("realizedPnL", 0.0)
        open_trade_equity = data.get("openTradeEquity", 0.0)
        return float(realized_pnl + open_trade_equity)
    
    async def get_open_positions(self) -> list[dict]:
        endpoint = "/position/list"
        data = await self._get(endpoint)
        # Filter out positions with netPos == 0
        return [pos for pos in data if pos.get("netPos", 0) != 0]

    async def close(self):
        if self.session and not self.session.closed:
            await self.session.close()
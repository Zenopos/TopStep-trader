from pydantic import BaseSettings, validator
from typing import Literal


class Settings(BaseSettings):
    # Tradovate API credentials
    TRADOVATE_USERNAME: str
    TRADOVATE_PASSWORD: str
    TRADOVATE_APP_ID: str
    TRADOVATE_APP_VERSION: str
    TRADOVATE_CID: str
    TRADOVATE_SECRET: str
    TRADOVATE_ENV: Literal["demo", "live"] = "demo"
    
    # Trading parameters
    SYMBOL: str = "NQ"
    TOPSTEP_DAILY_LOSS_LIMIT: float = 500.0
    TOPSTEP_MAX_CONTRACTS: int = 5
    HEARTBEAT_TIMEOUT_MS: int = 30000
    
    # Notification settings
    TELEGRAM_BOT_TOKEN: str = ""
    TELEGRAM_CHAT_ID: str = ""
    
    @validator('TRADOVATE_ENV')
    def validate_tradovate_env(cls, v):
        if v not in ["demo", "live"]:
            raise ValueError('TRADOVATE_ENV must be either "demo" or "live"')
        return v
    
    class Config:
        env_file = ".env"
        env_file_encoding = 'utf-8'


settings = Settings()
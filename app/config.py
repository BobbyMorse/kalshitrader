import os
from functools import lru_cache
from typing import Optional
from pydantic_settings import BaseSettings

# Resolve .env from project root regardless of where uvicorn is launched from
_HERE = os.path.dirname(os.path.abspath(__file__))
_ENV_FILE = os.path.join(_HERE, "..", ".env")


class Settings(BaseSettings):
    # Kalshi API
    kalshi_api_key_id: Optional[str] = None
    kalshi_private_key_path: str = "./private_key.pem"
    kalshi_private_key_content: Optional[str] = None  # PEM string for cloud deployments
    kalshi_email: Optional[str] = None
    kalshi_password: Optional[str] = None
    kalshi_demo: bool = True

    # Trading mode
    paper_trading: bool = True
    initial_bankroll: float = 2500.0

    # Strategy defaults (overridable from dashboard)
    scan_interval_seconds: int = 30
    min_edge: float = 0.04
    fee_buffer: float = 0.015
    slippage_buffer: float = 0.01
    max_contracts: int = 100
    max_exposure_pct: float = 0.05
    max_daily_drawdown_pct: float = 0.03

    @property
    def kalshi_host(self) -> str:
        return "https://demo-api.kalshi.co" if self.kalshi_demo else "https://api.elections.kalshi.com"

    @property
    def kalshi_api_prefix(self) -> str:
        return "/trade-api/v2"

    @property
    def has_credentials(self) -> bool:
        return bool(self.kalshi_api_key_id or (self.kalshi_email and self.kalshi_password))

    class Config:
        env_file = _ENV_FILE


@lru_cache()
def get_settings() -> Settings:
    return Settings()

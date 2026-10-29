import hashlib
import hmac
import time
from urllib.parse import urlencode

import pandas as pd
import requests

MAINNET_URL = "https://api.binance.com"
TESTNET_URL = "https://testnet.binance.vision"

INTERVAL_MS = {
    "1m": 60_000,
    "3m": 180_000,
    "5m": 300_000,
    "15m": 900_000,
    "30m": 1_800_000,
    "1h": 3_600_000,
    "2h": 7_200_000,
    "4h": 14_400_000,
    "1d": 86_400_000,
}


class BinanceError(Exception):
    pass


def sign(query: str, secret: str) -> str:
    return hmac.new(secret.encode(), query.encode(), hashlib.sha256).hexdigest()


def klines_to_frame(rows: list) -> pd.DataFrame:
    df = pd.DataFrame(
        [r[:7] for r in rows],
        columns=["open_time", "open", "high", "low", "close", "volume", "close_time"],
    )
    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = df[col].astype(float)
    return df


class BinanceClient:
    def __init__(self, api_key: str = "", api_secret: str = "", testnet: bool = True):
        self.base_url = TESTNET_URL if testnet else MAINNET_URL
        self.api_key = api_key
        self.api_secret = api_secret
        self.session = requests.Session()
        self.time_offset_ms = 0

    def _request(self, method: str, path: str, params: dict | None = None, signed: bool = False):
        params = dict(params or {})
        headers = {}
        query = urlencode(params)
        if signed:
            if not self.api_key or not self.api_secret:
                raise BinanceError("BINANCE_API_KEY e BINANCE_API_SECRET não configuradas (veja .env.example)")
            params["timestamp"] = self.now_ms()
            params["recvWindow"] = 5000
            query = urlencode(params)
            query += "&signature=" + sign(query, self.api_secret)
            headers["X-MBX-APIKEY"] = self.api_key

        url = self.base_url + path + ("?" + query if query else "")
        resp = self.session.request(method, url, headers=headers, timeout=15)
        try:
            data = resp.json()
        except ValueError:
            raise BinanceError(f"HTTP {resp.status_code}: {resp.text[:200]}")
        if resp.status_code != 200:
            raise BinanceError(f"HTTP {resp.status_code}: {data}")
        return data

    def now_ms(self) -> int:
        return int(time.time() * 1000) + self.time_offset_ms

    def sync_time(self) -> None:
        server = self._request("GET", "/api/v3/time")["serverTime"]
        self.time_offset_ms = server - int(time.time() * 1000)

    def symbol_info(self, symbol: str) -> dict:
        return self._request("GET", "/api/v3/exchangeInfo", {"symbol": symbol})["symbols"][0]

    def klines(self, symbol: str, interval: str, limit: int = 500, start_time: int | None = None) -> pd.DataFrame:
        params = {"symbol": symbol, "interval": interval, "limit": limit}
        if start_time is not None:
            params["startTime"] = start_time
        return klines_to_frame(self._request("GET", "/api/v3/klines", params))

    def history(self, symbol: str, interval: str, days: int) -> pd.DataFrame:
        step = INTERVAL_MS[interval]
        start = self.now_ms() - days * 86_400_000
        frames = []
        while True:
            df = self.klines(symbol, interval, limit=1000, start_time=start)
            if df.empty:
                break
            frames.append(df)
            start = int(df["open_time"].iloc[-1]) + step
            if len(df) < 1000:
                break
        if not frames:
            return klines_to_frame([])
        return pd.concat(frames, ignore_index=True).drop_duplicates("open_time").reset_index(drop=True)

    def balances(self) -> dict[str, float]:
        account = self._request("GET", "/api/v3/account", signed=True)
        return {b["asset"]: float(b["free"]) for b in account["balances"]}

    def market_buy(self, symbol: str, quote_amount: float) -> dict:
        params = {"symbol": symbol, "side": "BUY", "type": "MARKET", "quoteOrderQty": f"{quote_amount:.2f}"}
        return self._request("POST", "/api/v3/order", params, signed=True)

    def market_sell(self, symbol: str, quantity: str) -> dict:
        params = {"symbol": symbol, "side": "SELL", "type": "MARKET", "quantity": quantity}
        return self._request("POST", "/api/v3/order", params, signed=True)

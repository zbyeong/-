"""Public market-data loaders for the Crypto Fear & Greed Swing Lab."""
from __future__ import annotations

import time
from datetime import date, datetime, timezone
from typing import Iterable

import pandas as pd
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

BINANCE_FAPI = "https://fapi.binance.com"
FNG_API = "https://api.alternative.me/fng/"


class DataFetchError(RuntimeError):
    """Raised when a public market-data endpoint cannot be queried."""


def _session() -> requests.Session:
    session = requests.Session()
    retry = Retry(
        total=4,
        connect=4,
        read=4,
        backoff_factor=0.6,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset(["GET"]),
        respect_retry_after_header=True,
    )
    adapter = HTTPAdapter(max_retries=retry, pool_connections=4, pool_maxsize=4)
    session.mount("https://", adapter)
    session.headers.update({"User-Agent": "Crypto-FNG-Swing-Lab/1.0"})
    return session


def _request_json(session: requests.Session, url: str, params: dict | None = None):
    try:
        response = session.get(url, params=params, timeout=(8, 30))
        if response.status_code == 451:
            raise DataFetchError(
                "거래소 API가 현재 접속 지역 또는 네트워크에서 제한되었습니다. "
                "Streamlit Cloud에서 재시도하거나 다른 네트워크에서 실행해 주세요."
            )
        response.raise_for_status()
        payload = response.json()
        if isinstance(payload, dict) and payload.get("code"):
            raise DataFetchError(f"Binance API 오류: {payload.get('msg', payload)}")
        return payload
    except DataFetchError:
        raise
    except requests.RequestException as exc:
        raise DataFetchError(f"데이터 API 요청 실패: {exc}") from exc
    except ValueError as exc:
        raise DataFetchError("API 응답을 JSON으로 해석하지 못했습니다.") from exc


def fetch_binance_futures_klines(
    symbol: str,
    start_date: date,
    end_date: date,
    interval: str = "4h",
) -> pd.DataFrame:
    """Fetch USD-M perpetual OHLCV. Returned candle timestamps are UTC open times."""
    session = _session()
    start_ms = int(pd.Timestamp(start_date, tz="UTC").timestamp() * 1000)
    # Include the selected end date through its final millisecond.
    end_exclusive = pd.Timestamp(end_date, tz="UTC") + pd.Timedelta(days=1)
    end_ms = int(end_exclusive.timestamp() * 1000) - 1
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    end_ms = min(end_ms, now_ms)

    rows: list[list] = []
    cursor = start_ms
    while cursor <= end_ms:
        payload = _request_json(
            session,
            f"{BINANCE_FAPI}/fapi/v1/klines",
            {"symbol": symbol, "interval": interval, "startTime": cursor, "endTime": end_ms, "limit": 1000},
        )
        if not payload:
            break
        rows.extend(payload)
        last_open = int(payload[-1][0])
        next_cursor = last_open + 1
        if next_cursor <= cursor or len(payload) < 1000:
            break
        cursor = next_cursor
        time.sleep(0.12)

    columns = [
        "open_time", "open", "high", "low", "close", "volume", "close_time",
        "quote_volume", "trades", "taker_buy_base", "taker_buy_quote", "ignore",
    ]
    if not rows:
        raise DataFetchError(f"{symbol}: 선택한 기간의 4시간봉 데이터가 없습니다.")
    df = pd.DataFrame(rows, columns=columns)
    df["open_time"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
    df["close_time"] = pd.to_datetime(df["close_time"], unit="ms", utc=True)
    for col in ["open", "high", "low", "close", "volume", "quote_volume", "taker_buy_base", "taker_buy_quote"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df["trades"] = pd.to_numeric(df["trades"], errors="coerce")
    df = df.dropna(subset=["open_time", "open", "high", "low", "close", "volume"])
    # Do not allow the still-forming latest candle into signals or historical tests.
    now = pd.Timestamp.now(tz="UTC")
    df = df[df["close_time"] < now].copy()
    df = df.drop_duplicates(subset=["open_time"]).sort_values("open_time").reset_index(drop=True)
    if df.empty:
        raise DataFetchError(f"{symbol}: 완료된 캔들이 없습니다.")
    return df[["open_time", "open", "high", "low", "close", "volume", "close_time", "quote_volume", "trades"]]


def fetch_binance_funding_rates(
    symbol: str,
    start_date: date,
    end_date: date,
) -> pd.DataFrame:
    """Fetch historical funding events for USD-M perpetual contracts."""
    session = _session()
    start_ms = int(pd.Timestamp(start_date, tz="UTC").timestamp() * 1000)
    end_exclusive = pd.Timestamp(end_date, tz="UTC") + pd.Timedelta(days=1)
    end_ms = int(end_exclusive.timestamp() * 1000) - 1
    rows: list[dict] = []
    cursor = start_ms
    while cursor <= end_ms:
        payload = _request_json(
            session,
            f"{BINANCE_FAPI}/fapi/v1/fundingRate",
            {"symbol": symbol, "startTime": cursor, "endTime": end_ms, "limit": 1000},
        )
        if not payload:
            break
        rows.extend(payload)
        last_time = int(payload[-1]["fundingTime"])
        next_cursor = last_time + 1
        if next_cursor <= cursor or len(payload) < 1000:
            break
        cursor = next_cursor
        time.sleep(0.12)

    if not rows:
        return pd.DataFrame(columns=["funding_time", "funding_rate", "mark_price"])
    df = pd.DataFrame(rows)
    df["funding_time"] = pd.to_datetime(df["fundingTime"], unit="ms", utc=True)
    df["funding_rate"] = pd.to_numeric(df["fundingRate"], errors="coerce")
    df["mark_price"] = pd.to_numeric(df.get("markPrice"), errors="coerce")
    df = df.dropna(subset=["funding_time", "funding_rate"]).drop_duplicates("funding_time")
    return df[["funding_time", "funding_rate", "mark_price"]].sort_values("funding_time").reset_index(drop=True)


def fetch_fear_greed() -> pd.DataFrame:
    """Fetch all available daily Alternative.me Fear & Greed observations."""
    session = _session()
    payload = _request_json(session, FNG_API, {"limit": 0, "format": "json"})
    records = payload.get("data", []) if isinstance(payload, dict) else []
    if not records:
        raise DataFetchError("Alternative.me 공포·탐욕 지수 응답에 데이터가 없습니다.")
    df = pd.DataFrame(records)
    df["timestamp"] = pd.to_numeric(df["timestamp"], errors="coerce")
    df["value"] = pd.to_numeric(df["value"], errors="coerce")
    df["observed_at"] = pd.to_datetime(df["timestamp"], unit="s", utc=True)
    df["observed_date"] = df["observed_at"].dt.floor("D")
    df["effective_at"] = df["observed_date"] + pd.Timedelta(days=1)
    df = df.dropna(subset=["value", "effective_at"]).sort_values("observed_date")
    df = df.drop_duplicates("observed_date", keep="last").reset_index(drop=True)
    # Changes are computed only from observations already available at effective_at.
    df["fng_change_1d"] = df["value"].diff()
    df["fng_rising_2d"] = (df["fng_change_1d"] > 0) & (df["fng_change_1d"].shift(1) > 0)
    df["fng_min_3d"] = df["value"].rolling(3, min_periods=1).min()
    df["fng_recovery_5"] = (df["value"] - df["fng_min_3d"]) >= 5
    df["fng_min_7d"] = df["value"].rolling(7, min_periods=1).min()
    return df[[
        "observed_date", "effective_at", "value", "value_classification",
        "fng_change_1d", "fng_rising_2d", "fng_recovery_5", "fng_min_7d",
    ]]


def merge_fear_greed(bars: pd.DataFrame, fng: pd.DataFrame) -> pd.DataFrame:
    """As-of merge with a conservative one-day publication lag."""
    left = bars.sort_values("open_time").copy()
    right = fng.sort_values("effective_at").copy()
    merged = pd.merge_asof(
        left,
        right,
        left_on="open_time",
        right_on="effective_at",
        direction="backward",
        allow_exact_matches=True,
    )
    return merged

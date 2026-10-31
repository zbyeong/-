"""Indicators, three long-only swing strategies, and a conservative backtester."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import numpy as np
import pandas as pd

from data_loader import merge_fear_greed


STRATEGY_NAMES = {
    "S1": "공포 필터 + 추세 전환",
    "S2": "극단적 공포 + 반등 확인",
    "S3": "탐욕 구간 + 고점 돌파",
}


def _rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    rs = gain / loss.replace(0, np.nan)
    out = 100 - (100 / (1 + rs))
    return out.where(loss.ne(0), 100.0)


def _adx(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> pd.Series:
    """Wilder-style ADX calculated from daily OHLC data."""
    up_move = high.diff()
    down_move = -low.diff()
    plus_dm = up_move.where((up_move > down_move) & (up_move > 0), 0.0)
    minus_dm = down_move.where((down_move > up_move) & (down_move > 0), 0.0)
    previous_close = close.shift(1)
    true_range = pd.concat(
        [high - low, (high - previous_close).abs(), (low - previous_close).abs()],
        axis=1,
    ).max(axis=1)
    smooth = lambda series: series.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    atr = smooth(true_range)
    plus_di = 100.0 * smooth(plus_dm) / atr.replace(0, np.nan)
    minus_di = 100.0 * smooth(minus_dm) / atr.replace(0, np.nan)
    denominator = (plus_di + minus_di).replace(0, np.nan)
    dx = 100.0 * (plus_di - minus_di).abs() / denominator
    return smooth(dx)


def build_market_regime(btc_bars: pd.DataFrame) -> pd.DataFrame:
    """Classify the broad crypto market from completed BTC daily candles.

    A day's regime becomes available at 00:00 UTC on the following day,
    preventing an unfinished daily candle from leaking into 4h decisions.
    """
    required = {"open_time", "open", "high", "low", "close", "volume"}
    missing = required.difference(btc_bars.columns)
    if missing:
        raise ValueError(f"BTC OHLCV 데이터에 필요한 열이 없습니다: {sorted(missing)}")
    source = btc_bars.sort_values("open_time").copy()
    daily = source.set_index("open_time").resample("1D", label="left", closed="left").agg(
        open=("open", "first"), high=("high", "max"), low=("low", "min"),
        close=("close", "last"), volume=("volume", "sum"),
    ).dropna(subset=["close"])
    daily["ema50"] = daily["close"].ewm(span=50, adjust=False, min_periods=50).mean()
    daily["ema200"] = daily["close"].ewm(span=200, adjust=False, min_periods=200).mean()
    daily["ema50_slope20_pct"] = (daily["ema50"] / daily["ema50"].shift(20) - 1.0) * 100.0
    daily["adx14"] = _adx(daily["high"], daily["low"], daily["close"], 14)
    previous_close = daily["close"].shift(1)
    daily_tr = pd.concat([
        daily["high"] - daily["low"],
        (daily["high"] - previous_close).abs(),
        (daily["low"] - previous_close).abs(),
    ], axis=1).max(axis=1)
    daily_atr = daily_tr.ewm(alpha=1 / 14, adjust=False, min_periods=14).mean()
    daily["atr_pct"] = daily_atr / daily["close"] * 100.0

    valid = daily[["ema50", "ema200", "ema50_slope20_pct", "adx14"]].notna().all(axis=1)
    bullish = (
        valid & (daily["close"] > daily["ema200"])
        & (daily["ema50"] > daily["ema200"])
        & (daily["ema50_slope20_pct"] > 0) & (daily["adx14"] >= 18)
    )
    bearish = (
        valid & (daily["close"] < daily["ema200"])
        & (daily["ema50"] < daily["ema200"])
        & (daily["ema50_slope20_pct"] < 0) & (daily["adx14"] >= 18)
    )
    daily["market_regime"] = np.select(
        [bullish, bearish, valid],
        ["상승장", "하락장", "횡보/전환"],
        default="데이터 부족",
    )
    daily["known_at"] = daily.index + pd.Timedelta(days=1)
    daily = daily.reset_index().rename(columns={"open_time": "regime_date"})
    return daily[[
        "regime_date", "known_at", "market_regime", "close", "ema50", "ema200",
        "ema50_slope20_pct", "adx14", "atr_pct",
    ]].rename(columns={
        "close": "regime_btc_close", "ema50": "regime_ema50",
        "ema200": "regime_ema200", "adx14": "regime_adx14",
        "atr_pct": "regime_atr_pct",
    })


def attach_market_regime(bars: pd.DataFrame, regime_daily: pd.DataFrame) -> pd.DataFrame:
    """Attach the latest already-known BTC regime to each 4h candle."""
    left = bars.sort_values("open_time").copy()
    right = regime_daily.sort_values("known_at").copy()
    merged = pd.merge_asof(
        left, right, left_on="open_time", right_on="known_at",
        direction="backward", allow_exact_matches=True,
    )
    merged["market_regime"] = merged["market_regime"].fillna("데이터 부족")
    return merged


def regime_filter_mask(data: pd.DataFrame, strategy: str) -> pd.Series:
    """Return the regime-compatible signal mask for each strategy."""
    if "market_regime" not in data.columns:
        return pd.Series(True, index=data.index, dtype=bool)
    regime = data["market_regime"].fillna("데이터 부족")
    if strategy in {"S1", "S3"}:
        return regime.eq("상승장")
    if strategy == "S2":
        return ~regime.isin(["하락장", "데이터 부족"])
    raise ValueError(f"알 수 없는 전략: {strategy}")


def add_indicators(
    bars: pd.DataFrame,
    fng: pd.DataFrame,
    symbol: str,
    btc_bars: Optional[pd.DataFrame] = None,
) -> pd.DataFrame:
    """Add 4h indicators and lagged, completed-day indicators without look-ahead."""
    df = bars.sort_values("open_time").copy().reset_index(drop=True)
    df["ema20"] = df["close"].ewm(span=20, adjust=False, min_periods=20).mean()
    df["ema50"] = df["close"].ewm(span=50, adjust=False, min_periods=50).mean()
    df["rsi14"] = _rsi(df["close"], 14)
    prev_close = df["close"].shift(1)
    tr = pd.concat([
        df["high"] - df["low"],
        (df["high"] - prev_close).abs(),
        (df["low"] - prev_close).abs(),
    ], axis=1).max(axis=1)
    df["atr14"] = tr.ewm(alpha=1 / 14, adjust=False, min_periods=14).mean()
    df["volume_sma20"] = df["volume"].rolling(20, min_periods=20).mean()
    df["prior_high20"] = df["high"].rolling(20, min_periods=20).max().shift(1)
    df["prior_low12"] = df["low"].rolling(12, min_periods=12).min().shift(1)

    # Daily trend indicators are shifted one whole UTC day before being mapped to 4h bars.
    daily = df.set_index("open_time").resample("1D", label="left", closed="left").agg(
        open=("open", "first"), high=("high", "max"), low=("low", "min"),
        close=("close", "last"), volume=("volume", "sum"),
    ).dropna(subset=["close"])
    daily["daily_ema200_prev"] = daily["close"].ewm(span=200, adjust=False, min_periods=200).mean().shift(1)
    daily["daily_ema20_prev"] = daily["close"].ewm(span=20, adjust=False, min_periods=20).mean().shift(1)
    day_key = df["open_time"].dt.floor("D")
    df["daily_ema200"] = day_key.map(daily["daily_ema200_prev"])
    df["daily_ema20"] = day_key.map(daily["daily_ema20_prev"])

    if symbol == "ETHUSDT" and btc_bars is not None and not btc_bars.empty:
        eth_daily = daily["close"].copy()
        btc_daily = btc_bars.set_index("open_time").resample("1D", label="left", closed="left")["close"].last()
        relative = (eth_daily / btc_daily).dropna()
        relative_ema = relative.ewm(span=20, adjust=False, min_periods=20).mean().shift(1)
        df["ethbtc_ratio"] = day_key.map(relative)
        df["ethbtc_ema20"] = day_key.map(relative_ema)
    else:
        df["ethbtc_ratio"] = np.nan
        df["ethbtc_ema20"] = np.nan

    df = merge_fear_greed(df, fng)
    df["fng_value"] = pd.to_numeric(df["value"], errors="coerce")
    df["fng_min_7d"] = pd.to_numeric(df["fng_min_7d"], errors="coerce")
    df["fng_rising_2d"] = df["fng_rising_2d"].astype("boolean").fillna(False).astype(bool)
    df["fng_recovery_5"] = df["fng_recovery_5"].astype("boolean").fillna(False).astype(bool)

    # Crosses are evaluated on completed candles. Orders are filled at the next candle open.
    df["ema_bull_cross"] = (df["ema20"] > df["ema50"]) & (df["ema20"].shift(1) <= df["ema50"].shift(1))
    df["ema_bear_cross"] = (df["ema20"] < df["ema50"]) & (df["ema20"].shift(1) >= df["ema50"].shift(1))
    df["rsi_cross_50"] = (df["rsi14"] > 50) & (df["rsi14"].shift(1) <= 50)
    df["close_cross_ema20"] = (df["close"] > df["ema20"]) & (df["close"].shift(1) <= df["ema20"].shift(1))

    # Strategy 1: daily trend regime + 4h bullish EMA cross + sentiment filter.
    df["sig_s1"] = (
        (df["close"] > df["daily_ema200"])
        & df["ema_bull_cross"]
        & (df["rsi14"] > 50)
        & (df["fng_value"] > 25)
    ).fillna(False)

    # Strategy 2: a recent extreme-fear reading, sentiment recovery, and price confirmation.
    df["sig_s2"] = (
        (df["fng_min_7d"] <= 25)
        & df["fng_rising_2d"]
        & df["fng_recovery_5"]
        & df["close_cross_ema20"]
        & df["rsi_cross_50"]
        & (df["low"] > df["prior_low12"])
    ).fillna(False)

    # Strategy 3: rising greed, daily bull regime, 20-bar breakout and volume confirmation.
    eth_relative_ok = True
    if symbol == "ETHUSDT":
        eth_relative_ok = (df["ethbtc_ratio"] > df["ethbtc_ema20"]).fillna(False)
    df["sig_s3"] = (
        (df["fng_value"] >= 55)
        & df["fng_rising_2d"]
        & (df["close"] > df["daily_ema200"])
        & (df["close"] > df["prior_high20"])
        & (df["volume"] >= df["volume_sma20"])
        & eth_relative_ok
    ).fillna(False)

    return df


@dataclass
class BacktestConfig:
    initial_capital: float = 10_000.0
    risk_per_trade: float = 0.005
    max_leverage: float = 5.0
    fee_rate: float = 0.0005       # per side, 0.05%
    slippage_rate: float = 0.0005  # per side, 0.05%
    s1_atr_multiple: float = 2.0
    s2_stop_atr: float = 1.5
    s2_target_r: float = 1.5
    s2_max_bars: int = 42
    s3_stop_atr: float = 2.0
    s3_max_bars: int = 84
    use_regime_filter: bool = True


def _funding_events_in_window(funding: pd.DataFrame, start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame:
    if funding is None or funding.empty:
        return pd.DataFrame(columns=["funding_time", "funding_rate", "mark_price"])
    return funding[(funding["funding_time"] > start) & (funding["funding_time"] <= end)]


def run_backtest(
    df: pd.DataFrame,
    strategy: str,
    funding: pd.DataFrame | None = None,
    config: BacktestConfig | None = None,
    trade_start: pd.Timestamp | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Sequential long-only backtest. Signal at close; fills at next open.

    Stop gaps are filled at the worse opening price. If stop and target are both
    touched in one candle, the stop is assumed to have occurred first.
    """
    cfg = config or BacktestConfig()
    signal_col = f"sig_{strategy.lower()}"
    if signal_col not in df.columns:
        raise ValueError(f"알 수 없는 전략: {strategy}")
    data = df.sort_values("open_time").reset_index(drop=True).copy()
    signal_values = data[signal_col].fillna(False).astype(bool)
    if cfg.use_regime_filter:
        signal_values = signal_values & regime_filter_mask(data, strategy)
    if len(data) < 5:
        return pd.DataFrame(), pd.DataFrame()
    funding = funding if funding is not None else pd.DataFrame()
    equity = float(cfg.initial_capital)
    equity_rows: list[dict] = []
    trades: list[dict] = []
    position = None
    pending_entry = None

    start_idx = 0
    if trade_start is not None:
        trade_start = pd.Timestamp(trade_start)
        if trade_start.tzinfo is None:
            trade_start = trade_start.tz_localize("UTC")
        else:
            trade_start = trade_start.tz_convert("UTC")
        matches = np.flatnonzero(data["open_time"].values >= trade_start.to_datetime64())
        start_idx = int(matches[0]) if len(matches) else len(data)

    def close_position(exit_time, exit_price, reason, bar_idx):
        nonlocal position, equity
        if position is None:
            return
        # Exit slippage is adverse for a long.
        fill = float(exit_price) * (1 - cfg.slippage_rate)
        qty = float(position["qty"])
        gross = (fill - position["entry_price"]) * qty
        exit_fee = fill * qty * cfg.fee_rate
        position["realized_gross"] += gross
        position["fees"] += exit_fee
        net = position["realized_gross"] - position["fees"] - position["funding_cost"]
        equity_before = position["equity_before"]
        equity += net
        trades.append({
            "strategy": STRATEGY_NAMES[strategy],
            "entry_time": position["entry_time"],
            "exit_time": exit_time,
            "entry_price": position["entry_price"],
            "exit_price_last": fill,
            "initial_stop": position["initial_stop"],
            "initial_risk_pct": position["risk_pct"],
            "notional": position["notional"],
            "leverage_used": position["notional"] / max(position["equity_before"], 1e-12),
            "gross_pnl": position["realized_gross"],
            "fees": position["fees"],
            "funding_cost": position["funding_cost"],
            "net_pnl": net,
            "return_on_equity_pct": net / max(equity_before, 1e-12) * 100,
            "return_on_notional_pct": net / max(position["notional"], 1e-12) * 100,
            "bars_held": int(bar_idx - position["entry_idx"]),
            "exit_reason": reason,
            "equity_after": equity,
        })
        position = None

    for i in range(start_idx, len(data)):
        row = data.iloc[i]
        # Execute a signal generated on the previous fully closed candle at this candle's open.
        if pending_entry is not None and position is None:
            signal_idx = pending_entry
            sig = data.iloc[signal_idx]
            entry_raw = float(row["open"])
            entry_price = entry_raw * (1 + cfg.slippage_rate)
            atr = float(sig["atr14"]) if pd.notna(sig["atr14"]) else np.nan
            if strategy == "S2":
                low_ref = float(sig["prior_low12"]) if pd.notna(sig["prior_low12"]) else np.nan
                stop = low_ref - cfg.s2_stop_atr * atr if np.isfinite(low_ref) and np.isfinite(atr) else np.nan
            elif strategy == "S1":
                stop = entry_price - cfg.s1_atr_multiple * atr if np.isfinite(atr) else np.nan
            else:
                stop = entry_price - cfg.s3_stop_atr * atr if np.isfinite(atr) else np.nan
            risk_per_unit = entry_price - stop if np.isfinite(stop) else np.nan
            risk_pct = risk_per_unit / entry_price if np.isfinite(risk_per_unit) and entry_price > 0 else np.nan
            if np.isfinite(risk_pct) and risk_pct > 0 and risk_pct <= 0.15 and equity > 0:
                risk_budget = equity * cfg.risk_per_trade
                notional = min(risk_budget / risk_pct, equity * cfg.max_leverage)
                qty = notional / entry_price
                entry_fee = notional * cfg.fee_rate
                position = {
                    "entry_idx": i,
                    "entry_time": row["open_time"],
                    "entry_price": entry_price,
                    "initial_stop": stop,
                    "stop": stop,
                    "qty": qty,
                    "qty_initial": qty,
                    "notional": notional,
                    "risk_pct": risk_pct,
                    "equity_before": equity,
                    "realized_gross": 0.0,
                    "fees": entry_fee,
                    "funding_cost": 0.0,
                    "target": entry_price + cfg.s2_target_r * risk_per_unit if strategy == "S2" else np.nan,
                    "partial_done": False,
                    "highest_high": float(row["high"]),
                    "entry_fng": float(sig["fng_value"]) if pd.notna(sig["fng_value"]) else np.nan,
            "entry_market_regime": str(sig.get("market_regime", "미분류")),
                }
            pending_entry = None

        if position is not None:
            previous_time = data.iloc[i - 1]["open_time"] if i > 0 else position["entry_time"]
            funding_start = max(previous_time, position["entry_time"])
            events = _funding_events_in_window(funding, funding_start, row["open_time"])
            if not events.empty:
                for ev in events.itertuples(index=False):
                    mark = float(ev.mark_price) if pd.notna(ev.mark_price) and float(ev.mark_price) > 0 else float(row["close"])
                    # Positive funding means longs pay shorts; negative is a credit.
                    position["funding_cost"] += position["qty"] * mark * float(ev.funding_rate)

            # Use only information known before this candle to update trailing stops.
            stop = float(position["stop"])
            exit_reason = None
            exit_price = None

            # Strategy 1: a two-ATR chandelier-style trailing stop based on prior highs/ATR.
            if strategy == "S1":
                atr_prev = float(data.iloc[i - 1]["atr14"]) if i > position["entry_idx"] and pd.notna(data.iloc[i - 1]["atr14"]) else np.nan
                if np.isfinite(atr_prev):
                    candidate = position["highest_high"] - cfg.s1_atr_multiple * atr_prev
                    position["stop"] = max(stop, candidate)
                    stop = float(position["stop"])
            elif strategy == "S2" and position["partial_done"]:
                atr_prev = float(data.iloc[i - 1]["atr14"]) if i > position["entry_idx"] and pd.notna(data.iloc[i - 1]["atr14"]) else np.nan
                if np.isfinite(atr_prev):
                    trail = position["highest_high"] - 2.0 * atr_prev
                    position["stop"] = max(position["stop"], trail)
                    stop = float(position["stop"])

            # Strategy 2: stop first in an ambiguous candle, 50% take profit at 1.5R,
            # then a two-ATR trailing stop; hard time exit after seven days.
            if float(row["low"]) <= stop:
                exit_reason = "손절/추적손절"
                exit_price = min(float(row["open"]), stop) if float(row["open"]) < stop else stop
            elif strategy == "S2" and not position["partial_done"] and float(row["high"]) >= float(position["target"]):
                target_fill = float(position["target"]) * (1 - cfg.slippage_rate)
                sold_qty = position["qty"] * 0.5
                position["realized_gross"] += (target_fill - position["entry_price"]) * sold_qty
                position["fees"] += target_fill * sold_qty * cfg.fee_rate
                position["qty"] -= sold_qty
                position["partial_done"] = True
                position["stop"] = max(position["stop"], position["entry_price"])
                # After the partial target, conservatively assume a same-candle
                # return through breakeven closes the remaining half.
                if float(row["low"]) <= float(position["stop"]):
                    exit_reason = "1차 익절 후 본전 손절"
                    exit_price = min(float(row["open"]), float(position["stop"])) if float(row["open"]) < float(position["stop"]) else float(position["stop"])
            if exit_reason is None and strategy == "S1" and bool(row["ema_bear_cross"]):
                exit_reason = "EMA 데드크로스"
                exit_price = float(row["close"])
            if exit_reason is None and strategy == "S3" and float(row["close"]) < float(row["ema20"]):
                exit_reason = "EMA20 종가 이탈"
                exit_price = float(row["close"])
            if exit_reason is None and strategy == "S2" and (i - position["entry_idx"]) >= cfg.s2_max_bars:
                exit_reason = "최대 보유기간"
                exit_price = float(row["close"])
            if exit_reason is None and strategy == "S3" and (i - position["entry_idx"]) >= cfg.s3_max_bars:
                exit_reason = "최대 보유기간"
                exit_price = float(row["close"])
            if exit_reason is not None:
                close_position(row["open_time"], exit_price, exit_reason, i)
            elif position is not None:
                # Current candle high becomes available only after its stop checks.
                position["highest_high"] = max(position["highest_high"], float(row["high"]))

        # Signals on this candle are eligible for execution on the next candle only.
        if position is None and pending_entry is None and i < len(data) - 1:
            if bool(signal_values.iloc[i]):
                pending_entry = i

        # Mark-to-market equity for drawdown and daily Sharpe calculations.
        unrealized = 0.0
        if position is not None:
            unrealized = position["realized_gross"] + (float(row["close"]) - position["entry_price"]) * position["qty"]
            unrealized -= position["fees"] + position["funding_cost"]
        equity_rows.append({"time": row["open_time"], "equity": equity + unrealized})

    if position is not None:
        last = data.iloc[-1]
        close_position(last["open_time"], float(last["close"]), "데이터 종료", len(data) - 1)
        if equity_rows:
            equity_rows[-1]["equity"] = equity

    trades_df = pd.DataFrame(trades)
    equity_df = pd.DataFrame(equity_rows)
    return trades_df, equity_df


def summarize_backtest(trades: pd.DataFrame, equity_curve: pd.DataFrame, initial_capital: float) -> dict:
    if equity_curve is None or equity_curve.empty:
        return {
            "trades": 0, "total_return_pct": 0.0, "cagr_pct": 0.0,
            "mdd_pct": 0.0, "win_rate_pct": 0.0, "profit_factor": np.nan,
            "avg_trade_pct": 0.0, "sharpe": np.nan, "sortino": np.nan,
            "final_equity": initial_capital,
        }
    eq = equity_curve.copy()
    eq["time"] = pd.to_datetime(eq["time"], utc=True)
    eq = eq.drop_duplicates("time").set_index("time").sort_index()
    daily = eq["equity"].resample("1D").last().dropna()
    returns = daily.pct_change().dropna()
    final_equity = float(eq["equity"].iloc[-1])
    total_return = (final_equity / initial_capital - 1) * 100
    days = max((eq.index[-1] - eq.index[0]).total_seconds() / 86400, 1)
    cagr = ((final_equity / initial_capital) ** (365.25 / days) - 1) * 100 if final_equity > 0 else -100.0
    peak = eq["equity"].cummax()
    drawdown = eq["equity"] / peak - 1
    mdd = float(drawdown.min() * 100)
    if trades is not None and not trades.empty:
        wins = trades["net_pnl"] > 0
        gross_profit = float(trades.loc[trades["net_pnl"] > 0, "net_pnl"].sum())
        gross_loss = abs(float(trades.loc[trades["net_pnl"] < 0, "net_pnl"].sum()))
        pf = gross_profit / gross_loss if gross_loss > 0 else np.inf if gross_profit > 0 else np.nan
        win_rate = float(wins.mean() * 100)
        avg_trade = float(trades["return_on_equity_pct"].mean())
    else:
        pf, win_rate, avg_trade = np.nan, 0.0, 0.0
    sharpe = float(np.sqrt(365) * returns.mean() / returns.std(ddof=1)) if len(returns) > 1 and returns.std(ddof=1) > 0 else np.nan
    downside = returns[returns < 0]
    sortino = float(np.sqrt(365) * returns.mean() / downside.std(ddof=1)) if len(downside) > 1 and downside.std(ddof=1) > 0 else np.nan
    return {
        "trades": int(len(trades)) if trades is not None else 0,
        "total_return_pct": total_return,
        "cagr_pct": cagr,
        "mdd_pct": mdd,
        "win_rate_pct": win_rate,
        "profit_factor": pf,
        "avg_trade_pct": avg_trade,
        "sharpe": sharpe,
        "sortino": sortino,
        "final_equity": final_equity,
    }

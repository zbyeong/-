from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from alerts import get_telegram_credentials, send_telegram_message, TelegramAlertError
from data_loader import DataFetchError, fetch_binance_funding_rates, fetch_binance_futures_klines, fetch_fear_greed
from strategy_engine import (
    BacktestConfig, STRATEGY_NAMES, add_indicators, attach_market_regime,
    build_market_regime, regime_filter_mask, run_backtest, summarize_backtest,
)

st.set_page_config(page_title="Crypto F&G Swing Lab", page_icon="📊", layout="wide")

st.markdown(
    """
    <style>
    .main .block-container {max-width: 1500px; padding-top: 1.4rem; padding-bottom: 2.5rem;}
    div[data-testid="stMetric"] {background: rgba(128,128,128,.07); padding: 12px 14px; border-radius: 10px;}
    .small-note {font-size: .83rem; color: #8a8a8a;}
    </style>
    """,
    unsafe_allow_html=True,
)

st.title("📊 Crypto Fear & Greed Swing Lab")
st.caption("BTC·ETH 스윙 전략 모니터링 / 백테스트 / 선물 포지션 위험 계산기")


@st.cache_data(ttl=900, show_spinner=False)
def cached_klines(symbol: str, start: date, end: date) -> pd.DataFrame:
    return fetch_binance_futures_klines(symbol, start, end, "4h")


@st.cache_data(ttl=3600, show_spinner=False)
def cached_funding(symbol: str, start: date, end: date) -> pd.DataFrame:
    return fetch_binance_funding_rates(symbol, start, end)


@st.cache_data(ttl=3600, show_spinner=False)
def cached_fng() -> pd.DataFrame:
    return fetch_fear_greed()


def fmt_pct(value, digits=2):
    return "—" if value is None or pd.isna(value) else f"{value:,.{digits}f}%"


def fmt_num(value, digits=2):
    return "—" if value is None or pd.isna(value) else f"{value:,.{digits}f}"


def make_price_chart(df: pd.DataFrame, title: str):
    view = df.tail(420).copy()
    fig = go.Figure()
    fig.add_trace(go.Candlestick(
        x=view["open_time"], open=view["open"], high=view["high"],
        low=view["low"], close=view["close"], name="4H 캔들",
    ))
    fig.add_trace(go.Scatter(x=view["open_time"], y=view["ema20"], name="EMA20", line=dict(width=1.2)))
    fig.add_trace(go.Scatter(x=view["open_time"], y=view["ema50"], name="EMA50", line=dict(width=1.2)))
    fig.update_layout(
        title=title, height=440, xaxis_rangeslider_visible=False,
        legend=dict(orientation="h", y=1.02, x=0), margin=dict(l=10, r=10, t=55, b=10),
    )
    return fig


def effective_signal_text(df: pd.DataFrame, row: pd.Series, strategy: str, use_filter: bool) -> str:
    raw_col = f"sig_{strategy.lower()}"
    if not bool(row.get(raw_col, False)):
        return "대기"
    if use_filter and not bool(regime_filter_mask(df, strategy).iloc[-1]):
        return "국면 필터 차단"
    return "진입 후보"


with st.sidebar:
    st.header("데이터 및 백테스트 설정")
    today_utc = pd.Timestamp.now(tz="UTC").date()
    default_start = date(max(2019, today_utc.year - 4), 1, 1)
    start_date = st.date_input("시작일 (UTC)", value=default_start, max_value=today_utc - timedelta(days=2))
    end_date = st.date_input("종료일 (UTC)", value=today_utc, min_value=start_date, max_value=today_utc)
    assets = st.multiselect("거래 대상", ["BTCUSDT", "ETHUSDT"], default=["BTCUSDT", "ETHUSDT"])
    st.divider()
    initial_capital = st.number_input("초기 자본 (USDT)", min_value=100.0, value=10_000.0, step=500.0)
    risk_pct = st.slider("거래당 계좌 위험 (%)", min_value=0.1, max_value=2.0, value=0.5, step=0.1) / 100
    leverage_cap = st.slider("최대 명목 레버리지 (배)", min_value=1.0, max_value=10.0, value=5.0, step=0.5)
    use_regime_filter = st.checkbox(
        "시장 국면 필터 적용", value=True,
        help="S1·S3는 상승장에서만, S2는 하락장이 아닐 때만 진입하도록 제한합니다.",
    )
    fee_pct = st.number_input("편도 수수료 (%)", min_value=0.0, max_value=0.2, value=0.05, step=0.01) / 100
    slippage_pct = st.number_input("편도 슬리피지 (%)", min_value=0.0, max_value=0.5, value=0.05, step=0.01) / 100
    st.caption("기본값: 편도 수수료 0.05% + 편도 슬리피지 0.05%. 실제 계정 등급·체결 환경에 맞게 조정하세요.")
    reload_data = st.button("🔄 데이터 새로고침", use_container_width=True)
    if reload_data:
        cached_klines.clear()
        cached_funding.clear()
        cached_fng.clear()
        st.rerun()

if not assets:
    st.info("왼쪽 메뉴에서 BTCUSDT 또는 ETHUSDT를 선택하세요.")
    st.stop()
if start_date >= end_date:
    st.error("종료일은 시작일보다 뒤여야 합니다.")
    st.stop()

try:
    with st.spinner("거래소 가격·펀딩비와 공포·탐욕 지수를 불러오는 중입니다..."):
        fng = cached_fng()
        raw_bars = {symbol: cached_klines(symbol, start_date, end_date) for symbol in set(assets + (["BTCUSDT"] if "ETHUSDT" in assets else []))}
        funding_data = {symbol: cached_funding(symbol, start_date, end_date) for symbol in assets}
        regime_daily = build_market_regime(raw_bars["BTCUSDT"])
        btc_regime_bars = attach_market_regime(raw_bars["BTCUSDT"], regime_daily)
        market = {}
        for symbol in assets:
            enriched = add_indicators(
                raw_bars[symbol], fng, symbol,
                btc_bars=raw_bars.get("BTCUSDT") if symbol == "ETHUSDT" else None,
            )
            market[symbol] = attach_market_regime(enriched, regime_daily)
except Exception as exc:
    st.error(f"데이터를 불러오지 못했습니다: {exc}")
    st.markdown("""가능한 원인: Binance API 지역 제한, 일시적인 API 장애, 기간이 너무 짧거나 선택한 종목의 과거 선물 데이터가 없는 경우입니다. 잠시 후 다시 시도하거나 기간을 줄여 주세요.""")
    st.stop()

latest_fng = fng.iloc[-1]
latest_fng_value = int(latest_fng["value"])
latest_fng_label = str(latest_fng["value_classification"])

st.markdown(
    f"<div class='small-note'>가격·펀딩비: Binance USDⓈ-M 무기한 선물 공개 API · 심리지표: "
    f"<a href='https://alternative.me/crypto/fear-and-greed-index/' target='_blank'>Alternative.me Crypto Fear & Greed Index</a> · "
    f"심리지표는 미래정보 방지를 위해 관측일 다음 UTC 날짜부터 적용합니다.</div>",
    unsafe_allow_html=True,
)

# Global market snapshot
k1, k2, k3, k4 = st.columns(4)
k1.metric("최근 공포·탐욕 지수", f"{latest_fng_value} / 100", latest_fng_label)
for col, symbol in zip([k2, k3], ["BTCUSDT", "ETHUSDT"]):
    if symbol in market:
        last = market[symbol].iloc[-1]
        col.metric(f"{symbol} 최근가", f"${last['close']:,.2f}", f"F&G {last['fng_value']:.0f}" if pd.notna(last['fng_value']) else "F&G 데이터 없음")
    else:
        col.metric(f"{symbol} 최근가", "미선택")
k4.metric("최근 지수 관측일", pd.Timestamp(latest_fng["observed_date"]).strftime("%Y-%m-%d"))


tab_dashboard, tab_backtest, tab_risk, tab_alerts, tab_method = st.tabs([
    "📡 실시간 신호", "🧪 백테스트", "🧮 포지션 계산", "🔔 텔레그램 알림", "📘 전략·데이터 설명"
])

with tab_dashboard:
    st.subheader("BTC 기준 시장 국면 자동 진단")
    regime_last = btc_regime_bars.iloc[-1]
    current_regime = str(regime_last.get("market_regime", "데이터 부족"))
    adx_value = regime_last.get("regime_adx14", np.nan)
    ema50_value = regime_last.get("regime_ema50", np.nan)
    ema200_value = regime_last.get("regime_ema200", np.nan)
    regime_guidance = {
        "상승장": "S1·S3 우선 검토. 추세가 유지되는 동안 눌림목과 돌파 신호를 확인합니다.",
        "하락장": "롱 신규 진입을 보수적으로 제한합니다. 반등 신호가 있어도 추세 전환 확인 전까지 관망이 기본입니다.",
        "횡보/전환": "돌파 추격을 줄이고 S2의 반등 확인 신호를 우선 관찰합니다. 거래당 위험을 낮추는 편이 안전합니다.",
        "데이터 부족": "일봉 EMA200 워밍업 데이터가 부족합니다. 분석 시작일을 더 과거로 설정하세요.",
    }.get(current_regime, "시장 국면 데이터 확인이 필요합니다.")
    r1, r2, r3, r4 = st.columns(4)
    r1.metric("시장 국면", current_regime)
    r2.metric("일봉 ADX(14)", fmt_num(adx_value, 1))
    r3.metric("일봉 EMA50", f"${ema50_value:,.0f}" if pd.notna(ema50_value) else "—")
    r4.metric("일봉 EMA200", f"${ema200_value:,.0f}" if pd.notna(ema200_value) else "—")
    st.info(regime_guidance + " 국면 분류는 BTC 일봉 기준이며 ETH에도 공통 적용됩니다.")
    if current_regime == "데이터 부족":
        st.warning("상승·하락 국면 판정에는 약 220일 이상의 일봉 워밍업 데이터가 필요합니다. 사이드바의 시작일을 충분히 앞당겨 주세요.")

    regime_chart = go.Figure()
    regime_view = regime_daily.tail(240).copy()
    regime_chart.add_trace(go.Scatter(x=regime_view["known_at"], y=regime_view["regime_btc_close"], name="BTC 종가", line=dict(width=1.0, color="#94a3b8")))
    regime_colors = {"상승장": "#22c55e", "하락장": "#ef4444", "횡보/전환": "#f59e0b"}
    for regime_name, regime_color in regime_colors.items():
        regime_subset = regime_view[regime_view["market_regime"] == regime_name]
        if not regime_subset.empty:
            regime_chart.add_trace(go.Scatter(
                x=regime_subset["known_at"], y=regime_subset["regime_btc_close"],
                name=regime_name, mode="markers", marker=dict(size=5, color=regime_color),
            ))
    regime_chart.add_trace(go.Scatter(x=regime_view["known_at"], y=regime_view["regime_ema50"], name="EMA50", line=dict(width=1.2)))
    regime_chart.add_trace(go.Scatter(x=regime_view["known_at"], y=regime_view["regime_ema200"], name="EMA200", line=dict(width=1.2)))
    regime_chart.update_layout(title="BTC 일봉 추세 및 시장 국면 기준선", height=330, margin=dict(l=10, r=10, t=45, b=10), legend=dict(orientation="h", y=1.02, x=0))
    st.plotly_chart(regime_chart, use_container_width=True)
    st.caption("분류 규칙: 일봉 종가·EMA50·EMA200 배열과 EMA50의 20일 기울기, ADX(14) 18 이상을 함께 사용합니다. 당일 진행 중인 일봉은 다음 UTC 날짜부터 반영합니다.")

    st.subheader("전략별 최신 신호")
    signal_rows = []
    for symbol, df in market.items():
        last = df.iloc[-1]
        signal_rows.append({
            "종목": symbol,
            "기준 시각 (UTC)": last["open_time"].strftime("%Y-%m-%d %H:%M"),
            "종가": last["close"],
            "F&G": last["fng_value"],
            "시장 국면": last.get("market_regime", "데이터 부족"),
            "S1 추세 필터": effective_signal_text(df, last, "S1", use_regime_filter),
            "S2 공포 반등": effective_signal_text(df, last, "S2", use_regime_filter),
            "S3 탐욕 돌파": effective_signal_text(df, last, "S3", use_regime_filter),
        })
    signal_df = pd.DataFrame(signal_rows)
    st.dataframe(signal_df, use_container_width=True, hide_index=True)
    st.caption("‘진입 후보’는 완료된 4시간봉에서 신호 조건이 충족됐다는 뜻입니다. 시장 국면 필터가 켜져 있으면 S1·S3는 상승장, S2는 하락장이 아닐 때만 표시합니다. 앱은 주문을 실행하지 않습니다.")

    selected_symbol = st.selectbox("차트 종목", assets, key="dashboard_symbol")
    st.plotly_chart(make_price_chart(market[selected_symbol], f"{selected_symbol} · 4시간봉"), use_container_width=True)
    fng_chart = go.Figure()
    fng_tail = fng.tail(365)
    fng_chart.add_trace(go.Scatter(x=fng_tail["observed_date"], y=fng_tail["value"], mode="lines", name="Fear & Greed", line=dict(width=2)))
    fng_chart.add_hrect(y0=0, y1=25, fillcolor="red", opacity=0.08, line_width=0)
    fng_chart.add_hrect(y0=75, y1=100, fillcolor="green", opacity=0.08, line_width=0)
    fng_chart.update_layout(title="공포·탐욕 지수 (최근 1년)", yaxis=dict(range=[0, 100]), height=260, margin=dict(l=10, r=10, t=45, b=10))
    st.plotly_chart(fng_chart, use_container_width=True)

with tab_backtest:
    st.subheader("전략별 과거 성과 비교")
    st.warning("과거 시뮬레이션은 미래 수익을 보장하지 않습니다. 거래소 가격/펀딩비를 사용하지만 실제 체결, 청산, 마진 모드, 보험기금, 거래소별 수수료 차이는 완전히 재현하지 못합니다.")
    if st.button("▶ 선택 종목·전략 백테스트 실행", type="primary"):
        cfg = BacktestConfig(
            initial_capital=float(initial_capital), risk_per_trade=float(risk_pct),
            max_leverage=float(leverage_cap), fee_rate=float(fee_pct), slippage_rate=float(slippage_pct),
            use_regime_filter=bool(use_regime_filter),
        )
        all_trades = []
        all_equity = []
        metrics_rows = []
        progress = st.progress(0)
        tasks = [(symbol, strategy) for symbol in assets for strategy in ["S1", "S2", "S3"]]
        for idx, (symbol, strategy) in enumerate(tasks, start=1):
            tdf, eqdf = run_backtest(market[symbol], strategy, funding_data.get(symbol), cfg)
            summary = summarize_backtest(tdf, eqdf, initial_capital)
            metrics_rows.append({
                "종목": symbol, "전략": STRATEGY_NAMES[strategy], "거래 수": summary["trades"],
                "누적 수익률": summary["total_return_pct"], "CAGR": summary["cagr_pct"],
                "MDD": summary["mdd_pct"], "승률": summary["win_rate_pct"],
                "Profit Factor": summary["profit_factor"], "Sharpe": summary["sharpe"],
                "최종 자산": summary["final_equity"],
            })
            if not tdf.empty:
                tdf.insert(0, "symbol", symbol)
                tdf.insert(1, "strategy_code", strategy)
                all_trades.append(tdf)
            if not eqdf.empty:
                eqdf.insert(0, "symbol", symbol)
                eqdf.insert(1, "strategy_code", strategy)
                all_equity.append(eqdf)
            progress.progress(idx / len(tasks))

        result = pd.DataFrame(metrics_rows)
        st.session_state["bt_metrics"] = result
        st.session_state["bt_trades"] = pd.concat(all_trades, ignore_index=True) if all_trades else pd.DataFrame()
        st.session_state["bt_equity"] = pd.concat(all_equity, ignore_index=True) if all_equity else pd.DataFrame()

    if "bt_metrics" in st.session_state:
        result = st.session_state["bt_metrics"].copy()
        styled = result.style.format({
            "누적 수익률": "{:+.2f}%", "CAGR": "{:+.2f}%", "MDD": "{:.2f}%",
            "승률": "{:.1f}%", "Profit Factor": "{:.2f}", "Sharpe": "{:.2f}", "최종 자산": "{:,.2f}",
        }, na_rep="—")
        st.dataframe(styled, use_container_width=True, hide_index=True)
        st.caption("포지션은 거래당 계좌 위험 예산으로 산정하고, 최대 명목 레버리지 상한을 적용합니다. 백테스트는 전략별 독립 계좌로 초기 자본을 각각 부여합니다.")
        st.caption("시장 국면 필터: " + ("적용 중 (S1·S3=상승장, S2=하락장 제외)" if use_regime_filter else "미적용 (원 신호 기준)"))

        regime_trades = st.session_state["bt_trades"]
        if not regime_trades.empty and "entry_market_regime" in regime_trades.columns:
            st.subheader("시장 국면별 전략 성과")
            regime_stats = regime_trades.groupby(["symbol", "strategy_code", "entry_market_regime"], dropna=False).agg(
                거래수=("net_pnl", "size"),
                승률=("net_pnl", lambda x: (x > 0).mean() * 100),
                평균손익=("net_pnl", "mean"),
                총손익=("net_pnl", "sum"),
            ).reset_index()
            regime_stats["전략"] = regime_stats["strategy_code"].map(STRATEGY_NAMES)
            regime_stats = regime_stats.rename(columns={"symbol": "종목", "entry_market_regime": "진입 당시 국면"})
            regime_stats = regime_stats[["종목", "전략", "진입 당시 국면", "거래수", "승률", "평균손익", "총손익"]]
            st.dataframe(regime_stats.style.format({"승률": "{:.1f}%", "평균손익": "{:+,.2f}", "총손익": "{:+,.2f}"}), use_container_width=True, hide_index=True)
            st.caption("거래가 청산된 시점이 아니라 진입 시점의 BTC 시장 국면으로 분류한 결과입니다. 표본 수가 적은 구간은 해석에 주의하세요.")

        eq_all = st.session_state["bt_equity"]
        if not eq_all.empty:
            plot_symbol = st.selectbox("자산 곡선 종목", assets, key="bt_plot_symbol")
            fig = go.Figure()
            sub = eq_all[eq_all["symbol"] == plot_symbol]
            for code, group in sub.groupby("strategy_code"):
                fig.add_trace(go.Scatter(x=group["time"], y=group["equity"], mode="lines", name=STRATEGY_NAMES[code]))
            fig.update_layout(title=f"{plot_symbol} · 전략별 자산 곡선", height=400, margin=dict(l=10, r=10, t=50, b=10))
            st.plotly_chart(fig, use_container_width=True)
        trade_results = st.session_state["bt_trades"]
        if not trade_results.empty:
            st.subheader("거래 내역")
            st.dataframe(trade_results.sort_values("entry_time", ascending=False), use_container_width=True, hide_index=True)
            st.download_button(
                "거래 내역 CSV 다운로드",
                trade_results.to_csv(index=False).encode("utf-8-sig"),
                file_name="crypto_fng_swing_trades.csv", mime="text/csv",
            )
        else:
            st.info("선택 기간과 조건에서 체결된 거래가 없습니다. 시작일을 앞당기거나 전략 조건을 확인해 주세요.")
    else:
        st.info("왼쪽 설정을 확인한 뒤 백테스트 실행 버튼을 눌러 주세요.")

with tab_risk:
    st.subheader("선물 포지션 규모 계산기")
    c1, c2 = st.columns(2)
    with c1:
        capital = st.number_input("계좌 평가액 (USDT)", min_value=100.0, value=float(initial_capital), step=100.0, key="risk_capital")
        risk_budget_pct = st.number_input("허용 계좌 손실 (%)", min_value=0.1, max_value=5.0, value=0.5, step=0.1) / 100
        entry_price = st.number_input("진입 가격", min_value=0.000001, value=100000.0, format="%.6f")
        stop_price = st.number_input("손절 가격", min_value=0.000001, value=98000.0, format="%.6f")
        lev = st.number_input("사용 레버리지", min_value=1.0, max_value=50.0, value=5.0, step=0.5)
    with c2:
        risk_amount = capital * risk_budget_pct
        stop_pct = abs(entry_price - stop_price) / entry_price if entry_price else np.nan
        if np.isfinite(stop_pct) and stop_pct > 0:
            desired_notional = risk_amount / stop_pct
            max_notional = capital * lev
            notional = min(desired_notional, max_notional)
            qty = notional / entry_price
            margin = notional / lev
            est_loss = qty * abs(entry_price - stop_price)
            st.metric("허용 손실 예산", f"${risk_amount:,.2f}")
            st.metric("권장 명목 포지션", f"${notional:,.2f}")
            st.metric("필요 증거금 (단순 계산)", f"${margin:,.2f}")
            st.metric("예상 손절 손실", f"${est_loss:,.2f}", f"계좌의 {est_loss/capital*100:.2f}%")
            st.metric("주문 수량", f"{qty:.6f}")
            if desired_notional > max_notional:
                st.warning("레버리지 상한 때문에 허용 손실 예산을 모두 활용하지 못합니다. 계산된 손절 손실이 설정 위험 예산보다 작습니다.")
        else:
            st.error("진입 가격과 손절 가격은 서로 달라야 합니다.")
    st.caption("거래 수수료, 펀딩비, 슬리피지, 유지증거금 및 강제청산 가격은 포함하지 않은 기초 포지션 사이징 계산입니다. 롱/숏 모두 진입가와 손절가를 입력하면 손실 규모는 동일한 방식으로 계산됩니다.")

with tab_alerts:
    st.subheader("텔레그램 알림 연결")
    token, chat_id = get_telegram_credentials(st.secrets)
    if token and chat_id:
        st.success("Telegram Bot Token과 Chat ID가 설정되어 있습니다.")
        st.caption("앱의 테스트 메시지는 아래 버튼으로 전송할 수 있습니다.")
        if st.button("✉️ 텔레그램 연결 테스트", type="primary"):
            try:
                send_telegram_message(
                    token, chat_id,
                    "✅ Crypto F&G Swing Lab 연결 테스트\n텔레그램 알림 설정이 정상적으로 작동합니다.",
                )
                st.success("테스트 메시지를 전송했습니다. 텔레그램 대화방을 확인하세요.")
            except TelegramAlertError as exc:
                st.error(str(exc))
    else:
        st.warning("텔레그램 인증 정보가 아직 설정되지 않았습니다. 코드나 GitHub에 토큰을 직접 입력하지 마세요.")

    st.markdown("""
    **1. 텔레그램 봇 생성**

    텔레그램에서 `@BotFather`를 열고 `/newbot` 명령으로 봇을 생성합니다. 발급된 Bot Token은 비밀 정보이므로 공개 저장소나 채팅에 붙여 넣지 마세요. 생성한 봇 대화방에서 `/start`를 한 번 전송해야 알림을 받을 수 있습니다.

    **2. Streamlit Cloud Secrets 설정**

    Streamlit 앱의 Manage app → Settings → Secrets에 아래 TOML을 입력합니다.
    """)
    st.code('''[telegram]
bot_token = "TELEGRAM_BOT_TOKEN을 입력"
chat_id = "개인 대화방 CHAT_ID를 입력"''', language="toml")
    st.markdown("""
    **3. GitHub Actions Secrets 설정**

    GitHub 저장소의 Settings → Secrets and variables → Actions → New repository secret에서 아래 두 항목을 각각 등록합니다.

    - `TELEGRAM_BOT_TOKEN`
    - `TELEGRAM_CHAT_ID`

    **4. 자동 알림 실행**

    저장소 기본 브랜치에 `.github/workflows/telegram-alerts.yml`을 업로드하면 GitHub Actions가 15분 간격으로 BTC·ETH 데이터를 확인합니다. 새 진입 신호(S1~S3) 또는 BTC 시장 국면 전환이 발생하면 텔레그램으로 전송합니다. 중복 방지 상태는 `alert_state.json`에 기록됩니다.
    """)
    st.info("알림은 BTC 시장 국면 필터를 기본 적용합니다(S1·S3=상승장, S2=하락장 제외). 사이드바에서 백테스트 필터를 바꿔도 GitHub Actions 알림 규칙에는 영향을 주지 않습니다. 자동 알림은 Streamlit 화면이 열려 있어야 하는 방식이 아니라 GitHub Actions가 별도로 실행합니다. 다만 예약 실행 지연이 가능해 초 단위 실시간 전달을 보장하지 않습니다.")
    st.markdown("공식 안내: [Streamlit Secrets 관리](https://docs.streamlit.io/deploy/streamlit-community-cloud/deploy-your-app/secrets-management) · [GitHub Actions 예약 실행](https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax#onschedule)")

with tab_method:
    st.subheader("전략 규칙")
    with st.expander("S1 · 공포 필터 + 추세 전환", expanded=True):
        st.markdown("""
        **진입:** 일봉 EMA200 위, 4시간 EMA20이 EMA50 상향 교차, RSI14 > 50, 공포·탐욕 지수 > 25.

        **청산:** 진입 후 최고가에서 2 ATR 추적손절 또는 EMA20·EMA50 데드크로스.
        """)
    with st.expander("S2 · 극단적 공포 + 반등 확인"):
        st.markdown("""
        **진입:** 최근 7개 관측치 중 공포지수 최저값 ≤ 25, 최근 2회 연속 지수 상승, 최근 3일 저점 대비 5포인트 이상 회복, 4시간 종가 EMA20 상향 돌파, RSI14 50 상향 돌파, 저가가 직전 12개 봉 최저가보다 높음.

        **청산:** 직전 12개 봉 최저가 - 1.5 ATR 손절, 1.5R에서 50% 익절, 잔여 물량은 2 ATR 추적손절, 최대 7일 보유. 동일 봉에서 손절·익절이 모두 닿으면 보수적으로 손절을 먼저 체결한 것으로 가정합니다.
        """)
    with st.expander("S3 · 탐욕 구간 + 고점 돌파"):
        st.markdown("""
        **진입:** 공포·탐욕 지수 ≥ 55 및 최근 2회 연속 상승, 일봉 EMA200 위, 직전 20개 4시간봉 고점 돌파, 거래량이 20봉 평균 이상. ETH는 ETH/BTC 비율도 일봉 EMA20 위여야 합니다.

        **청산:** 진입 기준 2 ATR 손절, 4시간 종가 EMA20 이탈 또는 최대 14일 보유.
        """)

    with st.expander("시장 국면 자동 분류 및 필터"):
        st.markdown("""
        - **상승장:** BTC 일봉 종가 > EMA200, EMA50 > EMA200, EMA50의 20일 기울기 양수, ADX(14) ≥ 18.
        - **하락장:** 종가 < EMA200, EMA50 < EMA200, EMA50 기울기 음수, ADX(14) ≥ 18.
        - **횡보/전환:** 지표가 충분히 계산됐지만 상승·하락 조건을 모두 충족하지 않는 구간.
        - **데이터 부족:** 일봉 EMA200 및 ADX 워밍업 관측치가 부족한 구간.
        - 국면은 BTC를 기준으로 하며, 완성된 UTC 일봉 정보만 다음 UTC 날짜부터 사용합니다.
        - 필터가 켜지면 S1·S3는 상승장에서만, S2는 하락장·데이터 부족 구간을 제외하고 진입합니다. 이 조건은 전략 가설이며, 국면별 백테스트 결과로 별도 검증해야 합니다.
        """)

    st.subheader("데이터 처리·백테스트 가정")
    st.markdown("""
    - **가격:** Binance USDⓈ-M 무기한 선물 `BTCUSDT`, `ETHUSDT` 4시간봉.
    - **펀딩비:** Binance 과거 펀딩 이벤트를 포지션 보유 시간에 반영. 양(+)의 펀딩은 롱 포지션 비용, 음(-)은 수취로 처리.
    - **심리지표:** Alternative.me 일별 공포·탐욕 지수. 해당 관측일의 값은 미래정보 사용을 피하기 위해 다음 UTC 날짜부터 적용.
    - **신호·체결:** 완료된 봉 종가에서 신호를 계산하고 다음 봉 시가에서 진입. 손절 가격을 갭 하락/상승으로 건너뛰면 불리한 시가 체결을 가정.
    - **비용:** 편도 수수료·슬리피지 설정을 각각 진입·청산에 적용.
    - **포지션 사이징:** 거래당 계좌 위험 예산 ÷ 진입-손절 가격 차이. 명목금액은 설정한 최대 레버리지 상한을 초과하지 않음.
    - **방향:** 이 앱의 세 전략은 롱 전용입니다. 자동 주문, 숏 포지션, 청산·유지증거금 시뮬레이션은 수행하지 않습니다.
    - **텔레그램:** GitHub Actions 예약 작업이 15분마다 신호와 BTC 시장 국면 변화를 점검합니다. Streamlit 세션과 별도로 실행되며, Telegram Bot Token·Chat ID는 GitHub Actions Secrets와 Streamlit Secrets에 분리 보관합니다.
    """)
    st.subheader("중요한 한계")
    st.markdown("""
    1. Alternative.me는 BTC 중심 심리지표이며, ETH의 독립적인 투자심리 지표가 아닙니다.
    2. 공포·탐욕 지수는 가격 변동성·모멘텀 등 시장가격 파생 요소를 포함하므로 독립적인 선행지표로 해석하면 안 됩니다.
    3. Binance의 시세·펀딩은 OrangeX와 다를 수 있습니다. 실제 거래소 수수료, 체결 슬리피지, 주문 규모별 시장충격도 별도 확인해야 합니다.
    4. 백테스트의 ATR·이동평균 초기 구간, 데이터 결측, 시장 미시구조는 실제 체결 환경과 차이가 있습니다.
    5. 이 도구는 분석·의사결정 보조용이며 투자 권유 또는 자동매매 시스템이 아닙니다.
    """)

st.divider()
st.caption("데이터 출처: Binance USDⓈ-M Futures API · Alternative.me Crypto Fear & Greed Index. Alternative.me 지수 사용 시 출처 표기를 유지해야 합니다.")

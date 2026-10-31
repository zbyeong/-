# Crypto Fear & Greed Swing Lab

BTC·ETH의 공포·탐욕 지수 기반 4시간봉 스윙 전략을 모니터링하고 백테스트하는 Streamlit 앱입니다. **텔레그램 및 외부 알림 기능은 포함하지 않습니다.**

## 주요 기능

- Binance USDⓈ-M 무기한 선물 BTCUSDT·ETHUSDT 4시간봉 OHLCV 자동 수집
- Binance 과거 펀딩비 자동 수집 및 보유 기간 비용 반영
- Alternative.me 일별 Crypto Fear & Greed Index 자동 수집
- 심리지표 1일 지연 적용으로 look-ahead bias 완화
- 3개 롱 스윙 전략의 최신 신호 모니터링
- BTC 일봉 기반 시장 국면 자동 분류: 상승장 / 하락장 / 횡보·전환 / 데이터 부족
- 국면 필터: S1·S3는 상승장, S2는 하락장·데이터 부족을 제외하고 진입하도록 선택 가능
- 백테스트 결과를 진입 당시 시장 국면별로 분해해 거래 수·승률·평균 및 총손익 비교
- 수수료·슬리피지·펀딩비·위험 기반 포지션 사이징 반영 백테스트
- 거래 내역 CSV 다운로드
- 레버리지/손절폭 기반 포지션 규모 계산기

## 파일 구성

```text
repository-root/
├── app.py
├── data_loader.py
├── strategy_engine.py
├── requirements.txt
├── README.md
├── .gitignore
└── .streamlit/
    └── config.toml
```

## GitHub에 업로드된 기존 저장소를 수정하는 방법

1. 저장소의 기존 `app.py`를 이 패키지의 `app.py`로 교체합니다.
2. `README.md`도 교체하면 텔레그램 설정 안내가 제거됩니다.
3. `data_loader.py`, `strategy_engine.py`, `requirements.txt`는 동일 버전으로 교체하는 것을 권장합니다.
4. 기존 `alerts.py`, `alert_worker.py`, `.github/workflows/telegram-alerts.yml`, `alert_state.json`이 있다면 삭제해도 됩니다. 이 버전에서는 사용하지 않습니다.
5. Streamlit 설정 파일은 저장소 최상위의 `config.toml`이 아니라 `.streamlit/config.toml` 경로에 있어야 합니다. `.streamlit` 폴더가 없으면 새로 만드세요.
6. GitHub에 변경 사항을 commit/push하면 Streamlit Community Cloud가 다시 배포합니다.

## Streamlit Community Cloud 배포

1. GitHub 저장소의 기본 브랜치에 위 파일을 저장합니다.
2. [Streamlit Community Cloud](https://share.streamlit.io/)에서 저장소를 연결합니다.
3. Main file path를 `app.py`로 지정하고 Deploy합니다.
4. 별도 API 키나 Streamlit Secrets는 필요하지 않습니다. Binance 및 Alternative.me의 공개 API를 사용합니다.

## 로컬 실행

```bash
python -m venv .venv

# Windows
.venv\\Scripts\\activate

# macOS / Linux
source .venv/bin/activate

pip install -r requirements.txt
streamlit run app.py
```

## 전략 및 시장 국면 규칙

### S1. 공포 필터 + 추세 전환

진입: 일봉 EMA200 위, 4시간 EMA20/EMA50 골든크로스, RSI14 > 50, F&G > 25.

청산: 최고가 기준 2 ATR 추적손절 또는 EMA20/EMA50 데드크로스.

### S2. 극단적 공포 + 반등 확인

진입: 최근 7개 공포지수 관측치 최저 ≤ 25, 최근 2회 연속 상승, 최근 3일 저점 대비 5포인트 이상 회복, EMA20 상향 돌파, RSI14 50 상향 돌파, 저가가 직전 12개 봉 최저가보다 높음.

청산: 직전 12개 봉 최저가 - 1.5 ATR 손절, 1.5R에서 50% 익절, 잔여 물량 2 ATR 추적손절, 최대 7일 보유.

### S3. 탐욕 구간 + 고점 돌파

진입: F&G ≥ 55 및 최근 2회 연속 상승, 일봉 EMA200 위, 직전 20개 4시간봉 고점 돌파, 거래량이 20봉 평균 이상. ETH는 ETH/BTC 비율도 일봉 EMA20 위여야 함.

청산: 2 ATR 손절, 4시간 종가 EMA20 이탈 또는 최대 14일 보유.

### BTC 시장 국면 분류

- 상승장: BTC 일봉 종가 > EMA200, EMA50 > EMA200, EMA50의 20일 기울기 > 0, ADX(14) ≥ 18
- 하락장: 종가 < EMA200, EMA50 < EMA200, EMA50 기울기 < 0, ADX(14) ≥ 18
- 횡보/전환: 지표가 준비됐지만 상승·하락 조건을 모두 충족하지 않음
- 데이터 부족: 일봉 EMA200 및 ADX 초기 계산에 필요한 데이터가 부족함
- 하루의 국면은 해당 일봉이 완료된 다음 UTC 날짜부터 적용해 미완성 일봉의 미래정보 참조를 방지합니다.
- 국면 필터를 활성화하면 S1·S3는 상승장에서만, S2는 하락장 또는 데이터 부족이 아닌 구간에서만 진입합니다. 검증 대상인 전략 가설이며 수익을 보장하지 않습니다.

## 데이터 출처 및 한계

- 가격·펀딩비: Binance USDⓈ-M Futures public REST API (`/fapi/v1/klines`, `/fapi/v1/fundingRate`)
- 심리지표: [Alternative.me Crypto Fear & Greed Index](https://alternative.me/crypto/fear-and-greed-index/)
- Alternative.me 출처 표기를 유지하세요. 이 지수는 BTC 중심 심리지표이며 ETH의 독립적인 심리 지표가 아닙니다.
- 공포·탐욕 지수에는 가격 변동성·모멘텀 등 가격 파생 요소가 포함되므로 독립적인 선행지표로 해석하면 안 됩니다.
- Binance 시세와 펀딩은 OrangeX 등 다른 거래소와 다를 수 있습니다.
- 백테스트는 주문장 유동성, 실제 체결 지연, 거래소별 유지증거금, 강제청산 및 시장 충격을 완벽하게 재현하지 않습니다.
- 앱은 분석·의사결정 보조용이며 투자 권유 또는 자동매매 시스템이 아닙니다.

## 문제 해결

- Binance API 451/403: Streamlit Cloud 실행 지역 또는 네트워크에서 API 접속이 제한된 경우입니다.
- API timeout/429: 잠시 후 재시도하고 너무 잦은 새로고침은 피하세요.
- 데이터 부족 국면: 시작일을 최소 220~250일 이상 과거로 설정하세요.
- `ModuleNotFoundError`: 저장소에 `app.py`, `data_loader.py`, `strategy_engine.py`가 모두 최상위에 있는지 확인하고, 최신 `requirements.txt`로 재배포하세요.

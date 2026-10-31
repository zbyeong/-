# Crypto Fear & Greed Swing Lab

BTC·ETH의 공포·탐욕 지수 기반 4시간봉 스윙 전략을 모니터링하고 백테스트하는 Streamlit 앱입니다.

## 주요 기능

- Binance USDⓈ-M 무기한 선물 BTCUSDT·ETHUSDT 4시간봉 OHLCV 자동 수집
- Binance 과거 펀딩비 자동 수집 및 보유 기간 비용 반영
- Alternative.me 일별 Crypto Fear & Greed Index 자동 수집
- 심리지표 1일 지연 적용으로 look-ahead bias 완화
- 3개 롱 스윙 전략의 최신 신호 모니터링
- **BTC 일봉 기반 시장 국면 자동 분류:** 상승장 / 하락장 / 횡보·전환 / 데이터 부족
- **국면 필터:** S1·S3는 상승장, S2는 하락장·데이터 부족을 제외하고 진입하도록 선택 가능
- 백테스트 결과를 진입 당시 시장 국면별로 분해해 거래 수·승률·평균 및 총손익 비교
- 수수료·슬리피지·펀딩비·위험 기반 포지션 사이징 반영 백테스트
- 거래 내역 CSV 다운로드
- 레버리지/손절폭 기반 포지션 규모 계산기
- **Telegram 알림:** 새 전략 진입 신호와 BTC 시장 국면 변경 알림
- **GitHub Actions 예약 작업:** Streamlit 앱을 열어 두지 않아도 15분 간격으로 신호 검사
- 중복 알림 방지 상태를 `alert_state.json`에 기록

## 파일 구성

```text
crypto_fng_swing_app/
├── app.py
├── data_loader.py
├── strategy_engine.py
├── alerts.py
├── alert_worker.py
├── alert_state.json
├── requirements.txt
├── README.md
├── .streamlit/
│   └── config.toml
└── .github/
    └── workflows/
        └── telegram-alerts.yml
```

## Streamlit Community Cloud 배포

1. 이 폴더의 파일과 숨김 폴더 `.github`, `.streamlit`을 GitHub 저장소 최상위에 업로드합니다.
2. Streamlit Community Cloud에서 해당 저장소와 기본 브랜치를 선택합니다.
3. Main file path를 `app.py`로 지정하고 Deploy합니다.
4. 텔레그램 테스트 전송을 앱에서도 사용하려면 Streamlit 앱의 Manage app → Settings → Secrets에 아래 TOML을 등록합니다.

```toml
[telegram]
bot_token = "YOUR_TELEGRAM_BOT_TOKEN"
chat_id = "YOUR_TELEGRAM_CHAT_ID"
```

`secrets.toml` 파일 자체는 절대 GitHub에 업로드하지 마세요. `.gitignore`에서 해당 파일을 제외하도록 설정되어 있습니다.

## Telegram 자동 알림 설정

### 1. 봇 생성

1. Telegram에서 `@BotFather`를 열고 `/newbot`으로 봇을 생성합니다.
2. 발급된 Bot Token을 복사해 안전하게 보관합니다.
3. 새로 만든 봇과의 대화방에서 `/start`를 보냅니다. 사용자가 먼저 봇에 메시지를 보내야 개인 대화로 알림을 받을 수 있습니다.
4. Chat ID는 Bot API의 `getUpdates` 응답에서 `message.chat.id` 값을 확인합니다. 예시 주소를 만들 때 토큰이 포함되므로 해당 주소나 결과를 공개하지 마세요.

### 2. GitHub Actions Secrets 등록

GitHub 저장소에서 **Settings → Secrets and variables → Actions → New repository secret**을 열고 아래 두 항목을 등록합니다.

- `TELEGRAM_BOT_TOKEN`: BotFather에서 받은 토큰
- `TELEGRAM_CHAT_ID`: 알림을 받을 개인 대화방 ID

### 3. 워크플로 활성화

`.github/workflows/telegram-alerts.yml`을 저장소의 **기본 브랜치**에 업로드합니다. GitHub Actions가 매시 07·22·37·52분(UTC)에 실행되어 BTC·ETH 4시간봉, 공포·탐욕 지수, 시장 국면과 전략 신호를 검사합니다. 필요하면 Actions 탭에서 `workflow_dispatch`로 수동 실행할 수 있습니다.

예약 작업은 GitHub가 해당 시각에 실행하도록 요청하는 방식이며, 서비스 부하에 따라 지연될 수 있습니다. 정확히 15분 간격 또는 실시간 실행을 보장하지 않습니다. 공개 저장소의 예약 워크플로는 장기간 저장소 활동이 없을 경우 자동 비활성화될 수 있습니다.

### 4. 알림 종류

- **진입 신호:** S1·S2·S3 신호가 직전 완료 봉에서 꺼져 있다가 최신 완료 4시간봉에서 새로 켜진 경우
- **시장 국면 변경:** BTC 시장 국면이 이전 실행 시점의 상승장·하락장·횡보 전환과 달라진 경우
- 신호 알림은 분석 정보이며 주문 제출, 포지션 오픈, 손절·익절 주문을 실행하지 않습니다.
- 첫 실행에서는 현재 시장 국면을 기준 상태로 저장하며, 최초 상태 설정만으로는 국면 변경 알림을 보내지 않습니다.
- 상태 파일에는 마지막 신호 시각과 시장 국면만 저장되며 Bot Token은 저장되지 않습니다. GitHub Actions가 상태 파일 변경을 기본 브랜치에 커밋할 수 있도록 워크플로에 `contents: write` 권한이 설정되어 있습니다.

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

Streamlit Secrets를 로컬에서 시험하려면 `.streamlit/secrets.toml`을 만들고 위의 TOML 내용을 입력합니다. 이 파일은 GitHub에 커밋하지 마세요.

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

- **상승장:** BTC 일봉 종가 > EMA200, EMA50 > EMA200, EMA50 20일 기울기 > 0, ADX(14) ≥ 18
- **하락장:** BTC 일봉 종가 < EMA200, EMA50 < EMA200, EMA50 20일 기울기 < 0, ADX(14) ≥ 18
- **횡보/전환:** 지표가 준비됐지만 상승·하락 조건을 모두 충족하지 않음
- **데이터 부족:** 일봉 EMA200 및 ADX 초기 계산에 필요한 데이터가 부족함
- 하루의 국면은 해당 일봉이 완료된 다음 UTC 날짜부터 적용하여 미완성 일봉의 미래정보 참조를 방지합니다.
- 국면 필터는 선택 사항입니다. 활성화하면 S1·S3는 상승장에서만, S2는 하락장 또는 데이터 부족이 아닌 구간에서만 진입합니다. 이 필터는 검증 대상인 전략 가설이지, 성과가 보장된 규칙이 아닙니다.

## 데이터 출처 및 한계

- 가격·펀딩비: Binance USDⓈ-M Futures public REST API (`/fapi/v1/klines`, `/fapi/v1/fundingRate`)
- 심리지표: [Alternative.me Crypto Fear & Greed Index](https://alternative.me/crypto/fear-and-greed-index/)
- Alternative.me의 출처 표기를 유지하세요. 이 지수는 BTC 중심 심리지표이며 ETH의 독립적인 심리 지표가 아닙니다.
- 공포·탐욕 지수에는 가격 변동성·모멘텀 등 가격 파생 요소가 포함되므로 독립적인 선행지표로 해석하면 안 됩니다.
- Binance 시세와 펀딩은 OrangeX 등 다른 거래소와 다를 수 있습니다.
- 백테스트는 주문장 유동성, 실제 체결 지연, 거래소별 유지증거금, 강제청산 및 시장 충격을 완벽하게 재현하지 않습니다.
- Telegram 알림은 GitHub Actions 예약 실행 시점에만 점검하며, GitHub 예약 실행 지연이나 API 장애에 따라 전달이 늦어질 수 있습니다.
- 앱은 분석·의사결정 보조용이며 투자 권유 또는 자동매매 시스템이 아닙니다.

## 문제 해결

- **Binance API 451/403:** Streamlit Cloud 또는 GitHub Actions의 실행 지역에서 API 접속이 제한된 경우입니다.
- **API timeout/429:** 잠시 후 재시도하세요. 너무 잦은 새로고침은 피하세요.
- **데이터 부족 국면:** 사이드바의 시작일을 최소 220~250일 이상 과거로 설정하세요.
- **Telegram 테스트 실패:** Bot Token/Chat ID 오타, `/start` 미전송, 봇 차단 여부를 확인하세요.
- **GitHub Actions가 실행되지 않음:** 파일이 기본 브랜치에 있는지, Actions가 활성화되어 있는지, GitHub Secrets가 정확한지 확인하세요.

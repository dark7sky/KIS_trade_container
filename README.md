# Personal Trading MCP

개발·작업 인계는 [에이전트 시작 안내](AGENTS.md)와 [구조·알고리즘·운영 인계 문서](docs/AGENT_HANDOFF.md)를 먼저 참고하세요.

ChatGPT 웹에서 한국투자증권의 국내주식과 토스증권의 미국주식을 조회하고 현금 주문을 전송하는 개인용 MCP 서버입니다. Python MCP SDK의 Streamable HTTP, Keycloak OAuth, SQLite 주문 이력, Telegram 체결 알림을 사용합니다.

MCP 표시 이름은 `Personal Trading`입니다. 기존 설치와 OAuth 연결을 보존하기 위해 Python 패키지 `kis_mcp`, 배포 프로젝트 `kis-trade`, OAuth client/scope 이름은 유지합니다. 실행 명령은 `personal-trading-mcp`와 기존 `kis-mcp` 모두 지원합니다.

## 거래 동작

- **처음 시작하면 실전(real)** 입니다. 모드는 모든 대화가 공유하고 재시작 후에도 유지됩니다. `demo`는 KIS 모의투자만 지원하며 토스의 새 주문·계좌·시세 조회는 차단합니다. 기존 토스 주문의 조회·취소·상태 추적은 원래 실전 계좌를 사용합니다.
- `place_order`, `cancel_order`는 검증 후 즉시 전송합니다. `LIVE_TRADING_ENABLED`, `KIS_ENV`, `OPENAI_*`, `AUTO_TRADE_*`는 사용하지 않습니다.
- 전략 구성, 위험한도, 손익 제한, 종목·수량 판단은 이 MCP 서버 밖의 클라이언트가 담당합니다. 이 서버는 증권 정보, 계좌 정보, 주문·취소 실행과 그 결과 추적에 집중합니다.
- KIS는 실전 KRX/NXT, 모의 KRX의 지정가·시장가 주문을 지원합니다. 토스는 미국주식 실전 정수 수량의 지정가·시장가 DAY 주문, 잔량 전체 취소, 보유주식 OCO 익절·손절 지정가 조건주문을 지원합니다. 토스 소수점 수량·금액 주문, LOC, 주문 정정은 지원하지 않습니다. 신용·공매도·자동 진입 전략은 제공하지 않습니다.
- KIS는 현금 매수가능수량과 금액을 모두 확인합니다. 토스는 USD 현금 매수가능금액과 매도가능수량을 확인하며, 시장가 매수가능수량 API가 없으므로 확정 수량을 계산하지 않습니다. 지정가 금액 사전 검사는 수수료를 포함하지 않으며, 수수료·실시간 시장가·주문 가능 여부는 토스의 최종 심사를 따릅니다. 외부 HTS/MTS의 동시 주문·출금까지 서버가 잠글 수는 없습니다.
- 가격·시간·거래소별 종목 적격성의 최종 판단은 KIS가 수행합니다. 거절 시 다른 시장·계좌·주문 유형으로 자동 재주문하지 않습니다.
- `accepted`는 접수 성공이지 체결 완료가 아닙니다. `get_order` 또는 Telegram으로 체결을 확인합니다.
- `remaining`은 만료분을 제외한 주문 잔량이며 현재 취소가능수량을 보장하지는 않습니다. 일별조회 원본 잔량은 `broker_remaining`, 만료수량은 `expired`로 구분합니다. 지원 중인 KRX/NXT 지정가·시장가 주문은 한국시간으로 주문일이 지난 뒤 일별 내역 조회에 성공하고 수량이 일치하면 남은 수량을 `expired`로 처리합니다. 부분체결·직접 취소 수량은 보존하며 만료를 취소 완료 알림으로 보내지 않습니다. 당일 세션 종료 시각에 따른 즉시 만료는 아직 지원하지 않습니다.
- Telegram은 **이 MCP에서 만든 주문의 체결·취소 완료만** 알립니다. 부분체결은 새로 관측한 수량과 해당 구간 평균가로 알립니다. 폴링 사이 여러 체결이 합쳐질 수 있습니다.

## 도구

아래 기존 이름의 도구는 KIS 국내주식 전용입니다. `get_status`는 KIS와 토스의 구성 상태를 함께 반환하며 `set_mode`는 공통 모드를 바꿉니다.

| 도구 | 주요 입력 / 역할 |
| --- | --- |
| `get_status` | 전역 모드, 마지막 API 상태, 추적 오류, 알림 대기열 |
| `set_mode` | `mode`: real 또는 demo |
| `get_account` | `exchange`: KRX 기본, NXT 선택. 잔고 평가가격 관측시각 포함 |
| `get_quote` | `symbol`, `exchange`: 현재가·10단계 호가와 가격·호가별 관측시각 |
| `get_order_capacity` | `symbol`, `price`, `exchange`: 현금 주문가능액·수량 |
| `place_order` | `client_request_id`, `symbol`, `side`, `quantity`, `exchange`, `order_type`, `price`, 선택 `expected_mode` |
| `list_orders` | `start_date`, `end_date`: YYYY-MM-DD, 최근 90일 |
| `get_order` | `order_id`: 서버가 반환한 UUID |
| `cancel_order` | 새 `client_request_id`, `order_id`, 선택 `quantity`, 선택 `expected_mode` |
| `resolve_order` | `order_id`, `broker_id`: 응답 유실 후 사용자가 확인한 KIS 주문번호 연결 |
| `resolve_order_not_submitted` | `order_id`: 사용자가 KIS 주문내역에서 주문이 없음을 확인한 응답 유실 주문을 미접수로 확정 |

종목은 6자리 코드(ETN은 Q+6자리), 매수/매도는 buy/sell, 주문유형은 limit/market입니다. 지정가는 양의 정수 원, 시장가는 `price=0`입니다. `client_request_id`는 8~100자의 영문·숫자·`_.:-`입니다.
`expected_mode`는 `real` 또는 `demo`입니다. `place_order`는 현재 서버 모드가 다르면 전송하지 않고, `cancel_order`는 원주문의 모드가 다르면 전송하지 않습니다.

`get_order`와 `cancel_order`는 현재 모드가 아닌 **주문의 원래 계좌**를 사용합니다. 계좌번호 설정이 바뀌면 기존 주문에 접근하지 않고 오류를 반환합니다.

`get_account`와 `get_quote`는 `price_observed_at`을 KST 오프셋이 포함된 ISO-8601 형식으로 반환합니다. `get_quote`는 `component_observations.price.received_at`과 `component_observations.orderbook.received_at`도 함께 반환합니다. `price_time_basis` 또는 구성 요소의 `time_basis`가 `server_received_at`이면 이는 서버가 KIS 응답을 받은 시각이며 거래소의 마지막 체결시각은 아닙니다. 잔고의 `prpr`와 별도 현재가 `price`는 서로 다른 KIS 조회 스냅샷이므로, 가격 차이를 판단할 때 값과 관측시각을 함께 비교해야 합니다.

## 토스증권 미국주식

| 도구 | 주요 입력 / 역할 |
| --- | --- |
| `toss_get_account` | 미국주식 보유수량·평균매입가·평가가격과 USD 현금 매수가능액 |
| `toss_get_quote` | `symbol`: 미국 티커, 현재가 USD와 관측시각 |
| `toss_get_minute_bars` | `symbol`, `count`(1~199, 기본 20): 최신 완성 1분봉 OHLCV와 지연·누락·연속성 판정 |
| `toss_get_order_capacity` | `symbol`: USD 현금 매수가능액·매도가능수량 |
| `toss_get_market_calendar` | 전일·당일·익일 영업일 세션, 휴장·서머타임을 반영한 브로커 시간표 |
| `toss_place_order` | `client_request_id`, `symbol`, `side`, 정수 `quantity`, `order_type`, USD `price` |
| `toss_list_orders` | `start_date`, `end_date`: 최근 90일, 주문 생성일의 **한국시간** 기준 |
| `toss_get_order` | `order_id`: 이 MCP가 반환한 토스 주문 UUID |
| `toss_cancel_order` | 새 `client_request_id`, `order_id`: 미체결 잔량 **전체** 취소 |
| `toss_resolve_order` | `order_id`, `broker_id`: 사용자가 토스에서 확인한 응답 유실 주문 연결 |
| `toss_resolve_order_not_submitted` | `order_id`: 사용자가 미접수를 확인한 응답 유실 주문 종결 |
| `toss_create_oco` | 요청 ID, 미국 티커, 정수 수량, 익절·손절 감시가와 각각의 매도 지정가, 만료일로 보호 조건 등록 |
| `toss_list_conditions` / `toss_get_condition` | 이 MCP에서 등록한 보호 조건과 발생 주문의 최신 체결 상태 조회 |
| `toss_cancel_condition` | 감시 중인 보호 조건을 토스에서 취소. 이미 발생한 주문은 별도 확인 |
| `toss_resolve_condition` / `toss_resolve_condition_not_submitted` | 응답 유실 조건의 브로커 ID 연결 또는 앱에서 미접수를 확인한 뒤 종결 |
| `toss_resolve_condition_cancel` | 조건 취소 응답 유실을 토스 앱과 대조해 수동 복구 |
| `toss_create_watch` / `toss_list_watches` / `toss_stop_watch` | 가격 이상·이하 또는 1분봉 거래량 증가와 고점 돌파 감시. 일회성 신호 기록·조회, 최대 24시간 |
| `toss_replay_watch` | 입력한 과거 관측값으로 규칙을 재생. 브로커 호출·주문·저장 없음 |
| `toss_get_situation` | 지정 티커의 현재가, 보유, USD 현금, 활성 보호 조건·감시를 한 번에 조회 |
| `toss_get_stream_status` | 실시간 체결·호가·주문 구독, 거부 항목, 지연·연결 상태 확인 |
| `toss_estimate_entry_cost` | 현재 호가 스프레드·계좌 수수료·입력한 슬리피지 여유를 합쳐 예상 왕복 비용을 평가. 주문 없음 |
| `toss_place_guarded_order` | 실시간 호가가 10초 이내이고 예상 상승폭이 왕복 비용보다 크며 스프레드 제한 이내일 때만 토스 지정가 매수 전송 |

`toss_get_minute_bars`는 토스 REST 1분봉의 **봉 종료시각**을 그대로 표시합니다. 조회 시점보다 5초 이상 전에 끝난 봉만 반환합니다. 마지막 봉이 70초 이내이고 최근 6개 봉이 1분 간격으로 이어질 때만 `usable_for_entry=true`입니다. 장 마감·휴장·API 지연 또는 연속 봉 부족 시에는 `false`가 됩니다. `gaps`는 반환 봉 사이 누락 구간 수로, 세션 경계에서도 발생할 수 있습니다. 이 값은 데이터 품질 판정이지 방향·수익 예측이 아닙니다. 분봉은 별도 `MARKET_DATA_CHART` 호출 한도를 사용하므로 초단위 반복 조회를 피하세요.

미국 티커는 대문자로 입력합니다(예: `AAPL`, `BRK.B`). 실제 종목 메타데이터에서 미국 시장·USD 여부를 확인하므로 영문 종목코드의 국내주식으로 잘못 주문하지 않습니다. 지정가 가격은 $1 이상 소수 둘째 자리, $1 미만 넷째 자리까지 허용하며 초과 자릿수는 조용히 절삭하지 않고 거부합니다. 시장가는 `price=0`입니다. 정수 수량만 주문하지만 보유·매도가능수량 조회에는 기존 소수점 주식도 표시됩니다.

토스 설정은 선택 사항입니다. `.env`에 다음 세 값을 모두 설정하면 활성화되고 모두 비어 있으면 KIS만 동작합니다. 일부만 설정하면 시작 시 오류로 중단합니다.

- `TOSS_CLIENT_ID`, `TOSS_CLIENT_SECRET`: 토스증권 웹 설정 → Open API에서 발급합니다.
- `TOSS_ACCOUNT_SEQ`: 공식 `GET /api/v1/accounts` 응답의 본인 계좌 식별값입니다. 인쇄된 계좌번호가 아닙니다.
- 토스 웹의 허용 IP 목록에 **컨테이너가 인터넷으로 나갈 때 사용하는 외부 IP**를 등록합니다.

키는 배포 서버의 `.env`에만 보관합니다. `get_status`의 `brokers.toss.enabled`는 설정 유무이며 인증 성공을 의미하지 않습니다. 실제 API 인증·계좌 조회·실전 주문은 별도 운영 검증 대상입니다. 토스 모의 거래로 대체하거나 KIS 계좌로 자동 전환하지 않습니다.

주문은 증권사·통화·계좌 지문을 저장하고 원래 증권사에서만 복구·취소합니다. 요청 ID는 두 증권사 전체에서 중복 사용을 검사합니다. 토스 `clientOrderId`에는 MCP 주문 UUID를 보내지만 공식 멱등성 보장은 10분뿐이므로, SQLite 요청 기록을 계속 유지하고 POST를 자동 재전송하지 않습니다. `unknown`은 새 요청 ID로 재주문하지 말고 토스 내역과 대조합니다. 후보가 하나여도 자동 연결하지 않습니다.

취소 응답의 ID는 취소 자주문 ID이며 원주문 ID를 덮어쓰지 않습니다. `accepted`는 취소 접수일 뿐입니다. 원주문이 취소·전량체결·거절로 종결되거나 취소 자주문의 `CANCEL_REJECTED`가 확인될 때 취소 요청 결과를 갱신합니다. 응답이 유실된 취소는 임의 재전송하지 않습니다. 외부 정정(`REPLACED` 등)이나 알려지지 않은 상태는 `needs_review`로 남깁니다. 해당 상태에서는 토스 새 주문을 차단하며 원주문을 다른 주문에 자동 연결하지 않습니다.

보호 조건은 토스의 OCO 조건주문입니다. 등록 직전에 실제 USD 보유 매도가능수량과 현재가를 조회하고 `익절 감시가 > 현재가 > 손절 감시가`를 검사합니다. 두 다리 모두 매도 지정가이며 수량은 동일합니다. 한쪽 **조건이 발동하면** 다른 조건이 취소됩니다. 발동 주문이 미체결·부분체결·거절될 수 있으므로 `toss_get_condition`의 `generated_orders`에서 실제 주문 상태와 체결수량을 확인해야 합니다. 발생 주문이 접수 또는 부분체결 상태에서 90초간 추가 체결되지 않으면 한 번 알리고, 새 체결 후 다시 90초간 멈추면 다시 알립니다. 알림은 재시작 후에도 중복 억제되며 자동 취소·재주문은 하지 않습니다. 별도로 `toss_get_order`는 기존 MCP 즉시 주문 ID 전용입니다. 조건주문 등록·취소 POST/DELETE가 불명확해지면 자동 재전송하지 않습니다. `unknown` 조건은 토스 앱과 대조한 뒤 복구 도구로 해결합니다. 조건 자동 수정·트레일링·자동 매매는 아직 제공하지 않습니다.

가격 감시는 토스 웹소켓 체결 프레임을 받는 즉시 평가합니다. 체결 시각이 10초 넘게 지났거나 연결이 끊긴 구간의 데이터는 쓰지 않으며, 재연결 시 호가 캐시를 버리고 구독을 다시 선언합니다. 토스 시세 스트림은 중간 프레임을 잃을 수 있고 순번도 제공하지 않으므로 모든 순간 터치를 포착한다고 보장할 수 없습니다. 감시 신호를 손절 보호 수단으로 쓰지 마세요. 거래량 돌파는 15초마다 조회한 최신 완성 1분봉 종가가 이전 5개 연속 봉 고점을 넘고 거래량이 그 5개 평균의 지정 배수 이상일 때 1회 알립니다. 조건 만료·신호 발생·감시 중지는 SQLite에 저장되며 재시작 후 복구됩니다. 감시 신호는 SQLite에만 기록하며 텔레그램으로 보내지 않습니다. `toss_list_watches`에서 발생 시각과 근거를 조회할 수 있습니다. 기존 대기열의 감시 신호도 발송 전에 제외합니다. 체결·취소 및 보호 조건 관련 거래 알림은 계속 전송됩니다. 감시 신호가 직접 주문을 생성하지는 않으며 ChatGPT로의 자동 이벤트 전송은 아직 구현하지 않았습니다.

진입 비용 평가는 최우선 매수·매도 호가 차이 1회, 매수·매도 계좌 수수료 2회, 호출자가 입력한 **왕복 슬리피지 여유**를 bp(1bp=0.01%)로 합산합니다. 호가 양쪽의 최우선 잔량이 주문 수량 이상이어야 하고 매수 지정가는 관측 매도호가에서 슬리피지 여유 안에 있어야 합니다. `expected_move_bps`는 AI 또는 사용자의 추정치일 뿐 검증된 확률이나 수익 보장이 아닙니다. `toss_place_guarded_order`는 이 값이 비용보다 클 때만 진입 요청을 전송하고 사용한 비용 스냅샷을 주문 기록에 저장하지만, 주문 접수·체결·최종 비용은 토스 응답으로 별도 확인해야 합니다. 기존 `toss_place_order`에는 이 필터가 적용되지 않으므로 AI 자동 진입에는 guarded 도구를 지정하세요. 과거 체결 기반 슬리피지 자동 보정과 성과 재생은 아직 구현하지 않았습니다.

한국시간 자정이나 로컬 장 종료 계산만으로 미국 주문을 만료 처리하지 않습니다. 토스의 주문 상세 응답으로 체결·취소를 확인합니다. 상세 응답이 부분체결만 나타내고 종료 증거가 없으면 잔량 상태를 보수적으로 유지합니다. 주문 웹소켓 이벤트는 조건주문 REST 재조회만 앞당기는 신호로 사용합니다. 웹소켓 연결 중 이벤트라도 최종 체결 판단은 REST가 기준입니다. 알림은 `[실전 TOSS US]` 및 USD 금액으로 구분합니다.

구현 기준: [토스 공식 API 가이드](https://developers.tossinvest.com/), [공식 REST OpenAPI](https://openapi.tossinvest.com/openapi-docs/latest/openapi.json) 버전 `1.2.24`, [공식 실시간 AsyncAPI](https://openapi.tossinvest.com/openapi-docs/latest/asyncapi.json) 버전 `1.2.2`(2026-10-14 확인).

## 설치 및 설정

운영 서버는 Linux Docker Engine + Compose를 가정합니다. 기존 Nginx와 새 서버 사이 사설망/VPN이 필요합니다.

1. `.env.example`을 참고해 배포 서버의 `.env`를 준비합니다. 기존 `.env`가 있으면 **덮어쓰지 말고** 누락된 설정만 추가합니다. 파일 권한은 `chmod 600 .env`로 제한합니다.
2. `DATA_DIR`를 호스트 절대경로(예: `/srv/kis-data`)로 정합니다. 저장소 바깥의 로컬 디스크를 사용하고 NFS/SMB는 피합니다.
3. `DATA_DIR/mcp`를 만들고 소유자를 `10001:10001`, 권한을 `700`으로 지정합니다. PostgreSQL 하위 디렉터리는 컨테이너가 초기화합니다.
4. `PRIVATE_BIND_IP`에는 새 서버의 사설/VPN IP, `TRUSTED_PROXY_IPS`에는 Nginx 서버의 정확한 내부 IP를 넣습니다. 컨테이너 포트 8000/8080에 대한 방화벽 허용 대상을 Nginx와 필요한 내부 관리 경로로 제한합니다. Docker 포트 게시가 일반 UFW 규칙을 우회할 수 있으므로 DOCKER-USER/nftables 또는 네트워크 방화벽에서도 확인합니다.
5. MCP와 로그인용 DNS 두 개를 기존 Nginx에 연결하고 각각 HTTPS 인증서를 준비합니다.
6. 다음 값을 설정합니다.

| 변수 | 예시 / 의미 |
| --- | --- |
| `MCP_PUBLIC_URL` | `https://mcp.example.com` — 경로·끝 슬래시 없음 |
| `KEYCLOAK_PUBLIC_URL` | `https://login.example.com` |
| `OAUTH_ISSUER` | `https://login.example.com/realms/kis` |
| `OAUTH_ALLOWED_SUB` | 새 UUID. Keycloak 초기 사용자 ID와 동일 |
| `CHATGPT_CLIENT_SECRET` | 별도로 생성한 충분히 긴 무작위 비밀값 |
| `CHATGPT_REDIRECT_URI` | ChatGPT 연결 관리 화면에 표시되는 정확한 OAuth callback URI |
| `TRADER_INITIAL_PASSWORD` | 최초 trader 로그인용 비밀번호 |
| `KC_BOOTSTRAP_ADMIN_USERNAME/PASSWORD` | 초기 Keycloak 관리자 자격정보 |
| `POSTGRES_PASSWORD` | 인증 DB 전용 비밀번호 |
| `MCP_ALLOWED_ORIGINS` | 생략 시 `https://chatgpt.com` |
| `ORDER_SUPERVISOR_INTERVAL_SECONDS` | 생략 시 10초, 1~3600 |

KIS 계좌번호는 앞 8자리, 상품코드는 뒤 2자리를 각각 입력합니다. 실전·모의 자격정보와 Telegram 설정은 모두 필요합니다. Telegram에서 해당 봇을 시작하고 허용 채팅에 메시지를 보낼 수 있게 설정합니다. 봇은 명령을 수신하지 않습니다.

```sh
docker compose --env-file .env config --quiet
docker compose --env-file .env up -d --build
docker compose ps
```

`docker compose config`를 `--quiet` 없이 실행하면 비밀값이 출력될 수 있습니다. 공유하지 마세요. Keycloak은 첫 실행에서만 realm을 import합니다. 이후 `.env`의 사용자 ID·비밀번호·클라이언트 비밀값·callback을 바꿔도 기존 realm이 자동 갱신되지 않습니다. 기존 DB를 삭제하지 말고 관리자 콘솔에서 명시적으로 갱신하세요.

`deploy/nginx/kis.conf.example`의 도메인·인증서·내부 IP를 바꿔 기존 Nginx에 적용합니다. `nginx -t` 후 reload합니다. `/admin`과 관리 포트 9000은 인터넷에 공개하지 않습니다. 관리자 작업은 사설망/VPN의 제한된 관리 경로에서 수행합니다. 프록시 신뢰 설정과 Host/Origin 검사를 끄지 마세요.

## ChatGPT 웹 연결

1. 계정에서 개발자 모드와 원격 MCP 연결 기능을 사용할 수 있는지 확인합니다.
2. 원격 MCP 주소로 `https://mcp.example.com/mcp`, 인증은 OAuth를 선택합니다.
3. 고정 클라이언트 ID `chatgpt-kis`와 `CHATGPT_CLIENT_SECRET`을 입력합니다. ChatGPT가 표시한 callback 주소를 `CHATGPT_REDIRECT_URI`에 정확하게 등록합니다. 최초 주소를 확인해야 한다면 서버를 올리기 전 연결 설정 화면에서 확인하거나, 초기 연결 시도 후 관리자 설정에서 수정합니다. 와일드카드 callback은 사용하지 않습니다.
4. `trader` 계정으로 로그인합니다. 최초 비밀번호 변경과 OTP 등록을 완료합니다.
5. `get_status`로 연결을 확인하고 `set_mode("demo")`를 호출합니다. 별도로 정한 모의 주문으로 조회→접수→체결/취소 흐름을 확인합니다.

서버는 JWT 서명·issuer·audience·만료·`kis:access` scope·`chatgpt-kis` client·사용자 UUID를 검사합니다. 올바른 비밀번호로 다른 사용자가 로그인하더라도 계좌 도구는 사용할 수 없습니다. 공개 OAuth 리소스 메타데이터는 `/.well-known/oauth-protected-resource/mcp`에 있습니다. Keycloak의 audience mapper는 이 개인 서버의 `/mcp` 리소스로만 토큰 대상을 제한합니다.

참고: [OpenAI 인증](https://developers.openai.com/plugins/build/auth), [ChatGPT 개발자 모드](https://developers.openai.com/api/docs/guides/developer-mode), [Keycloak 프록시 설정](https://www.keycloak.org/server/reverseproxy), [KIS 공식 예제](https://github.com/koreainvestment/open-trading-api).

## 결과 유실과 복구

- 같은 입력·요청 ID는 기존 결과를 반환합니다. 같은 ID로 입력을 바꾸면 오류입니다. 모드를 바꿔 재시도해도 원래 주문 결과를 반환합니다.
- POST 요청은 자동 재전송하지 않습니다. 타임아웃, 5xx, 잘못된 응답은 `unknown`으로 보존합니다. 해당 계좌의 불명확한 주문이 해결되기 전에는 추가 신규 주문을 차단합니다.
- KIS 주문 API에는 이 서버의 요청 ID를 저장하는 필드가 없습니다. 따라서 시간·종목·수량이 같은 후보가 하나여도 외부 주문과 확실히 구분할 수 없습니다. `get_order`로 후보를 확인하고 KIS 내역과 대조한 뒤 `resolve_order`로 연결합니다. 연결은 새 주문을 보내지 않습니다.
- 후보가 없다고 주문 실패가 자동 확정되는 것은 아닙니다. KIS 주문내역/고객지원으로 미접수를 확인한 뒤에만 `resolve_order_not_submitted`를 호출하세요. 도구는 확인 시점에 일별 주문내역을 다시 조회하며 일치 후보가 있으면 거부합니다. 확정된 주문은 `not_submitted`가 되고 새 주문 차단이 해제됩니다. 기존 요청 ID는 종결된 결과를 반환하므로 실제로 다시 주문하려면 새 요청 ID가 필요합니다.
- 취소 접수 역시 완료가 아닙니다. 실제 취소량이 확인될 때만 알립니다. 취소 결과가 불명확한 동안 중복 취소를 막습니다. 취소와 체결이 경합하면 실제 관측된 체결·취소 수량을 반영합니다.
- API 조회가 실패하거나 수량이 모순되면 기존 상태를 보존하고 `refresh_error`/`poll_error`를 표시합니다. 장 종료만으로 미체결 주문을 취소 완료로 간주하지 않습니다. 90일이 넘는 미해결 상태는 운영 확인이 필요합니다.
- 재시작은 미완료 주문과 알림 대기열을 복구합니다. SQLite 파일 잠금으로 같은 데이터 디렉터리를 쓰는 두 MCP 프로세스의 실행을 막습니다. 다른 디렉터리로 동일 계좌 서버를 중복 배포하지 마세요.

## 데이터·비밀정보·백업

MCP 데이터는 호스트 `DATA_DIR/mcp` → 컨테이너 `/data`로 연결됩니다. SQLite에는 계좌번호 대신 지문을 저장하고 토큰은 메모리에서만 캐시합니다. 토큰은 재시작 후 재발급하므로 빠른 재시작 시 KIS 발급 제한 때문에 잠시 실패할 수 있습니다. 로그인 DB는 `DATA_DIR/postgres`에 저장됩니다.

`.env`는 Git에서 제외하고 Docker는 allowlist 방식으로 소스·잠금 파일·README만 복사합니다. 각 컨테이너에는 필요한 변수만 전달합니다. 원본 KIS 응답·자격정보·Telegram URL을 로그로 출력하지 않습니다. Docker 관리자 권한을 가진 사용자는 컨테이너 환경변수를 볼 수 있으므로 서버 접근을 제한하세요.

추가 Git 보호 장치는 `git config core.hooksPath .githooks`로 활성화합니다. pre-commit/pre-push가 `.env` 및 런타임 파일을 차단하며 Python이 필요합니다. Linux에서 clone 후 `chmod +x .githooks/pre-commit .githooks/pre-push`도 실행하세요. `--no-verify`로 훅을 우회하거나 보호 설정을 해제하면 Git 자체가 이를 강제할 수는 없습니다.

백업은 주문 전송 중이 아닌 시간에 MCP를 중지하고 SQLite 디렉터리 전체를 복사합니다. PostgreSQL은 `pg_dump`로 별도 백업하거나 Compose 전체를 정상 종료한 뒤 전체 `DATA_DIR`를 파일시스템 백업합니다. 동작 중인 PostgreSQL 파일을 단순 복사하지 않습니다. `.env`는 데이터와 별도로 암호화 보관합니다.

복원은 모든 서비스를 중지한 상태에서 같은 버전의 이미지·데이터·설정을 복원하고 소유권을 유지한 뒤 시작합니다. 저장된 모드와 미완료 주문을 확인합니다. **빈 디렉터리로 시작하면 실전 모드의 새 서버가 됩니다.** 손상된 모드값은 자동 초기화하지 않고 기동 실패합니다. Keycloak 복원 시 사용자 UUID, realm, issuer, audience도 일치해야 합니다.

알림은 SQLite outbox에서 재시도하고 성공 이벤트 ID로 중복을 억제합니다. Telegram이 수신한 뒤 응답만 유실되면 재전송으로 중복될 수 있습니다. Telegram 장애가 거래 재전송을 유발하지 않습니다.

## 개발 검증

```sh
python -m venv .venv
# Windows: .venv\Scripts\python / Linux: .venv/bin/python
python -m pip install -e '.[test]'
python -m pytest
```

테스트는 임시 디렉터리·가짜 KIS/Telegram·테스트용 RSA 키만 사용합니다. `.env`를 자동으로 읽지 않으며 실전·모의 주문을 외부로 전송하지 않습니다. 운영 이미지는 `requirements.lock`에 고정한 의존성을 설치합니다. 운영 HTTPS/OAuth 연결과 실제 모의 주문 검증은 서버·도메인 설정 후 별도로 수행해야 합니다.

Docker 통합 검증은 `tests/compose.test.env`와 `tests/compose.smoke.yaml`을 함께 사용합니다. 예: `docker compose -p kis-trade-smoke --env-file tests/compose.test.env -f compose.yaml -f tests/compose.smoke.yaml up -d --build`. 이 구성은 외부 네트워크·호스트 포트·영구 볼륨 없이 가짜 값으로 실행합니다. Keycloak 기동 후 `tests/smoke_check.py` 내용을 MCP 컨테이너의 `python -` 표준입력으로 전달하면 realm·PKCE 설정·실제 발급 토큰 검증을 수행합니다. **이 검증 스크립트는 임시 realm의 로그인 조건을 변경하므로 운영에서 실행하지 않습니다.** 검증 후 같은 Compose 인수로 `down`하여 테스트 컨테이너를 제거합니다.

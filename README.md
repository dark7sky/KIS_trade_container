# KIS Trading MCP

ChatGPT 웹에서 개인 한국투자증권 계좌를 조회하고 국내주식 현금 주문을 전송하는 MCP 서버입니다. Python MCP SDK의 Streamable HTTP, Keycloak OAuth, SQLite 주문 이력, Telegram 체결 알림을 사용합니다.

## 거래 동작

- **처음 시작하면 실전(real)** 입니다. `set_mode("demo")`로 모의투자 전환 후 사용하세요. 모드는 모든 대화가 공유하고 재시작 후에도 유지됩니다.
- `place_order`, `cancel_order`는 검증 후 즉시 전송합니다. `LIVE_TRADING_ENABLED`, `KIS_ENV`, `OPENAI_*`, `AUTO_TRADE_*`는 사용하지 않습니다.
- 실전 KRX/NXT, 모의 KRX의 지정가·시장가 주문만 지원합니다. NXT 모의투자, 신용·미수·공매도·SOR·자동매매·주문 정정은 지원하지 않습니다.
- 현금 매수가능수량과 금액을 모두 확인합니다. 시장가 수량은 KIS가 계산한 미수 없는 시장가 주문가능수량으로 제한합니다. 외부 HTS/MTS의 동시 주문·출금까지 서버가 잠글 수는 없으므로 계좌 자체도 증거금 100%/미수 미사용 설정을 권합니다.
- 가격·시간·거래소별 종목 적격성의 최종 판단은 KIS가 수행합니다. 거절 시 다른 시장·계좌·주문 유형으로 자동 재주문하지 않습니다.
- `accepted`는 접수 성공이지 체결 완료가 아닙니다. `get_order` 또는 Telegram으로 체결을 확인합니다.
- Telegram은 **이 MCP에서 만든 주문의 체결·취소 완료만** 알립니다. 부분체결은 새로 관측한 수량과 해당 구간 평균가로 알립니다. 폴링 사이 여러 체결이 합쳐질 수 있습니다.

## 도구

| 도구 | 주요 입력 / 역할 |
| --- | --- |
| `get_status` | 전역 모드, 마지막 API 상태, 추적 오류, 알림 대기열 |
| `set_mode` | `mode`: real 또는 demo |
| `get_account` | `exchange`: KRX 기본, NXT 선택 |
| `get_quote` | `symbol`, `exchange`: 현재가·10단계 호가 |
| `get_order_capacity` | `symbol`, `price`, `exchange`: 현금 주문가능액·수량 |
| `place_order` | `client_request_id`, `symbol`, `side`, `quantity`, `exchange`, `order_type`, `price` |
| `list_orders` | `start_date`, `end_date`: YYYY-MM-DD, 최근 90일 |
| `get_order` | `order_id`: 서버가 반환한 UUID |
| `cancel_order` | 새 `client_request_id`, `order_id`, 선택 `quantity` |
| `resolve_order` | `order_id`, `broker_id`: 응답 유실 후 사용자가 확인한 KIS 주문번호 연결 |

종목은 6자리 코드(ETN은 Q+6자리), 매수/매도는 buy/sell, 주문유형은 limit/market입니다. 지정가는 양의 정수 원, 시장가는 `price=0`입니다. `client_request_id`는 8~100자의 영문·숫자·`_.:-`입니다.

`get_order`와 `cancel_order`는 현재 모드가 아닌 **주문의 원래 계좌**를 사용합니다. 계좌번호 설정이 바뀌면 기존 주문에 접근하지 않고 오류를 반환합니다.

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
- 후보가 없다고 주문 실패가 확정되는 것은 아닙니다. 다른 요청 ID로 재주문하거나 DB의 unknown 상태를 지우지 마세요. KIS 내역/고객지원으로 전송 여부를 확정한 뒤 운영자가 백업을 보존하고 상태를 조사해야 합니다. v1은 임의의 ‘실패로 확정’ 도구를 제공하지 않습니다.
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

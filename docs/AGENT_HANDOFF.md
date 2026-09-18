# KIS Trading MCP 에이전트 인계

최종 문서 점검: 2026-09-18 (Asia/Seoul). 코드 기준: `7a86c7f`.
이 문서는 대화 없이 작업을 이어가기 위한 지도다. 실행 시점의 Git·컨테이너·브로커 상태는 다시 확인해야 한다.
실제 비밀값과 주문 식별자는 기록하지 않는다.

## 1. 현재 상태와 다음 작업

### 2026-09-18 불명 주문 미접수 확정 (배포 완료)

- 코드 커밋 `7a86c7f` (`feat: resolve confirmed unsubmitted orders`)을 `origin/main`에 푸시하고 Docker VM `/srv/kis-trade`에 동기화했다. MCP만 재빌드·재기동했다.
- 컨테이너 `kis-trade-mcp-1`은 `healthy`. 설치된 `service.py`/`app.py` SHA-256이 해당 커밋과 각각 `3c6c95d73d93ae92bbb6b4176023d8dab52b22f71b7048bcadfab741a3d8bf68`, `6c91b04898eebde1a0c4c2e58ff74e0e192889d9cbb6925d9a3c81e6ffaf5d38`로 일치했다.
- 공개 `/healthz` HTTP 200, 미인증 `/mcp` 초기화 요청 HTTP 401 확인. `.env`와 영속 데이터는 보존했다. 실제 주문·취소·모드 변경은 실행하지 않았다.

- 사용자 보고: 모의계좌 SK하이닉스 주문을 기존 요청 ID로 재시도해도 `unknown`; KIS 주문·보유 내역에는 해당 주문이 없었다. 중복 주문은 보내지 않았다.
- `resolve_order_not_submitted(order_id)`를 추가했다. `unknown`/`submitting`, 브로커 번호 없음, 원래 계좌 일별조회 성공, 신규 주문 후보 0건을 모두 확인한 뒤에만 `not_submitted`로 종결한다. 후보 존재·조회 실패·다른 상태는 거부하고 원상태를 유지한다.
- 이는 사용자의 KIS 내역/고객지원 확인을 기록하는 명시적 운영자 확정이다. API 조회에서 후보가 없다는 사실만으로 자동 실행하지 않는다. 증권사 POST를 보내지 않는다.
- 종결 시 활성·브로커 잔량을 0으로 만들고 `resolution=explicit_operator_no_broker_order`, `resolved_at`을 기록한다. 오류와 후보 목록을 제거하며 재시작·폴링 후에도 terminal이다. 동일 주문 요청 ID는 `not_submitted`를 반환하고 새 주문을 보내지 않는다. 새 주문에는 새 요청 ID가 필요하다.
- 회귀 테스트는 후보 존재, 조회 실패, 잘못된 상태, 멱등성, 신규 주문 차단 해제, 재시작 지속성을 포함한다. 실제 보고 주문은 아직 자동 종결하지 않았으며 해당 MCP `order_id`로 새 도구를 호출해야 한다.
- 배포 전 검증: 거래·만료 테스트 60 passed; `pytest -q -k 'not mcp_http_auth_initialization_tools_and_call'` 78 passed, 1 deselected; `pip_audit -r requirements.lock` 알려진 취약점 없음; `git diff --check` 통과. 기존 정지 이력의 MCP HTTP 통합 테스트는 제외했으므로 전체 E2E 통과로 해석하지 않는다.

### 2026-09-18 전일 주문 만료 수정 (배포 완료)

- 코드 커밋 `d8882d1` (`fix: expire reconciled previous-day order remainders`)을 `origin/main`에 푸시하고 Docker VM `/srv/kis-trade`에 같은 커밋의 추적 파일을 동기화했다. MCP만 재빌드·재기동했다.
- 컨테이너 `kis-trade-mcp-1`: `healthy`. 실제 import되는 `/usr/local/lib/python3.13/site-packages/kis_mcp/service.py` SHA-256은 로컬 커밋 파일과 동일한 `57d85627c7f6b1eed2a5a1b7d9f8bbe6e2163caa1394bcfecbb0130d989b6d49`다.
- 공개 `/healthz` HTTP 200, 미인증 `/mcp` 초기화 요청 HTTP 401 확인. `.env`와 영속 데이터는 보존했다. 실제 주문·취소·모드 변경 및 인증된 계좌 조회는 검증으로 실행하지 않았다.

- 원인: `refresh`가 일별조회 `rmn_qty`를 주문일과 무관하게 활성 잔량으로 사용했다. 사용자 보고의 전일 모의주문이 계속 `accepted`로 남는 경로를 가짜 브로커로 재현했다.
- `service.py`는 `broker_remaining`(일별 내역 잔량), `remaining`(만료분 제외 잔량), `expired`(만료수량)를 분리한다. 현재 지원하는 KRX/NXT 지정가·시장가에 한해 주문일 < 현재 KST 날짜이면, 성공한 재조회에서 수량 보존을 확인한 후 잔량을 만료 처리한다. 별도 DB 스키마 변경은 없다.
- `expired` 상태도 폴링을 계속한다. 늦게 반영되는 체결/취소는 재조회로 갱신하지만 역사적 미체결 잔량으로 `accepted`가 되살아나지 않는다. 만료만으로 pending 취소를 성공 처리하거나 취소 알림을 생성하지 않는다.
- 미확정 주문번호, 조회 실패, 날짜 불일치, 수량 불일치는 만료 확정 근거가 아니다. 잔량 0에 설명 없는 수량이 남으면 `needs_review`를 유지한다. 기존 `cncl_yn` 취소 증거를 날짜만으로 만료로 바꾸지 않는다.
- 범위: **다음 KST 날짜부터 보수적으로 확정**한다. 당일 세션별 즉시 만료는 미구현이다. KRX 정규/애프터 및 NXT의 효력이 다르고 저장된 주문에 세션/유효기간 정보가 없으므로, 장 마감 시각을 단일 상수로 하드코딩하지 않는다. `remaining`은 실시간 취소가능수량과 같지 않으며 `list_orders`는 여전히 저장 스냅샷이다.
- 근거: [한국투자증권 시장별 주문유지 안내](https://file.truefriend.com/Storage/customer/guide/regards/nxt01.html), [KIS 취소가능조회 공식 샘플](https://github.com/koreainvestment/open-trading-api/blob/main/examples_llm/domestic_stock/inquire_psbl_rvsecncl/inquire_psbl_rvsecncl.py). 당일 종료시각/휴장/세션 정책을 확장할 때 재확인한다. 모의 취소가능조회 부재를 잔고의 매도가능수량으로 대체해 만료 추정하지 않는다.
- 회귀 테스트: `tests/test_expiry.py`. 실전/모의·KRX/NXT·지정가/시장가, 부분체결, 취소와 만료 공존, 전량체결 우선, 조회 실패, 재시작, 지연 체결, 미확정 취소, 목록 재조회 포함.
- 실행 검증 결과는 아래 최신 기록을 참고한다. 운영 배포는 완료했지만 기존 계좌 주문이 실제로 재동기화됐는지는 별도 조회로 확인하지 않았다. 정상 폴링 시 새 만료 로직이 적용된다.

- `bfecf18`: 취소 접수번호로 취소 자주문을 연결해 원주문 취소 상태를 반영.
- `5eeab44`: 병렬 호출과 과거 취소 재동기화 재현 테스트.
- `5be99e1`: KIS 호출 직렬화, 저장된 취소 접수번호 재사용, 과거 취소 후보 추정.
- `5be99e1`은 `origin/main`에 푸시되었고, 2026-09-17에 Docker VM의 MCP 컨테이너에 배포됐다. 컨테이너 내부 `service.py` 해시가 로컬 커밋과 일치했고, 공개 `/healthz`는 `200`, 미인증 `/mcp` 초기화 요청은 `401`이었다.
- 현재 실행 이미지를 코드 커밋과 동일하다고 가정하지 않는다. 다음 작업 시작 시 Git 원격 상태와 컨테이너 상태·해시를 다시 확인한다.
- 이 문서를 바꾸는 시점의 작업 트리에 사용자 작성 문서 변경이 있을 수 있다. `git status --short`를 먼저 확인하고 기존 변경을 임의로 제거하거나 별도 작업과 섞어 커밋하지 않는다.
- 다음 우선순위: 아래 과거 취소 자동 추정 위험 검토 → 기존 주문 및 병렬 조회 재검증 → 실제 모의 주문은 별도 사용자 승인 후 수행.

사용자 보고: 주문·미체결 조회·취소는 성공했고, 초기에는 실제 취소 완료가 `needs_review`, 취소수량 0으로 표시됐다. 첫 수정 이후 새 취소 주문은 정상이나 과거 주문 하나가 남았고, 5개 동시 조회 중 `get_order`와 `get_order_capacity`가 한 차례 내부 오류를 냈다. 순차 재시도는 성공했다. 이는 사용자 관측이며 원본 API 응답과 당시 스택 트레이스는 이 저장소의 테스트 증거가 아니다.

## 2. 구조와 진입 경로

```text
ChatGPT / MCP client
  → 외부 Nginx (HTTPS)
    → mcp:8000 /mcp (OAuth JWT 검사)
      → TradingService → KIS REST
            ↕ SQLite (주문·요청·이벤트·outbox)
            → Notifications → Telegram
Keycloak:8080 → PostgreSQL (인증 데이터만)
```

| 경로 | 책임 / 먼저 볼 함수 |
| --- | --- |
| `src/kis_mcp/app.py` | `create_app`, MCP 도구 등록, `safe`, ASGI lifespan, `main` |
| `src/kis_mcp/service.py` | `place`, `refresh`, `cancel`, `resolve_order`, `resolve_order_not_submitted`, `poll`; 거래 정책과 상태 전이 |
| `src/kis_mcp/kis.py` | `call`, `token`, `throttle`, `pages`; KIS API·재시도·필드 변환 |
| `src/kis_mcp/store.py` | SQLite 트랜잭션, 멱등 요청, 취소 접수번호, 이벤트/outbox |
| `src/kis_mcp/models.py` | 입력 타입·수량/가격 검증, 거래 예외 |
| `src/kis_mcp/config.py` | `Settings.from_env`, 계좌 지문; `.env` 자동 로드 없음 |
| `src/kis_mcp/auth.py` | Keycloak JWKS와 JWT 검증 |
| `src/kis_mcp/telegram.py` | 알림 송신·재시도; 주문 전송과 독립 |
| `tests/conftest.py` | `FakeBroker`, 가짜 설정, 임시 Store |
| `tests/test_trading.py` | 멱등성·모드·체결·취소·재시작·과거 취소 테스트 |
| `tests/test_integrations.py` | 가짜 HTTP 기반 KIS, 인증, MCP HTTP, Telegram 테스트 |
| `tests/test_security_files.py`, `tests/test_compose_logging_driver.py` | 비밀 파일 보호 및 Compose 로깅 검사 |
| `scripts/check_git_secrets.py`, `.githooks/` | Git 비밀 파일 방지 검사 |
| `compose.yaml`, `Dockerfile` | 런타임 스택·이미지; `requirements.lock`으로 설치 |
| `deploy/nginx/kis.conf.example` | 외부 프록시 템플릿 |
| `deploy/keycloak/kis-realm.json` | 최초 realm import 템플릿 |
| `tests/compose.smoke.yaml`, `tests/smoke_check.py` | 격리된 Docker 인증 통합 검증 |

Python >=3.12, 운영 이미지 Python 3.13. 진입 명령은 `kis-mcp` → `kis_mcp.app:main`.
Uvicorn worker는 1개다. 앱 시작 시 Store, 주문 폴러, 알림 워커를 만들고 종료 시 작업 취소·클라이언트/DB 정리를 한다.

## 3. 반드시 보존할 거래 규칙

- 빈 DB의 모드는 `real`. `set_mode`는 서버 전체에 적용되고 재시작 후 유지된다.
- 실전 KRX/NXT, 모의 KRX만 지원한다. 현금 지정가·시장가만 지원하며 자동 전략/정정/신용 주문은 없다.
- `get_order`와 `cancel`은 주문에 저장된 모드·계좌 지문을 사용한다. 현재 설정 계좌와 다르면 차단한다.
- `accepted`는 접수이며 체결·취소 완료가 아니다. 최종 판단은 브로커 조회로 한다.
- KIS에 서버의 멱등 키를 저장하지 못한다. 로컬 `client_request_id`로만 중복 전송을 막는다.
- 실계좌·모의계좌에 대한 거래 수행은 별도 사용자 요청이 필요하다. 코드 테스트를 위해 실제 주문을 만들지 않는다.

## 4. 핵심 알고리즘

### 신규 주문과 결과 유실

`place`는 서비스 잠금 안에서 동일 요청 ID/입력 재시도를 확인한다. ID가 같고 입력이 다르면 오류다. 가격 문자열은 정수 원으로 정규화해 `1000`과 `1000.00`을 같은 입력으로 취급한다.

가능수량·금액 확인 → 주문 전 일별조회 `baseline_ids` 저장 → `submitting` 주문과 요청 기록을 트랜잭션으로 저장 → POST 1회 → 접수번호 수신 시 `accepted` 순서다. 명시적 거절/전송 전 실패는 `rejected`, 전송 여부가 불확실하면 `unknown`이다. 같은 모드에 `unknown`/`submitting`이 있으면 새 주문을 막는다.

주문번호가 없으면 `refresh`는 후보를 보여줄 뿐 자동 연결하지 않는다. `resolve_order`는 사용자가 확인한 번호를 종목·방향·수량·가격·날짜·기존 주문 중복과 대조하고 연결한다. `resolve_order_not_submitted`는 사용자가 미접수를 확인한 경우 원래 범위를 새로 조회해 후보가 없을 때만 terminal `not_submitted`로 종결한다. 두 도구 모두 새 거래를 전송하지 않는다.

### 체결·취소 수량과 상태

`refresh`는 원주문의 모드/날짜/거래소로 일별 내역을 가져와 `odno`가 일치하는 행 하나를 찾는다. 주문번호 비교는 선행 0을 제거한다.

```text
filled    = tot_ccld_qty
remaining = rmn_qty
rejected  = rjct_qty (없으면 0)
취소 증거가 있을 때의 취소량 = quantity - filled - remaining - rejected
```

취소 증거는 원주문 `cncl_yn=Y`, `cncl_qty`, 원주문번호+취소코드(`02`)가 연결된 자주문, 저장된 취소 접수번호와 일치하는 행이다. 자주문 경로는 `cncl_yn=Y`, 거절수량 0을 요구한다. 기존 누적 취소량보다 작아지지 않게 `max`를 사용한다. 과거 주문 후보 추정 예외는 아래 위험 항목을 반드시 읽는다.

상태 판정 순서는 다음과 같다.

| 조건 | 상태 |
| --- | --- |
| 전량 체결 | `filled` |
| 체결량 + 취소량 = 주문량 | `cancelled` (부분체결 후 잔량취소 포함) |
| 전량 거절 | `rejected` |
| 체결량 > 0 | `partial` |
| 위 조건 없음 | `accepted` |
| 잔량 0인데 체결+취소+거절 합계가 주문량과 불일치 | 위 결과를 `needs_review`로 변경 |

체결 감소, 수량 초과, 부적절한 체결금액을 감지하면 예외로 상태를 보존한다. `get_order`는 `TradingError`를 `refresh_error`로 반환하고 폴러는 실패 건수를 일반화한 `poll_error`로 기록한다.
`list_orders`의 `mcp_orders`는 저장된 스냅샷이며 각 주문을 새로 `refresh`하지 않는다.

### 취소 요청과 과거 재동기화

`cancel`: 기존 요청 조회 → 원주문 갱신 → 기존 `pending_cancel` 차단 → 취소가능 잔량 확인 → pending과 요청 결과 저장 → POST 1회. 접수번호는 `pending_cancel.broker_id`와 요청 결과 `cancellation_broker_id`에 남는다.

`refresh`는 취소 증가량이 요청량에 도달하거나 잔량이 0이면 취소 요청 결과를 갱신하고 pending을 지운다. 현재 코드는 완료 결과에도 접수번호를 보존한다. 예전 코드는 결과를 덮어쓰면서 번호를 잃을 수 있었다.

현재 재동기화는 별도 스키마 마이그레이션이 아니다. 폴링 또는 `get_order`가 저장된 요청의 취소 접수번호를 다시 찾아 반영한다. 데이터가 이미 소실된 경우 자동 복구의 확실성은 보장되지 않는다.

### 동시성·재시도·페이지 처리

- `TradingService.lock`: 주문/취소/갱신/모드 변경의 서비스 상태 보호. 모든 조회 도구를 한꺼번에 잠그지는 않는다.
- `KIS.call_locks[mode]`: 같은 모드의 업무 API HTTP 요청을 직렬화. 실전/모의 잠금은 별개다.
- `rate_locks[mode]`: `throttle`에서 실전 0.15초, 모의 1.05초 간격을 계산한다. **현재 throttle은 call 잠금 바깥에 있다.**
- `token_locks[mode]`: 토큰 발급 중복 방지. 만료 60초 전 갱신, 발급 시도 간 61초 제한. 토큰은 메모리에만 있다.
- GET은 최대 3회. 네트워크/응답 파싱 실패, HTTP 429/5xx, `EGW00201` 등 해당 분기에 1초·2초 백오프. 모든 브로커 거절을 재시도하지는 않는다.
- 주문/취소 POST는 1회. 전송 전 토큰 실패는 `SubmissionNotSent`; 전송 후 불확실성은 `UncertainSubmission`.
- `pages`: `tr_cont`와 FK/NK 커서로 최대 200페이지. 커서 공백·반복·한도 초과는 오류이며 부분 결과를 성공으로 반환하지 않는다.

### 이벤트와 재시작

누적 체결량 증가 시 `(신규 누적금액 - 이전 누적금액) / 증가수량`을 알림 구간 평균가로 계산한다. 수수료·세금 차감 후 정산금 계산이 아니다.
이벤트 ID는 `order_id:fill:누적수량` 또는 `order_id:cancel:누적수량`이다. 주문 상태·이벤트·outbox를 같은 트랜잭션에 저장하고 ID 중복을 억제한다.
Telegram이 수신한 뒤 응답만 유실되면 재전송 중복은 가능하다.

`poll`은 완료 상태(`filled`, `cancelled`, `rejected`) 중 pending 없는 주문을 건너뛴다. 나머지는 재시작 후에도 추적한다. 이미 잘못 완료 처리된 주문은 자동 폴링으로 정정되지 않을 수 있다.

## 5. 데이터·인증·환경 경계

SQLite 스키마 버전은 `1`, 파일은 `DATA_DIR/trading.sqlite3`. WAL + synchronous FULL, `server.lock`으로 동일 디렉터리 중복 실행 방지.

| 테이블 | 내용 |
| --- | --- |
| `meta` | 스키마 버전, 전역 모드 |
| `orders` | UUID와 주문 JSON (계좌 지문·브로커 번호·누적수량·pending 포함) |
| `requests` | 요청 ID, 종류, 정규화 입력 JSON, 결과 JSON |
| `events` | 고유 이벤트 ID, 이벤트 JSON |
| `outbox` | 메시지, 재시도 횟수/시각, 송신 완료 시각 |

Compose는 호스트 `${DATA_DIR}/mcp`를 MCP `/data`에, `${DATA_DIR}/postgres`를 PostgreSQL에 연결한다. PostgreSQL에는 거래 상태가 없다. 계좌 지문은 계좌번호+상품코드의 SHA-256이며 공개 주문 응답에서 제외된다.

JWT는 RS256/JWKS, issuer, `/mcp` audience, 만료, 허용 사용자 sub, `kis:access`, `azp=chatgpt-kis`를 검사한다. Host/Origin 검사도 활성화한다. `/healthz`의 `ok`는 앱 응답 확인일 뿐 브로커 인증/조회 정상 여부를 보장하지 않는다.

필수 설정 이름은 `.env.example`과 `Settings.from_env`를 따른다. 양쪽 모드 자격정보를 모두 요구한다. Keycloak realm import는 최초 생성용이므로 `.env` 수정만으로 기존 realm 설정이 바뀌지 않는다.

## 6. 알려진 위험과 후속 검토

아래는 2026-09-18 코드 읽기로 식별한 사항이다. 이 문서 작업에서는 실행 로직을 수정하지 않았다.

1. **과거 취소 후보의 자동 연결 위험**: `legacy_cancel_children`는 `needs_review`에서 종목·매매방향·수량·취소 플래그·거절수량만으로 단일 후보를 채택한다. 원주문 연결번호나 소유권을 요구하지 않아 같은 날 다른 주문의 취소를 오인할 수 있다. 앞선 설명의 “유일하므로 안전”은 보장이 아니다. 운영 자동 복구 전 이 추정을 제거/제한하거나 명시적 운영자 확인 절차를 설계해야 한다.
2. **직렬화와 호출 간격의 차이**: 앞 요청이 느리면 여러 요청이 throttle을 통과한 뒤 call 잠금에서 기다릴 수 있다. 잠금 해제 후 실제 전송 간격이 설정값보다 짧아질 수 있다. 전송 잠금 안에서 간격을 보장하는지 검토하고 지연된 첫 요청 뒤 5개 동시 조회로 검증한다. 토큰 발급은 별도 잠금 경로다.
3. **병렬 오류 원인 미확정**: 새 테스트는 모의 HTTP에서 요청 겹침을 재현한 것이다. 사용자 환경의 내부 오류가 반드시 이 원인이라는 증거는 없다. 비밀값을 제외한 오류 종류·호출 순서·시간을 확보해 확인한다.
4. **취소 결과와 주문 결과의 불일치**: 과거 pending이 이미 삭제되었다면 원주문 재동기화가 예전 `cancel_order` 요청 결과까지 고치지는 않는다. 같은 요청 ID 재호출은 그 저장 결과를 반환한다. 잔량 0만 먼저 보이는 경우 pending이 조기에 닫히는 흐름도 검토한다.
5. **미검증 실환경 영역**: 실계좌 인증/권한/계좌번호, 실전 KRX/NXT 주문코드, 네트워크 단절 직후 복구, 운영 재시작 추적, 장전/동시호가/장후, 실제 부분체결, 수수료·세금·정산금. 가짜 브로커 테스트와 실환경 검증을 구분한다.

## 7. 검증 방법과 마지막 증거

2026-09-18 만료 변경 검증: `pytest tests/test_expiry.py -q` 24 passed; `pytest -q -k 'not mcp_http_auth_initialization_tools_and_call'` 72 passed, 1 deselected (배포 전 재실행 통과). `git diff --check` 통과. `pip_audit -r requirements.lock` 알려진 취약점 없음. HTTP 통합 테스트는 기존 정지 이력으로 제외하며 실환경 전체 통과로 해석하지 않는다. 배포 후 공개 HTTP 검증 결과는 §1에 기록했다.

테스트는 가짜 자격정보·가짜 KIS/Telegram·임시 DB를 사용하고 `.env`를 로드하지 않는다.

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[test]'
.venv/bin/python -m pytest -q
.venv/bin/python -m pytest tests/test_trading.py -q
git diff --check
```

선택 도구는 현재 환경 설치 여부를 확인한다. `pip-audit`, `pytest-cov`, `ruff`는 프로젝트 test extra에 선언되어 있지 않다. 운영 설치의 기준은 `requirements.lock`이며 개발 가상환경 감사만으로 운영 이미지 전체를 검증했다고 말하지 않는다.

```sh
.venv/bin/python -m pip_audit
.venv/bin/python -m pip_audit -r requirements.lock
COVERAGE_FILE=/tmp/kis-agent.coverage .venv/bin/python -m pytest --cov=src/kis_mcp --cov-report=term-missing -q
```

이전 작업에서 기록된 결과(이번 문서 작업에서 재실행한 결과 아님):

- `pytest -k 'not mcp_http_auth_initialization_tools_and_call'`: 48 passed, 1 deselected.
- 제외한 MCP HTTP 통합 테스트는 30초 이상 정지 후 중단. 원인은 확정되지 않았으며 “환경 문제”로 단정하지 않는다. 전체 E2E 통과로 보고하지 않는다.
- 해당 제외 조건으로 커버리지 71%. 80% 목표 미달. 앱·설정 등 미검증 경로가 남는다.
- 당시 개발 가상환경 `pip-audit`: 알려진 취약점 없음, 로컬 `kis-trade-mcp`는 PyPI에 없어 감사 제외.
- `test_compose_logging_driver.py`는 모듈 최상위 `assert`로 검사한다. pytest 수집 시 실행되지만 별도 테스트 건수로 집계되지 않는다. 아래 명령으로 직접 확인할 수 있다.

```sh
.venv/bin/python tests/test_compose_logging_driver.py
```

Docker smoke는 README의 격리 스택 절차를 따른다. `tests/smoke_check.py`는 임시 realm 로그인 조건을 바꾸므로 운영 스택에서 실행하지 않는다.

## 8. 배포와 복구 절차

### 현재 운영 배치 (2026-09-17 확인)

| 항목 | 값 / 주의사항 |
| --- | --- |
| Docker VM | `root@192.168.45.222` SSH 22번 포트. NAS `ysyoo@192.168.45.100:6922`는 Docker 호스트가 아니다. |
| Compose 프로젝트 | `kis-trade` |
| 배포 소스 | `/srv/kis-trade` |
| 영속 데이터 | `/srv/kis-data` (`mcp` SQLite와 PostgreSQL 데이터). 재배포 시 삭제·초기화하지 않는다. |
| 공개 MCP | `https://trademcp.ysyoo.link/mcp` |
| 공개 OAuth | `https://tradelogin.ysyoo.link/realms/kis` |
| 프록시 | 별도 Nginx Proxy Manager가 HTTPS를 종료한다. MCP와 Keycloak 컨테이너의 호스트 바인딩·프록시 신뢰 설정을 임의로 넓히지 않는다. |

운영 `.env`는 Docker VM의 `/srv/kis-trade/.env`에만 있으며 권한을 제한한다. 이 문서·Git·명령 출력에 그 값을 쓰지 않는다. Keycloak의 사용자·클라이언트 설정은 기존 PostgreSQL realm에 저장되므로 `.env`만 바꿔서는 갱신되지 않는다.

### 필수 변경 완료 절차

사용자가 프로그램 변경과 배포를 승인한 경우, 코드·`Dockerfile`·`compose.yaml`·운영 의존성 변경은 다음 순서를 모두 거쳐야 한다. 문서 전용 변경이나 읽기 검토에는 재빌드가 필요 없다.

1. 로컬에서 관련 가짜 브로커 테스트를 실행한다. 거래 상태 변경은 재현 테스트, 부분체결·취소 경합·재시작·중복 요청을 포함한다. 실제 주문·취소·모드 변경으로 검증하지 않는다.
2. `pip_audit`, `git diff --check`, 푸시 전 `git diff`를 확인한다. HTTP 통합 테스트가 정지하면 제외 이유·실행 결과를 기록하고, 배포 후 런타임 HTTP 검증으로 대체하되 전체 통과라고 쓰지 않는다.
3. `docs/AGENT_HANDOFF.md`에 커밋, 테스트, 푸시와 배포 여부를 구분해 기록하고 관련 파일만 커밋한다.
4. `origin/main`으로 푸시한 뒤, 그 **동일한 `HEAD`**를 Docker VM에 배포한다. VM에는 `.env`와 영속 데이터를 보존해야 하므로 임의의 `git clean`, `down -v`, 데이터 디렉터리 교체를 사용하지 않는다.
5. MCP 코드만 바뀐 경우 MCP만 재빌드·재기동하고, 컨테이너 health·소스 해시·공개 health·OAuth 보호 응답을 검증한다. Keycloak/DB/데이터 볼륨을 불필요하게 재기동하지 않는다.

실제 배포 호스트에서 checkout 경로, 브랜치/커밋, 현재 컨테이너를 먼저 확인한다. 이 작업 공간의 경로는 `/home/ysyoo/KIS_trade_container`이며, VM 배포 경로는 위 표와 다르다.

```sh
git status --short
git log -5 --oneline
docker compose --env-file .env config --quiet
docker compose --env-file .env ps
```

업데이트 승인을 받은 범위에서 로컬의 추적된 `HEAD`만 VM에 동기화한다. 아래 방식은 VM의 `.env`와 영속 데이터를 덮어쓰지 않는다. MCP 코드만 변경한 경우 MCP 하나만 재빌드한다. 전체 스택/인증 설정 변경은 별도로 영향 검토한다.

```sh
git archive --format=tar HEAD | ssh root@192.168.45.222 \
  'tar -xf - -C /srv/kis-trade'
ssh root@192.168.45.222 \
  'cd /srv/kis-trade && docker compose -p kis-trade --env-file .env -f compose.yaml build mcp && docker compose -p kis-trade --env-file .env -f compose.yaml up -d --no-deps mcp && docker compose -p kis-trade ps mcp'
```

재기동 후 컨테이너의 변경 파일 해시가 로컬 `HEAD`와 같은지 비교하고, `https://trademcp.ysyoo.link/healthz`가 `200`, 인증 정보 없는 `POST /mcp` 초기화 요청이 `401`인지 확인한다. 이는 실제 주문을 만들지 않는 HTTP 런타임 검증이다. 인증된 MCP 클라이언트의 `get_status`·기존 주문 `get_order`는 DB와 폴링 상태를 변경할 수 있으므로 별도 승인 없이 자동 실행하지 않는다. 로그 확인 시 토큰/계좌/개인정보가 출력되지 않게 한다.

백업은 README를 따른다. 주문 전송 중이 아닌 때 MCP를 중지하고 SQLite 디렉터리 전체를 보존한다. PostgreSQL은 `pg_dump` 또는 정상 종료 후 백업한다. WAL 사용 중 DB 본체 파일만 복사하지 않는다. 복원은 동일 버전·설정·권한으로 서비스를 중지한 상태에서 수행한다. `down -v`나 빈 데이터 디렉터리로의 교체를 일반 복구 절차로 사용하지 않는다.

## 9. 다음 에이전트의 완료 기록 형식

작업 종료 시 이 문서의 현황에 날짜, 커밋, 변경 목적, 테스트 명령/결과/제외 항목, 알려진 미해결 문제, 푸시 여부, 배포 호스트와 이미지 확인 여부를 기록한다. 비밀값 없이 기록하며, 읽기 검토로 얻은 추론과 실제 실행 증거를 구분한다.

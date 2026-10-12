# Smart Solity Doorlock — Home Assistant 커스텀 통합

솔리티(SOLITY) 스마트 도어락을 **SmartThings 없이** 솔리티 클라우드 API로 직접 제어하는 HA 커스텀 통합.

- 🔓 잠금/해제(열기) — `lock` 엔티티
- 🔋 배터리 + 상태(deadBolt, 카드/비번 개수 등) — `sensor`
- 🕑 **이벤트 로그 빠른 감지** — 외부 열림(지문/카드/비번/실내)을 수초 내 감지 (`event` + "마지막 출입" 센서)
- 🔒 **실제 데드볼트(모터) 잠김 반영** — 열림 후 상태를 짧게 재조회해 진짜 잠긴 시점에 `locked` + `close` 이벤트
- 🚪 **문 열림/닫힘 센서** — 푸시풀 모티스의 문 감지 래치 값으로 `binary_sensor` 제공, 잠김 확인 간격도 문 상태에 맞춰 자동 조절 (v1.7.0부터)
- ⚙️ UI 설정흐름 (이메일/비번 입력 → 도어락 자동 탐색)
- 🔁 토큰 자동 갱신 (30일 JWT, 401/403 시 재로그인)
- 📟 폰 앱과 **독립 세션**(phoneToken `ha-solity`)이라 충돌 없음

> SmartThings API 유료화(2026.10부터)를 피해 솔리티 자체 클라우드를 직접 쓰는 방식입니다. (여전히 클라우드 경유 — 완전 로컬은 아님)

## 설치

### HACS (커스텀 저장소)
1. HACS → 우상단 ⋮ → **Custom repositories**
2. 이 저장소 URL 추가, 카테고리 **Integration**
3. "Smart Solity Doorlock" 검색 → 설치 → HA 재시작

### 수동
`custom_components/solity/` 폴더를 HA의 `/config/custom_components/` 아래로 복사 → HA 재시작

## 설정
설정 → **기기 및 서비스** → **통합 추가** → "Smart Solity" 검색
→ **이메일 + 비밀번호** 입력 → 도어락 자동 등록.

- 도어락이 여러 개면 선택 단계가 나옵니다.
- 설정 후 통합의 **옵션**에서 조회 주기(이벤트/상태)를 바꿀 수 있습니다.

## 엔티티
| 엔티티 | 설명 |
|---|---|
| `lock.<도어락이름>` | 잠금/해제/열기 — 상태는 **실제 데드볼트 값** (속성 `verifying`, `confirmed`, `last_verify_*`, `last_verify_trace`) |
| `binary_sensor.<도어락이름>_door` | **문 열림(on)/닫힘(off)** — 도어락의 문 감지 래치(`subLatch`) 값 |
| `sensor.<도어락이름>_battery` | 배터리 % |
| `sensor.<도어락이름>_last_access` | 마지막 출입 (누가/어떻게/언제) |
| `event.<도어락이름>_door_event` | `open`(출입 로그) / `close`(데드볼트 잠김 확인) / `other` — 자동화 트리거용 |

## 폴링 구조 (2가지)
| 종류 | 대상 | 도어락 깨움 | 기본 | 범위 |
|---|---|---|---|---|
| **이벤트 로그** | 클라우드 로그(`retrieveLog`) | ❌ 안 함 | 10초 | 5–600초 |
| **상태/배터리** | `get_status` | ✅ 깨움 | 30분 | 1–1440분 |

이벤트 로그는 도어락을 안 깨우니 **자주(수초)** 돌려도 배터리 부담이 없고, 상태 조회만 느리게 둡니다.

## 열림/잠김 상태 반영
솔리티 도어락은 **"열림"만 로그로 남기고 잠김 로그는 없습니다.** 실제 상태는 `get_status`로만 읽을 수 있고(도어락을 깨움), 여기에 두 값이 들어 있습니다.

| 값 | 의미 |
|---|---|
| `deadBolt` | **데드볼트**(모터로 나오는 잠금 빗장). 1 = 잠김, 0 = 열림 |
| `subLatch` | 푸시풀 모티스의 **문 감지 래치**(서브 래치). 문이 닫히면 문틀에 눌려 1, 열리면 0. 자동잠김은 이 래치가 눌리는 것을 신호로 걸립니다 |

### 열림 후 잠김 확인 (v1.7.0부터)
1. 이벤트 로그에서 **열림** 감지(또는 HA에서 열기) → `lock`을 즉시 `unlocked`로, 문 상태는 `unknown`으로
2. **바로 상태 조회** → 결과에 따라 다음 조회 시점을 정합니다:

| 조회 결과 | 판단 | 다음 조회 |
|---|---|---|
| 데드볼트 나옴 | 잠김 | **끝** (`locked` + `close` 이벤트) |
| 빗장 들어감 + **문 닫힘** | 곧 자동잠김이 걸림 | **1.5초 후** (3번까지. 그래도 안 잠기면 수동잠금 모드로 보고 간격을 늘림) |
| 빗장 들어감 + **문 열림** | 아직 드나드는 중 | 5초 → 10초 → 20초 … 최대 2분 간격 |

3. 15분이 지나도 안 잠기면 평소 상태조회 주기로 복귀

- 열림 직후 첫 상태 조회는 종종 응답 없이 멈춥니다(WiFi+ 모듈이 출입 기록을 올리는 중으로 추정). 그래서 잠김 확인 조회는 **2.5초 안에 응답이 없으면 끊고 바로 다시 조회**하며, 이런 실패는 `unavailable` 판정에 넣지 않습니다.
- 문을 열어 둔 채로 있으면 **잠길 때까지 `unlocked`로 유지**됩니다(타이머로 잠김 처리하지 않음).
- `lock` 속성 `last_verify_trace`에 마지막 확인 과정(몇 초에 무엇을 봤는지)이 남습니다. 예: `open → open → closed_unlocked → locked`
- 속성 `confirmed: false`는 값을 아직 도어락에서 다시 읽지 못했다는 뜻입니다(열림 직후, 조회 실패 등).
- 정기 상태조회가 가끔 실패해도 바로 `unavailable`로 떨어지지 않고 직전 값을 유지합니다(3회 연속 실패 시에만). 열림 이후의 실패는 `잠김`으로 되돌아가지 않습니다.
- 문 센서(`binary_sensor`)도 상태 조회 때만 갱신됩니다. 실시간 문 센서가 필요하면 별도 Zigbee 문 센서를 쓰세요.

> **이전 버전에서 업데이트:** v1.4의 `auto_close_seconds`, v1.5의 측정 모드, v1.6의 자동 보정(옵션·진단 센서·저장된 측정값)은 v1.7.0에서 삭제됐고 **설치 시 자동으로 정리**됩니다. `close` 이벤트는 실제 잠김 확인 시에만 발생합니다.

## 유의점
- HA에서 보낸 열기/잠그기는 즉시 반영됩니다.
- 이벤트는 도어락이 클라우드에 보고한 뒤 뜨므로, 그 보고 지연(수초)이 실제 하한입니다.
- 계정 자격증명은 HA config entry에 저장됩니다(비밀번호는 SHA-256 해시로만 보관).

### 자동화 예시
```yaml
automation:
  - alias: 아이 하교 알림
    triggers:
      - trigger: state
        entity_id: event.우리집_door_event
    conditions:
      - condition: template
        value_template: "{{ trigger.to_state.attributes.event_type == 'open' }}"
    actions:
      - action: notify.mobile_app_phone
        data:
          message: >
            현관 열림 — {{ trigger.to_state.attributes.who }}
            ({{ trigger.to_state.attributes.method }})
```

## 동작 원리
- `POST /api_v2/login` → JWT `token` + `tokenPwd` (헤더 `Authorization`, `AuthorizationPwd`)
- `PUT /api_v2/controlDevice/{id}` body `{"controlType":"open|close|get_status","optionValue":"1"}`
- `lang`은 반드시 숫자 `"0"` (문자 `"ko"`는 반쪽 토큰 발급됨)

## 면책
비공식 프로젝트입니다. 솔리티/제조사와 무관하며, 앱 API 변경 시 동작하지 않을 수 있습니다. 본인 계정·본인 도어락에 대해서만 사용하세요.

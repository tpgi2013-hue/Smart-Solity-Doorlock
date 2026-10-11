# Smart Solity Doorlock — Home Assistant 커스텀 통합

솔리티(SOLITY) 스마트 도어락을 **SmartThings 없이** 솔리티 클라우드 API로 직접 제어하는 HA 커스텀 통합.

- 🔓 잠금/해제(열기) — `lock` 엔티티
- 🔋 배터리 + 상태(deadBolt, 카드/비번 개수 등) — `sensor`
- 🕑 **이벤트 로그 빠른 감지** — 외부 열림(지문/카드/비번/실내)을 수초 내 감지 (`event` + "마지막 출입" 센서)
- 🔒 **실제 데드볼트(모터) 잠김 반영** — 열림 후 상태를 짧게 재조회해 진짜 잠긴 시점에 `locked` + `close` 이벤트
- ⚙️ UI 설정흐름 (이메일/비번 입력 → 도어락 자동 탐색)
- 🔁 토큰 자동 갱신 (30일 JWT, 401/403 시 재로그인)
- 📟 폰 앱과 **독립 세션**(phoneToken `ha-solity`)이라 충돌 없음

> SmartThings API 유료화(2026.10~)를 피해 솔리티 자체 클라우드를 직접 쓰는 방식입니다. (여전히 클라우드 경유 — 완전 로컬은 아님)

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
- 설정 후 통합의 **옵션**에서 조회 주기(이벤트/상태)와 **측정 모드**를 바꿀 수 있습니다.

## 엔티티
| 엔티티 | 설명 |
|---|---|
| `lock.<도어락이름>` | 잠금/해제/열기 — 상태는 **실제 데드볼트 값** (속성 `verifying`, `confirmed`, `last_verify_*`, `last_measurement`) |
| `sensor.<도어락이름>_battery` | 배터리 % |
| `sensor.<도어락이름>_last_access` | 마지막 출입 (누가/어떻게/언제) |
| `event.<도어락이름>_door_event` | `open`(출입 로그) / `close`(데드볼트 잠김 확인) / `other` — 자동화 트리거용 |

## 폴링 구조 (2가지)
| 종류 | 대상 | 도어락 깨움 | 기본 | 범위 |
|---|---|---|---|---|
| **이벤트 로그** | 클라우드 로그(`retrieveLog`) | ❌ 안 함 | 10초 | 5~600초 |
| **상태/배터리** | `get_status` | ✅ 깨움 | 30분 | 1~1440분 |

이벤트 로그는 도어락을 안 깨우니 **자주(수초)** 돌려도 배터리 부담이 없고, 상태 조회만 느리게 둡니다.

## 열림/잠김 상태 반영 (v1.5.0~)
솔리티 도어락은 **"열림"만 로그로 남기고 잠김 로그는 없습니다.** 실제 모터 상태는 `get_status`의 `deadBolt`(1=잠김)뿐이라, 이걸 기준으로 합니다.

1. 이벤트 로그에서 **열림** 감지(또는 HA에서 열기) → `lock`을 즉시 `unlocked`로, 캐시된 잠김 값은 폐기
2. 열림 감지 후 **3·8·15·30·60·120·180·300·600·900초**에 `get_status` 재조회
3. `deadBolt=1`이 읽히는 순간 `locked` + `close` 이벤트 발생 → 재조회 중단
4. 15분이 지나도 열려 있으면 평소 상태조회 주기로 복귀

- 보통 출입은 1~2회 조회로 잠김이 확인되므로 배터리 부담은 출입당 1~2회 깨우기 수준입니다.
- `lock` 속성 `last_verify_seconds` / `last_verify_polls` / `last_verify_result`에 마지막 확인 결과(감지 후 몇 초, 몇 번째 조회)가 남습니다. HA 기록에서 이 값을 보고 `const.LOCK_VERIFY_OFFSETS`를 조정하세요.
- 문을 열어 둔 채로 있으면 **잠길 때까지 `unlocked`로 유지**됩니다(타이머로 잠김 처리하지 않음).
- 속성 `confirmed: false`는 값을 아직 도어락에서 다시 읽지 못했다는 뜻입니다(열림 직후, 조회 실패 등).
- 상태조회가 가끔 실패해도(도어락 수면/타임아웃) 바로 `unavailable`로 떨어지지 않고 직전 값을 유지합니다(3회 연속 실패 시에만 unavailable). 열림 이후의 실패는 `잠김`으로 되돌아가지 않습니다.

### 측정 모드 (옵션, 기본 꺼짐)
잠김 확인 스케줄을 실측값으로 정하기 위한 모드입니다. 통합 **옵션 → 측정 모드**를 켜면:

- 열림 감지 후 **최대 30초간 연속 조회**합니다. 이전 조회가 끝나는 즉시 다음 조회를 보내며, 최소 간격은 0.5초입니다.
- 잠김이 확인되면 `lock` 속성 `last_measurement`에 기록하고 중단합니다. 30초 안에 안 잠기면 평소 스케줄로 넘어갑니다.

| 필드 | 의미 |
|---|---|
| `open_at` | 도어락 로그에 찍힌 **실제 열림 시각** |
| `detected_after_open_s` | 실제 열림 → HA 감지까지 걸린 시간 (클라우드 보고 + 로그 폴링 지연) |
| `locked_after_open_min_s` ~ `locked_after_open_max_s` | 실제 열림 후 **모터가 잠긴 시점의 범위** (정밀도 ≈ 조회 왕복 시간 × 2) |
| `locked_after_detect_max_s` | HA 감지 후 잠김 확인까지 걸린 시간 |
| `first_poll_already_locked` | 첫 조회에서 이미 잠겨 있었는지 |
| `rtt_avg_s` / `rtt_max_s` | 상태 조회 1회 왕복 시간 |
| `polls` / `failures` | 이번 측정에 쓴 조회 수 / 실패 수 |

> 출입 1회당 도어락을 여러 번 깨우므로 **하루 정도만 켜고** 끄세요. `open_at`은 도어락 시계(초 단위) 기준이라 HA 시계와 1초 안팎 차이가 날 수 있습니다.

> v1.4.x의 `auto_close_seconds`(자동잠김 반영 지연) 옵션과 `synthetic` 속성은 삭제됐습니다. `close` 이벤트는 이제 실제 잠김 확인 시에만 발생합니다.

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

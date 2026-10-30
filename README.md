# Easy-Gpt-Game-Toolkit

## scpsl_cleanroom: 클린룸 이식 자동화 툴킷

SCP: Secret Laboratory 10.2.2를 로블록스로 클린룸 방식으로 이식할 때 쓰는 도구입니다.
**게임 코드는 읽지 않습니다.** 스펙에 들어가는 값은 아래 세 가지 출처에서만 나옵니다.

| 출처 | 의미 | 도구 |
|---|---|---|
| `observed` | 게임을 실행하며 기록한 텔레메트리에서 측정한 값 (블랙박스) | `observe` |
| `asset` | 라이선스로 사용이 허용된 에셋(씬/프리팹/머티리얼)의 데이터 | `assets` |
| `design` | 이식 팀이 직접 정한 값 | `design` |

모든 값에는 출처(provenance), 샘플 수, 분포(p10–p90)가 함께 기록되어 나중에
"이 숫자는 어디서 왔는가"를 증명할 수 있습니다.

```
 [관측 팀]                                   [이식 팀]
 게임 플레이 → telemetry.jsonl ─┐
                                 ├─ observe ─► spec.json ─ build ─► SPEC.md, GameConfig.luau
 라이선스 에셋(.unity/.prefab/.mat) ─ assets ┘      ▲ guard(코드 혼입 차단)   LightingData.luau
                                                    │
 로블록스 포트 ─ TelemetryRecorder.luau ─► port.jsonl ─ compare ─► 스펙 대비 오차 리포트
```

### 설치

```bash
pip install -e .        # 또는 PyYAML만 설치하고 python -m scpsl_cleanroom 으로 실행
python -m unittest discover -s tests
```

### 1. 에셋 → 로블록스 조명/머티리얼

라이선스상 허용된 에셋을 Unity 텍스트(YAML) 형식으로 내보낸 폴더를 넣습니다.

```bash
python -m scpsl_cleanroom assets path/to/exported/Scenes path/to/Materials \
    -o out --spec spec.json --meters-per-stud 0.28 --brightness-scale 1.0
```

- `Light` 컴포넌트: 종류(Point/Spot/Directional/Area), 색, 세기, 범위, 스폿 각도, 그림자, 월드 좌표(부모 계층 합성)
- 같은 오브젝트의 렌더 파이프라인 조명 데이터(MonoBehaviour의 intensity/lightUnit/temperature 등)는 `unity_extra`로 보존
- `RenderSettings`: 앰비언트, 포그(지수 포그는 로블록스 선형 포그로 근사)
- 머티리얼: 기본 색, 이미시브 색 → `Neon` 후보
- 좌표 변환: Unity(왼손, +Z 전방, 미터) → Roblox(오른손, -Z 전방, 스터드). Spot은 `Face = Front` 기준으로 방향 일치
- 로블록스 라이트 최대 범위(60 스터드)를 넘는 값은 잘리고 `range_clamped = true`로 표시

결과물 `out/LightingData.luau`를 ModuleScript로 넣고 `roblox/LightingBuilder.luau`로 생성합니다.

```lua
local build = require(ReplicatedStorage.LightingBuilder)
build(require(ReplicatedStorage.LightingData), workspace)
```

`--brightness-scale`은 물리 단위 변환이 없으므로(로블록스 Brightness는 무단위) 스크린샷을 나란히 놓고 맞추는 값입니다.

### 2. 게임 동작 관측 → 수치

직접 운영하는 로컬 서버에서 플레이하며 텔레메트리를 JSON Lines로 남깁니다.
기록 방법은 자유입니다(공식 플러그인 API로 위치·체력 등 관측값 로깅, 화면 녹화 후 수동 기록 등).
형식은 `t`, `id`만 필수입니다.

```json
{"t": 12.31, "id": "p1", "role": "ClassD", "state": "sprint", "pos": [x, y, z], "hp": 100, "stamina": 0.82}
{"t": 15.02, "id": "p1", "event": "door_open_start", "tag": "LCZ_A"}
{"t": 16.20, "id": "p1", "event": "door_open_end", "tag": "LCZ_A"}
{"t": 20.05, "id": "p2", "hp": 87.5, "event": "hit", "tag": "pistol"}
```

```bash
python -m scpsl_cleanroom observe session1.jsonl session2.jsonl --spec spec.json
```

자동으로 뽑히는 값:

| 키 | 측정 방식 |
|---|---|
| `speed.<role>.<state>` | 연속 위치 샘플의 수평 속도 (정지·텔레포트 제외) |
| `vertical_speed.<role>.<state>` | 수직 속도 (점프, 낙하, 엘리베이터) |
| `rate.<field>.<role>.<state>` | 스태미나·체력 등 수치 필드의 초당 변화율 (구간별 선형 회귀, r² ≥ 0.9) |
| `regen_delay.<field>.<role>.<state>` | 상태 진입 후 회복이 시작되기까지의 지연 |
| `duration.<event>` | `X_start` ↔ `X_end` 이벤트 간 시간 (문 열림, 재장전, 엘리베이터 등) |
| `interval.<event>` | 같은 이벤트 반복 간격, `extra.min`은 연타했을 때 쿨다운 상한 |
| `damage.<tag>` | 피격 직전/직후 체력 차이, 최빈값(`extra.mode`) 포함 |

여러 세션을 넣으면 샘플 수 가중 평균으로 합쳐지고 출처가 누적됩니다.
샘플이 5개 미만인 값은 "더 기록하라"는 경고가 나옵니다.

**측정 팁**
- 상태(`state`)를 정확히 태깅할수록 값이 깨끗해집니다 (walk/sprint/crouch/idle).
- 쿨다운은 버튼을 연타한 기록으로 재야 `interval.*.min`이 실제 쿨다운에 수렴합니다.
- 데미지는 무기·부위별로 `tag`를 다르게 (`pistol_head`, `pistol_body`).

### 3. 이식 팀 전달물 생성

```bash
python -m scpsl_cleanroom design speed.Scp173.blink 0 m/s --note "블링크는 별도 구현" --spec spec.json
python -m scpsl_cleanroom build --spec spec.json -o out
```

- `out/SPEC.md`: 값·단위·분포·출처 표
- `out/GameConfig.luau`: `Config.speed.ClassD.walk` 형태의 중첩 테이블, 미터 단위는 스터드로 변환
- `design` 값은 이후 관측 데이터로 덮어써지지 않습니다.

### 4. 로블록스 포트 검증

`roblox/TelemetryRecorder.luau`를 서버에서 실행하면 같은 형식(미터 단위)으로 기록됩니다.
Output 창에 찍힌 `TLM {...}` 줄을 그대로 파일에 붙여넣으면 됩니다.

```bash
python -m scpsl_cleanroom compare port.jsonl --spec spec.json --tolerance 0.05
```

스펙 값 대비 ±5% 이내이거나 원본의 p10–p90 범위 안이면 `ok`, 아니면 `OFF`(종료 코드 1).

### 5. 코드 혼입 차단 (guard)

```bash
python -m scpsl_cleanroom guard spec.json out/
```

디컴파일러 헤더, C# 선언/어트리뷰트, IL 옵코드, `3.9f` 같은 C# 실수 리터럴, 게임 어셈블리 참조를 찾아내면
실패합니다. `assets`/`build` 출력은 쓰기 전에 자동으로 검사됩니다. 스펙 저장소의 pre-commit 훅으로 걸어 두세요.

### 클린룸 운영 원칙

1. 관측 팀과 이식 팀을 분리하고, 둘 사이에는 `spec.json` / `SPEC.md`만 오갑니다.
2. 디컴파일된 코드, 코드 스크린샷, 코드에서 옮겨 적은 상수는 스펙에 넣지 않습니다 (guard가 1차로 걸러냄).
3. 스펙의 모든 값은 `observed` / `asset` / `design` 중 하나의 출처를 가져야 합니다 (`validate`).
4. 텔레메트리 원본 파일을 보관해 두면 각 수치의 측정 근거가 됩니다.

이 도구는 법률 자문이 아닙니다. 게임 EULA와 서버 운영 규칙(공식 서버에서 자동화 도구 사용 등)은 직접 확인하세요.

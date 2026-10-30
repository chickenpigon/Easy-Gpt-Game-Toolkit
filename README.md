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

필요: Python 3.10+, PyYAML, numpy, (mp4/ogg 사운드 분석 시) ffmpeg.

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

### 6. 상세 명세서 자동화 (`kb`) — 핵심 워크플로

엔티티(역할, SCP, 아이템, 무기, 문, 시설 시스템, UI)마다 **질문 목록**이 있고,
플레이 기록을 넣을 때마다 모든 질문이 자동으로 다시 분석되어 명세서가 채워집니다.

```bash
python -m scpsl_cleanroom kb init --project project          # 카탈로그(질문 목록) 생성
python -m scpsl_cleanroom kb index path/to/exported_assets --project project   # 에셋 색인
# project/sessions/ 에 플레이 기록(*.jsonl)을 넣고
python -m scpsl_cleanroom kb build --project project         # 명세서 생성
python -m scpsl_cleanroom kb discover --project project      # 카탈로그에 없는 규칙 자동 발견
python -m scpsl_cleanroom kb demo --project demo             # (가짜 데이터로 결과물 미리보기)
```

생성물 (`project/spec/`):

| 파일 | 내용 |
|---|---|
| `index.md` | 엔티티별 진행률 (완료/부분/미측정) |
| `entities/<id>.md` | 엔티티 상세 명세: 수치, 공식(그래프 포함), 타임라인, 상태 머신, 에셋 목록 |
| `PLAN.md` | **다음 플레이 세션 체크리스트** — 아직 못 잰 항목과 기록 방법 |
| `DISCOVERED.md` | 카탈로그가 모르는 반복 패턴("A 다음엔 항상 B가 0.3초 뒤"), 상태 머신, 새 이벤트 |
| `GameSpec.luau` | 확정된 값/공식/타임라인을 로블록스 모듈로 |
| `spec.json` | 전체 결과 (기계 판독용) |

#### 분석 종류

| type | 하는 일 | 예 |
|---|---|---|
| `formula` | **공식 자동 발견**: 15개 후보 모델(선형, 기하급수, 상한 있는 증가, 포화, 거리 감쇠 4종 등)을 맞춰 AICc로 고르고, 노이즈 범위 안에서 반올림된 값(설계자가 쓸 법한 1.15, 200 등)을 제시 | 018 튕김당 속도 → `y = min(4 * 1.15^x, 30)`, 수류탄 → `y = 200 for x<=2, linear to 0 at x=8` |
| `sequence` | 트리거 이후 **항상 따라오는 이벤트**와 시간 오프셋 (모션/사운드/상태) | 096 게이트 강제 개방: `anim +0.06s → sound +0.29s → state:opening +2.1s → state:open +3.3s` |
| `states` | 상태 전이도, 각 상태 체류 시간, 전이를 일으킨 이벤트 | 096 평온→분노→진정 |
| `context` | 이벤트가 일어날 때의 조건(역할, 상태, 대상) | 강제 개방 가능 조건 |
| `speed` `rate` `regen_delay` `jump` | 이동 속도, 스태미나 증감, 회복 지연, 점프 높이/체공 | |
| `duration` `latency` `interval` `count_between` | 지속 시간, A→B 지연, 쿨다운, 탄창 용량 | |
| `damage` `field_delta` `field_max` | 피격당 피해, 사용 전후 체력 변화, 최대 체력 | |
| `asset` | 에셋 색인에서 이름 매칭: 사운드, 애니메이션(길이, 애니메이션 이벤트), 애니메이터(상태/전이 조건), UI 텍스트, 폰트, 조명 | |
| `manual` | `notes/*.yaml`에 직접 적은 답 | 말로만 설명 가능한 규칙 |

카탈로그(`project/catalog/*.yaml`)는 **시작점일 뿐입니다**. 엔티티 이름과 질문만 있고 수치는 하나도 없습니다.
10.2.2에 없는 것은 지우고, 모르는 시스템은 `DISCOVERED.md`의 제안을 보고 질문으로 추가하세요.

#### 플레이를 넣는 방법

1. **영상 어노테이터** `tools/annotator.html`: 브라우저로 열고 플레이 영상을 불러온 뒤 단축키로 이벤트를 찍습니다
   (`,` `.` 한 프레임 이동, 0.25배속 지원). 엔티티별 단축키 프리셋은 `kb hotkeys --entity Scp096`.
2. **사운드 자동 식별**: 라이선스 사운드 라이브러리가 영상의 몇 초에 재생됐는지 자동으로 찾아 이벤트로 만듭니다.
   ```bash
   python -m scpsl_cleanroom sounds match play3.mp4 --library assets/Audio -o project/sessions/play3.sounds.jsonl
   python -m scpsl_cleanroom sounds onsets play3.mp4 -o unknown.jsonl   # 라이브러리에 없는 소리의 시작점
   ```
3. **텔레메트리**: 직접 운영하는 서버에서 위치/체력 등을 기록한 JSONL.

같은 플레이의 파일은 점 앞 이름을 맞추세요 (`play3.jsonl`, `play3.annotations.jsonl`, `play3.sounds.jsonl` → 한 세션).
시간이 어긋나면 파일 첫 줄에 `# offset: 1.25` 를 넣습니다. 서로 다른 세션은 타임라인이 섞이지 않습니다.

#### 개별 도구

```bash
python -m scpsl_cleanroom fit s.jsonl --extract bounce --id scp018 --models growth --plot bounce.svg
python -m scpsl_cleanroom fit s.jsonl --extract falloff --tag frag_grenade --models falloff
python -m scpsl_cleanroom timeline s.jsonl --trigger gate_force_start --scope tag
python -m scpsl_cleanroom ui path/to/UI -o out    # UI 레이아웃/폰트 → UiLayout.luau (+ roblox/UiBuilder.luau)
```

UI 변환: RectTransform(앵커, 피벗, 위치, 크기)을 Roblox `UDim2`/`AnchorPoint`로 바꾸고, CanvasScaler 기준 해상도로
오프셋을 스케일로 바꿔 해상도에 무관하게 만듭니다. TextMeshPro와 기본 UI Text 모두 폰트, 크기, 색, 정렬을 추출하며,
`.meta` 파일의 guid로 폰트 파일 이름을 찾습니다.

#### 자동화의 한계

- 수치/공식/타이밍/상태 전이는 **기록만 있으면 자동**으로 나옵니다. 다만 무엇을 기록할지(이벤트 이름 붙이기)는 사람이 해야 합니다.
- 이 도구는 게임 시스템을 미리 알지 못합니다. 카탈로그는 일반적인 질문 틀이고, 숨겨진 규칙은 `discover`가 데이터에서 찾은 후보를 보고 사람이 판단합니다.
- 랜덤성이 있는 값(탄 퍼짐, 확률)은 분포(p10–p90)로 나옵니다. 샘플이 많을수록 정확해집니다.
- 영상에서 위치를 자동으로 추출하지는 않습니다. 위치·속도가 필요한 항목은 텔레메트리가 필요합니다.

### 7. 명세 공백 목록 → 측정 → CSV 반영 (`gaps`)

공백 목록 CSV(`id, category, subcategory, item, unit, status, current_value, current_source, fill_method, priority, notes`)의
`fill_method=play` 행은 측정 세션으로, `user` 행은 질문지로 바꾸고, 기록한 결과를 다시 CSV에 써 넣습니다.

```bash
python -m scpsl_cleanroom gaps plan  spec_gaps.csv --plan play_measurement_plan.md --project project
python -m scpsl_cleanroom gaps status spec_gaps.csv --project project
python -m scpsl_cleanroom gaps apply spec_gaps.csv --project project    # → spec_gaps.measured.csv + spec/MEASURED.md
```

`plan`이 만드는 것 (`project/measurements/`):

| 파일 | 용도 |
|---|---|
| `session_NN.md` | 세션 체크리스트: 항목, 측정 방법, 변형(empty/tactical, open/close…), 현재 후보값 |
| `session_NN.csv` | 기입 시트. 시도마다 한 줄. 이미 있으면 덮어쓰지 않음 |
| `session_NN.tasks.json` | 어노테이터 과제 파일 |
| `../notes/user_questions.yaml` | 기억으로 답할 질문 (`answer`만 채우기) |
| `../notes/observations.yaml` | 규칙·효과처럼 문장으로 답할 play 항목 |

단위별 자동 계산:

| 단위 | 기록 | 계산 |
|---|---|---|
| s | 어노테이터 `s`/`e` 키 (변형은 `v`), 또는 `time_s` | 끝−시작, 중앙값 |
| m | `distance_m` 또는 `cells` (복도 칸 수 × `CAL_CELL` 행에 잰 칸 길이) | 중앙값 |
| m/s | 거리 + `time_s` | 거리 ÷ 시간 |
| deg | 어노테이터: 조준점 클릭 → 탄착 클릭 (세로 FOV 입력), 또는 `lateral_m` + `distance_m` | atan |
| rgb | 어노테이터: 영상 클릭 (5×5 픽셀 중앙값), 또는 `#RRGGBB` | 채널별 중앙값 |
| HP, fraction | `before`/`after` | 차이 |
| HP/s, fraction/s | `before`/`after` + `time_s` | 차이 ÷ 시간 |
| curve, HP/m, `A vs B` | `x`, `y` 쌍 | 공식 자동 발견 + 그래프 |
| rule, effect … | `note` 또는 notes YAML | 문장 그대로 |

`apply`는 원래 열을 건드리지 않고 `measured_status / measured_value / measured_spread / measured_n / measured_source`
열을 붙입니다. `MEASURED.md`에는 후보값과 10% 이상 다른 측정이 맨 위에 모입니다.

### 클린룸 운영 원칙

1. 관측 팀과 이식 팀을 분리하고, 둘 사이에는 `spec.json` / `SPEC.md`만 오갑니다.
2. 디컴파일된 코드, 코드 스크린샷, 코드에서 옮겨 적은 상수는 스펙에 넣지 않습니다 (guard가 1차로 걸러냄).
3. 스펙의 모든 값은 `observed` / `asset` / `design` 중 하나의 출처를 가져야 합니다 (`validate`).
4. 텔레메트리 원본 파일을 보관해 두면 각 수치의 측정 근거가 됩니다.

이 도구는 법률 자문이 아닙니다. 게임 EULA와 서버 운영 규칙(공식 서버에서 자동화 도구 사용 등)은 직접 확인하세요.

# CLAUDE.md — 로컬 LLM 벤치마크 프로젝트 작업 규칙

> 이 파일은 Claude Code가 자동 로드하는 프로젝트 컨텍스트입니다.
> 이 디렉토리에서 대화할 때마다 아래 규칙을 따르세요.

---

## 🎯 프로젝트 정체성

Apple Silicon 4대 기기(Mac mini 24GB, Studio 32/64/256GB)에서 **MLX**로 로컬 LLM 성능을 벤치마크.
런타임은 **MLX 고정** (Ollama 아님).

---

## 📌 단일 진실 원천 (Single Source of Truth)

| 문서 | 역할 |
|------|------|
| [`PLAN.md`](./PLAN.md) | 테스트 설계·모델 매트릭스·점수 공식·실행 플로우 |
| [`README.md`](./README.md) | 사용자 매뉴얼·설치·실행·체크리스트·트러블슈팅 |
| `CLAUDE.md` (본 문서) | AI 작업 규칙·현재 상태·세션 간 일관성 |

---

## 🔒 필수 규칙

### 규칙 1: 문서 즉시 동기화
설계·스펙·공식·흐름이 바뀌는 결정을 내릴 때는 **같은 턴 안에서** PLAN.md와 README.md 중 해당 문서를 즉시 업데이트.
- 사용자가 "PLAN 업데이트해줘" 라고 말하지 않아도 자동으로.
- 바뀐 내용이 한쪽에만 해당해도 다른 쪽과 충돌 없는지 교차 확인.

### 규칙 2: 응답 전 참조
프로젝트 관련 질문을 받으면 **먼저 PLAN.md와 README.md의 관련 섹션을 읽고** 응답.
추측 답변 금지. 문서와 다르면 문서가 우선이거나, 문서를 업데이트.

### 규칙 3: '시작' 트리거
사용자가 `시작` 이라고 하면:
1. PLAN.md §5 "자동화 실행 흐름" 섹션 읽기
2. `sysctl hw.memsize` 로 현재 기기 RAM 감지
3. 해당 티어 모델 목록으로 `run_benchmark.py` 실행
4. 추가 질문 없이 바로 진행

### 규칙 4: 변경 로그
설계가 바뀌면 아래 §현재 상태 섹션에 한 줄 추가 (날짜 + 변경 요약).

### 규칙 5: 문서 비대화 방지
PLAN.md, README.md는 살아있는 문서. 새 결정으로 **낡은 내용이 생기면 업데이트가 아니라 교체**. "이전에는 X였다가 Y로 변경" 같은 히스토리는 이 CLAUDE.md의 §변경 로그에만 남김.

---

## 📂 디렉토리 구조 (현재)

```
local_llm_benchmark/
├── CLAUDE.md            ← 본 문서 (AI 작업 규칙)
├── PLAN.md              ← 설계
├── README.md            ← 매뉴얼
├── run_benchmark.py     [구현 완료, 1663줄 — MVP 1회 실행 검증]
├── setup.sh             [작성됨 2026-04-15]
├── pull_models.sh       [작성됨 2026-04-15]
├── start.sh             [작성됨 2026-04-15]
├── merge_results.py     [미작성]
├── prompts/
│   ├── l1_recall.json   [작성됨]
│   ├── l2_reasoning.json[작성됨, L2_001 = Phase B 고정 프롬프트]
│   ├── l3_longctx.json  [작성됨, context 본문은 placeholder]
│   └── l4_complex.json  [작성됨]
└── results/
    └── raw/             ← 장문 출력 저장 위치
```

---

## 📍 현재 상태

**Phase**: 🟢 Step 1 설계 보강 완료 (방향성 확정). Step 2 내부 구현 대기.
**마지막 업데이트**: 2026-04-15 (실전 1차 실행 후 보강)

### 확정 결정사항
- 런타임: **MLX** (`mlx-lm`)
- 배포: **Git repo**, 각 기기에서 clone
- 채점: **Mode B** (로컬 실행 중 채점 안 함, 사후 Claude Code에서 Opus 루브릭)
- 동접 N: **1 / 2 / 4 / 8 / 16** (측정은 모두, 유효 동시 공식 상한은 8)
- 긴 답변 저장: **하이브리드** (500 tok 초과는 per-run `raw/*.md`)
- 기기 감지: `sysctl hw.memsize` 자동
- 재현성: `temperature=0`, `seed=42` 고정
- **결과 구조**: per-run 폴더 `results/{hostname}_{YYYYMMDD_HHMMSS}/` (run.jsonl/log/json/md + raw/ + server_logs/)
- **서버 크래시 fail-fast**: ConnectError 2회 연속 + health 실패 → `model_server_dead` 이벤트 후 다음 모델로
- **신뢰성 플래그**: Phase B ok_samples<10 이면 p50/p95 null, MD 에 reliable 컬럼
- **solve_rate**: Phase A 레벨별 `answer_tokens>0` 건수 기록 (thinking cap hit 감지)
- **실행 순서**: think_mode **off 먼저 → on 나중**. 빠르고 안정적인 off 로 기기별 기본 성능 확보 후, 크래시 나면 on 스킵 가능
- **레벨별 프롬프트 개수**: L1=3, L2=3, L3=3, L4=2 (총 11)
- **Phase B 지속**: **180초 시간 고정** (모든 N·모든 모델 공통), 프롬프트 L2_001 고정
- **Phase B `max_tokens`**: think off=**512**, think on=**2048** (2026-04-15 두 차례 수정: 단일 200 → off/on 분리 → off 200→512 실측 answer 255 잘림 해소)
- **Phase A `max_tokens`**: L1=2048, **L2=6144** (4096→상향), **L3=12288** (8192→상향), L4=16384. 2026-04-15 L2/L3 50% 상향: 9B 가 thinking 폭주로 cap hit 하는 현상 완화.
- **powermetrics sudo**: **매번 암호 입력** (시스템 설정 변경 없음). `--no-power` 플래그로 스킵 가능.
- **증분 저장 + 로그** (Level 2):
  - `.jsonl` 에 이벤트 1개당 1줄 append (크래시 시에도 여기까진 보존)
  - `.log` 에 타임스탬프 포함 사람용 로그 (tail -f 가능)
  - 실행 종료 시 jsonl → `.json` + `.md` 로 최종 정리
  - 모델 1개 실패(OOM 등) 시 해당 모델만 `model_error` 이벤트로 기록하고 다음 모델로 계속
- **HF 경로 사전 검증**:
  - `huggingface_hub.repo_exists()` 로 각 모델 실행 직전 존재 확인 (다운로드 전 조기 차단)
  - 실패 시 `model_path_error` 이벤트 + 검색 URL / 전체 mlx-community URL / 수정 위치 안내
  - `TENTATIVE_MODEL_PATHS` 상수에 추정값 모델 등록 → 로그에 "(TENTATIVE 경로)" 마킹
  - `huggingface_hub` 미설치·네트워크 실패 시엔 검증 스킵하고 `mlx_lm.load()` 의 에러 문구에서 repo 관련 키워드 감지해 동일한 안내 제공

### 2026-04-15 설계 보강 (방향성 확정 8건)

**A. 점수 정규화**
- 비교 대상: **같은 모델 × 여러 기기** (모델 품질은 기기 간 동일해야 정상)
- 방식: 절대 임계 + 상대 스케일 혼합
  - TPS: 30 TPS=1.0 (체감 기준 절대 임계)
  - 동시성: 유효 동시 사용자 8/8=1.0
  - 안정성: L4 연속 부하 후 L1 대비 TPS 저하율 ≤10%=1.0
  - 효율성: M4 24GB 베이스라인 TPS/Watt 대비 배율
- `--no-power` 시 효율성 0.10 → 속도/동시성에 0.05씩 재배분

**B. thinking/verbose 토큰 분리 측정**
- Qwen3.5 `<think>...</think>` CoT 때문에 "정답까지 시간"이 모델별로 크게 다름
- `GenerationMetrics` 에 4개 필드 추가:
  - `total_output_tokens` (전체 출력)
  - `thinking_tokens` (CoT 영역)
  - `answer_tokens` (사용자 의미 있는 답변만)
  - `time_to_answer_ms` (</think> 이후 첫 토큰까지)
- 평가 시 **TPS(전체)** + **effective_tps(answer_tokens/time_to_answer)** 둘 다 집계

**C. L3 콘텐츠 — 삼성전자 2024 기업지배구조 보고서**
- 출처: `https://images.samsung.com/kdp/ir/corporate-governance-report/Corporate_Governance_fy_2024_kor.pdf`
- 같은 문서의 다른 섹션을 다른 분량으로 잘라 3문제 구성
- PDF 추출: **`pypdf` 우선**, 문제 시 `pdfplumber` 로 전환
- repo 에는 **텍스트 `.md` 만 커밋** (PDF 원본 X)
- 저장 위치: `prompts/contexts/samsung_governance_2024_{sec3|sec3+4|full}.md`
- **L3 분량 상한 확장**: 8K → **24–32K 토큰** (내부 문서 실사용 패턴과 일치, 24GB 기기에서 KV cache 압박 실측 의도)

**D. 모델 로드 전략 — 서버 경유 통일**
- 기존 설계(Phase A 인프로세스 + Phase B 서버 → 2회 로드) 폐기
- 변경: `mlx_lm.server` 1회 기동 → Phase A/B 모두 HTTP 로 요청
- 이유: 로드 시간 절감 (397B 에서 10분 절감), 코드 단일화, localhost HTTP 오버헤드 <5ms 로 무시 가능
- Phase A 첫 프롬프트가 자연스럽게 warmup 역할
- `mlx_generate_with_timing` 제거 → `send_one_request` 로 통합

**E. 중단/재개 — `--resume` 기본값**
- 기본: 기존 `{hostname}_{date}.jsonl` 있으면 `model_done` 이벤트 모델 자동 스킵, 나머지만 append 로 실행
- `--fresh`: 기존 파일을 `.jsonl.bak.{HHMMSS}` 로 백업 후 새로 시작
- CTRL+C: 현재 모델에 `model_aborted` 이벤트 기록 후 정상 종료
- 파일명 정책: 같은 날 재실행도 `{hostname}_{date}.jsonl` 하나에 누적 (timestamp 이벤트로 세션 구분)

**F. Phase B 동시성 정책**
- 유효 동시 사용자 공식: `min(argmax_N where p95 ≤ 2×single_latency, 8)`
- 측정 N: `[1, 2, 4, 8, 16]` 유지 (16은 "무너짐 관찰"용)
- N 사이 캐시 정리:
  - ≤40GB 모델: **서버 재기동** (명확한 초기화)
  - \>40GB 모델: 60초 idle + `/v1/cache/clear` 시도 (재기동 비용 과다 → 타협)

**G. L4 재설계 — 구조 설계 난이도**
- 단순 텍스트 출력이 아닌 **구조화된 설계** 과제로 재정의
- L4_001 아키텍처 설계서: "10만 사용자 규모 실시간 채팅 백엔드" — 컴포넌트 도식/API/DB 스키마/플로우/장애 시나리오/확장 전략. 출력 1,500–2,500 tok
- L4_002 멀티파일 프로젝트 스캐폴드: "React + FastAPI 할일관리" — 폴더 트리 + 파일별 목적 + 핵심 파일 5+ 실제 코드. 출력 2,000–3,000 tok
- 루브릭: 섹션 완결성 / 실행 가능성 / 내부 참조 정합성 / 깊이

**H. 차트 생성 — matplotlib PNG 4장**
- `merge_results.py` 에서 생성 → `results/COMPARISON.md` 에 임베드
- 차트: ①TPS 히트맵(모델×기기) ②Phase B TPS vs N ③L4 전후 저하율 ④최종 점수 스택 바

### 미결정/보류
- [ ] `Gemma-4-26B-4bit`, `Qwen3-Coder-Next-80B-4bit` 의 정확한 HF repo 경로 (pull 시점 검증)
- [ ] 삼성 PDF 텍스트 추출 및 섹션 분할 (Step 2 시작 시 수행)

---

## 🗺 구현 로드맵 (상세)

### ▶ Step 1: `run_benchmark.py` 뼈대 **[다음 세션 시작점]**

파일 구조:
```python
# 상단 상수
MODELS_BY_TIER = {
    24:  ["qwen3-30b-a3b-4bit", "qwen3-32b-4bit"],
    32:  ["qwen3-30b-a3b-4bit", "qwen3-32b-4bit", "qwen3-32b-8bit"],
    64:  [..., "qwen3-235b-a22b-4bit"],
    256: [..., "deepseek-v3.1", "llama-3.1-405b-4bit"],
}
CONCURRENCY_LEVELS = [1, 2, 4, 8, 16]

# 주요 함수
def detect_device() -> {hostname, ram_gb, chip}
def select_models(ram_gb) -> List[ModelSpec]
def load_prompts() -> Dict[level, List[Prompt]]

class PowerMonitor:  # contextmanager, powermetrics 서브프로세스
    def __enter__/__exit__
    def sample() -> {watts, temp_c}

def run_phase_a(model, prompts) -> PhaseAResult
    for level in [L1,L2,L3,L4]:
        for prompt in prompts[level]:
            metrics = mlx_generate_with_timing(model, prompt)
            save_raw_if_long(metrics)

def run_phase_b(model, levels) -> PhaseBResult
    server = start_mlx_server(model)
    for n in levels:
        result = asyncio.run(burst_test(n, duration_s=60))
    stop_server()

def save_results(hostname, data)  # JSON + MD
def main(): parse_args → select → run → save
```

CLI 인자: `--phase a|b|all`, `--model <name>`, `--dry-run`, `--port 8080`

### ▶ Step 2: `prompts/l*.json` 스키마 및 내용

공통 스키마:
```json
{
  "level": 1,
  "items": [
    {
      "id": "L1_001",
      "question": "대한민국의 수도는?",
      "expected": "서울",              // L1/L2 자동채점용
      "rubric": ["정확성", "...대체"], // L3/L4 Opus 채점용
      "max_tokens": 50,
      "context": null                  // L3에서만 사용 (긴 문서)
    }
  ]
}
```

레벨별 초안 개수: **L1=3 / L2=3 / L3=3 / L4=2** (총 11문항)
L3 문서 소스: 초안 — 공개 기술 블로그 발췌 or 자체 생성 가짜 리포트
L4 API 예시: 초안 — "할일관리 REST API (CRUD + 인증 + DB 스키마)"

### ▶ Step 3: `setup.sh` + `pull_models.sh` + `start.sh`

```bash
# setup.sh (최초 1회)
python3 -m venv .venv
source .venv/bin/activate
pip install mlx-lm httpx numpy

# pull_models.sh (티어 감지 후 해당 모델만)
# RAM → 모델 목록 → huggingface-cli download

# start.sh
source .venv/bin/activate
python3 run_benchmark.py "$@"
```

### ▶ Step 4: `merge_results.py`

4대 기기 결과 JSON glob → 통합 비교표 → `results/COMPARISON.md`
모델별 × 기기별 매트릭스, 최종점수 계산, 차트 선택

### ▶ Step 5: 실행 순서
1. Mac mini 24GB (소형 2개부터, 검증 목적)
2. Studio 32GB
3. Studio 64GB (235B 실패 여부 확인)
4. Studio 256GB (최대 모델까지)
5. 통합 비교 리포트 생성
6. Claude Code에서 Mode B 채점 실행

---

## 🎯 새 세션 시작 시 AI 행동 지침

사용자가 "이어서 작업 계속" 또는 "다음 스텝 진행" 이라고 하면:
1. 이 §현재 상태 / §구현 로드맵 섹션 재확인
2. **미결정 항목** 중 현재 스텝에 필요한 것만 사용자에게 간단히 확인
3. 확정된 부분은 재질문 없이 바로 진행
4. 작업 시작 전 TodoWrite로 현재 스텝의 하위 작업 트래킹 고려

---

## 📝 변경 로그

- 2026-04-15: 프로젝트 시작. PLAN.md, README.md, CLAUDE.md 초안 작성.
- 2026-04-15: 런타임을 Ollama → MLX로 변경.
- 2026-04-15: 점수 공식 개정 (동시성 0.20 신설, 정확도·속도·안정성 각 0.05 이양).
- 2026-04-15: 채점 방식 Mode A(실행 중) → Mode B(사후) 확정.
- 2026-04-15: 구현 로드맵 5단계 상세화 (Step 1~5). 미결정 항목 리스트업.
- 2026-04-15: 미결정 3건 확정 — 프롬프트 개수(초안), Phase B 180초 시간고정 + max_tokens=200, sudo는 매번 암호 입력. Step 1 착수: `run_benchmark.py` 뼈대 + `prompts/l1~l4.json` 작성.
- 2026-04-15: 저장 방식을 Level 2(증분 저장 + 로그)로 확정. `EventLogger` 클래스 도입, 4종 파일 구조(jsonl/log/json/md). 모델별 실패 격리(`model_error` 이벤트). 뼈대 업데이트.
- 2026-04-15: 모델 매트릭스 Qwen3 → **Qwen3.5** 전면 교체. 24GB=9B+35B-A3B, 32GB +Gemma-4-26B +Qwen3.5-27B-Claude-Distilled, 64GB +Coder-Next-80B, 256GB +Qwen3.5-397B-A17B. 기존 Qwen3-235B (130GB, 64GB 시도용) 제외. 실험 범위가 벤더(Qwen·Google·증류)·아키텍처(dense·MoE·코딩특화) 로 확장됨.
- 2026-04-15: HF 경로 사전 검증 로직 추가. `TENTATIVE_MODEL_PATHS` 에 Gemma-4·Coder-Next 등록. 실패 시 `model_path_error` 이벤트로 HF 검색 URL·수정 위치 안내 + 다음 모델로 스킵. `mlx_lm.load()` 단계의 repo 관련 에러도 동일 안내 부가.
- 2026-04-15 (설계 보강): Step 1 방향성 8건 확정 — ①점수 정규화(기기 간 비교 + 절대임계/상대 혼합), ②thinking/answer 토큰 분리(4필드), ③L3 콘텐츠 확정(삼성 지배구조 보고서, 상한 24–32K로 확장, pypdf→pdfplumber fallback), ④모델 로드 단일화(서버 경유로 Phase A/B 통일), ⑤`--resume` 기본값 + `--fresh` 리셋, ⑥Phase B 유효 동시 상한 8, N 사이 캐시 정리(40GB 경계), ⑦L4 구조 설계로 재정의, ⑧matplotlib PNG 4장. `mlx_generate_with_timing` 제거 예정 → `send_one_request` 로 통합.
- 2026-04-15 (max_tokens 상향): Phase A L2 4096→6144, L3 8192→12288 (50% 상향). 9B 에서 L2_002·L3 전부 cap hit 로 answer_tokens=0 된 문제 완화. 정상 모델은 자연 종료하므로 시간 부담 제한적. Phase B off 200→512 상향 — L2_001 실측 answer 255 토큰이 200 에서 잘리는 문제 발견 후 2배 여유로 상향.
- 2026-04-15 (실전 1차 보강 5건): 9B/35B-A3B 첫 실행 후 발견된 문제 대응.
  ①**PowerMonitor 재작성**: peak 830W·temp 0°C 버그 수정. package_watts/cpu_power/gpu_power 단위 명시 분기, 500W 상한 outlier 드롭, smc 샘플러 추가해 온도 센서 실측.
  ②**서버 크래시 감지 + fail-fast**: `ServerDeadError` + `is_server_alive()` 도입. Phase A 에서 ConnectError 2건 연속 + /v1/models 불응답 시 즉시 모델 스킵. Phase B 각 N 시작 전·후 health check. 이 모델에서 서버 죽으면 `model_server_dead` 이벤트 기록 후 **다음 모델로 바로**. (35B-A3B 가 30분 내내 실패 요청 스팸한 문제 해결)
  ③**서버 stderr 캡처**: `start_mlx_server(log_path=...)` 로 서브프로세스 stdout/stderr 를 파일로 직접 리다이렉트. 크래시 원인(OOM 등) 진단 가능. per-run 폴더의 `server_logs/` 에 저장.
  ④**per-run 결과 폴더**: `results/{hostname}_{YYYYMMDD_HHMMSS}/` 구조로 전환. 실행마다 새 폴더. 미완료(run_done 없거나 aborted=True) 폴더만 --resume 으로 이어감. 이전 평면 파일 (`{stem}.jsonl` 등) 방식은 하위 호환만.
  ⑤**unreliable + solve_rate 지표**: Phase B ok_samples<10 이면 reliable=false 플래그 + p50/p95 null. MD 리포트에 "reliable" 컬럼. Phase A 요약에 `n_solved` (answer_tokens>0 건수) 추가 → "solve" 컬럼 표시. model 단위 status 가 "done" 이지만 실제 전부 실패면 "unreliable" 로 재조정.
- 2026-04-15 (Phase B max_tokens 버그 수정): 단일 상수 `PHASE_B_MAX_TOKENS=200` → 모드별 분리 `PHASE_B_MAX_TOKENS_OFF=200` / `PHASE_B_MAX_TOKENS_ON=2048`. 이유: thinking on 에서 CoT 가 200 토큰을 넘어가 `</think>` 전에 잘리면 `answer_tokens=0` 이 되어 `tps_effective` 무의미. `burst_test` 가 think_mode 로 분기해 적정 값 사용. 미사용 필드 `_phase_b_max_tokens_override` 도 제거.
- 2026-04-15 (배포 스크립트): `setup.sh` / `pull_models.sh` / `start.sh` 3종 작성. `pull_models.sh` 는 `run_benchmark.py` 의 `detect_device()`·`select_models()`·`TENTATIVE_MODEL_PATHS` 를 Python import 로 재사용(진실 원천 1개 유지). `start.sh` 는 `--no-power`/`--dry-run`/`--finalize-only` 인자 감지해서 sudo 래핑 여부 자동 결정. README 모델 매트릭스를 Qwen3→Qwen3.5 로 동기화, 결과 배포는 git push/pull 에서 **rsync 직접 복사**로 변경 (`.gitignore` 와 충돌 해소).

---

## 🔗 외부 참조

- 사용자 메모리: `/Users/gv/.claude/projects/-Users-gv-AI/memory/local_llm_benchmark.md`
- 하드웨어 정보: `/Users/gv/.claude/projects/-Users-gv-AI/memory/hardware.md`

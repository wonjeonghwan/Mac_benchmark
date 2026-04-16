# Local LLM Benchmark (Apple Silicon, MLX)

Mac mini M4 24GB, Mac Studio 32/64/256GB **4대 기기**에서 동일한 로컬 LLM 모델을 돌리고 **속도·품질·안정성·동시성** 을 비교 측정하는 벤치마크.

런타임: **MLX** (`mlx-lm`). Ollama 미사용.

계획서 전문 → [`PLAN.md`](./PLAN.md)

---

## ✅ 사전 요구사항

- **macOS** + **Apple Silicon (arm64)** — MLX 전용. Intel Mac / Linux 미지원.
- **Python 3.10+** — `uv` 가 있으면 자동 사용, 없으면 `python3.10~3.13` 또는 `python3` 자동 탐색.
- **디스크 여유** — 모델당 20~130GB (티어별 누적). 24GB 기기는 50GB 이상 여유 권장.
- **sudo 권한** — `powermetrics` 전력 측정 시 필요. 측정 스킵하려면 `./start.sh --no-power`.

---

## 🚀 Quick Start (각 기기에서)

```bash
# 1. 클론
git clone <repo-url> ~/local_llm_benchmark
cd ~/local_llm_benchmark

# 2. 환경 세팅 (최초 1회)
./setup.sh
# 내부 동작:
#   - macOS / Apple Silicon 확인
#   - uv 또는 python3.10+ 자동 탐색 → .venv 생성
#   - requirements.txt 설치 (mlx-lm, httpx, pypdf, huggingface_hub, hf_transfer)

# 3. 모델 다운로드 (해당 기기 티어에 맞는 것만)
./pull_models.sh
# RAM 자동 감지 → 맞는 모델만 HF에서 pull

# 4. 벤치마크 실행 ('시작')
./start.sh
# 내부: run_benchmark.py 호출
#   - Phase A (L1→L2→L3→L4) × 모델별
#   - Phase B (동접 1/2/4/8/16) × 모델별
#   - 결과 저장: results/{hostname}_{date}.json + raw/

# 5. 결과 확인
cat results/$(hostname)_*.md
```

---

## 📋 테스트 구조 요약

### Phase A — 품질/속도 (4단계, 각 프롬프트 × think_mode=off/on)
| 레벨 | 프롬프트 | max_tokens | 주요 측정 |
|------|----------|------|-----------|
| L1 단순회상 | 수도/화학식 3문항 | 2,048 | TTFT, 로딩 |
| L2 논리추론 | 사과 계산·삼단논법·코드디버깅 3문항 | 6,144 | Sustained TPS |
| L3 장문맥락 | 6K/12K/24K 삼성 지배구조 보고서 요약 | 12,288 | 대역폭, prefill, KV cache 압박 |
| L4 복합창작 | 아키텍처 설계서, 멀티파일 스캐폴드 | 16,384 | 연속부하 TPS, 구조 일관성 |

**실행 순서**: 각 모델에서 `think_mode=off 먼저 → on 나중`. off 가 빠르고 안정적이라 기기별 기본 성능 확보 후 on 진행. off 단계에서 서버 크래시 감지되면 on 스킵 후 다음 모델로.

**지표 4필드 (thinking/answer 분리)**: `total_output_tokens` / `thinking_tokens` / `answer_tokens` / `time_to_answer_ms`. TPS도 `tps_total`(전체)와 `tps_effective`(answer만) 둘 다 수집. `solve`(answer_tokens>0 건수/전체) 지표로 thinking 폭주 감지.

### Phase B — 동시 사용자
`mlx_lm.server` 에 N=1/2/4/8/16 동시 요청, 각 180초. **프롬프트 L2_001 고정**, `max_tokens`는 **off=512 / on=2048** (off 는 L2_001 실측 255 토큰 답변 수용, on 은 thinking CoT 1K+ 수용).

**유효 동접** = `min(p95 ≤ 2 × single_latency 를 유지하는 최대 N, 8)` — **reliable 버스트만 고려** (ok_samples ≥ 10).

**신뢰성 플래그**: ok_samples<10 (서버 크래시로 1~2건만 '성공') 이면 `reliable=false`, p50/p95 `null`. fake 레이턴시가 집계에 섞이지 않음.

---

## 🖥 기기별 실행 모델 매트릭스

| 기기 | RAM | 자동 실행 모델 (티어 누적) |
|------|-----|-----------------|
| Mac mini M4 | 24GB | Qwen3.5-9B-4bit, Qwen3.5-35B-A3B-4bit |
| Studio | 32GB | 위 + Gemma-4-26B-4bit ⚠, Qwen3.5-27B-Claude-Distilled-4bit |
| Studio | 64GB | 위 + Qwen3-Coder-Next-80B-4bit ⚠ |
| Studio | 256GB | 위 + Qwen3.5-397B-A17B-4bit |

> 기기 RAM은 `sysctl hw.memsize` 로 자동 감지 → 해당 티어 모델만 실행.
> ⚠ 표시는 HF repo 경로 미확정 (`TENTATIVE_MODEL_PATHS`). pull 시 없으면 자동 스킵.

---

## 🧪 사용 명령어 (전체 플로우)

### 세팅 (최초 1회, 기기마다)
```bash
./setup.sh                    # Python venv + 의존성
./pull_models.sh              # 해당 기기 티어 모델만 HF pull
```

### 실행
```bash
./start.sh                    # 전체 벤치마크 (Phase A + B)
./start.sh --phase a          # Phase A만
./start.sh --phase b          # Phase B만
./start.sh --model qwen3-30b  # 특정 모델만
./start.sh --dry-run          # 실제 실행 없이 계획만 출력
```

### 결과 확인
```bash
ls results/                   # 기기별 JSON + MD
open results/*.md             # 마크다운 리포트 열기
```

### 전체 4대 결과 모으기 (벤치 완료 후)

`.gitignore` 가 `results/*/` 전체를 제외하므로 **결과는 git 으로 배포하지 않음**.
대신 각 기기에서 메인(통합) 기기로 per-run 폴더 자체를 직접 복사:

```bash
# 각 기기 → 메인 기기 (예: Mac Studio 256GB 이 메인)
rsync -av results/ studio256:/Users/gv/AI/local_llm_benchmark/results/

# 메인 기기에서 통합 (모든 {hostname}_* 폴더 스캔)
python3 merge_results.py      # → results/COMPARISON.md + charts/ 생성 (※ 아직 미구현)
```

### 품질 채점 (Claude Code에서 실행)
```
Claude Code 세션에서:
> 채점 시작 → results/raw/ 를 읽고 Opus로 루브릭 평가
```
(로컬 자동채점 아님. Mode B 선택 — API 키 분산 불필요.)

---

## ⚠️ 진행 중 확인할 점

### 실행 전
- [ ] `pip list | grep mlx-lm` 으로 설치 확인
- [ ] `df -h ~` 로 디스크 여유 확인 (모델당 20~130GB)
- [ ] 다른 무거운 앱 종료 (Chrome, Docker 등) — 메모리 경합 방지
- [ ] 전원 어댑터 연결 (노트북인 경우 스로틀링 방지)

### 실행 중
- [ ] **메모리 압박**: `vm_stat` / Activity Monitor 의 Memory Pressure 확인
  - 노랑/빨강 뜨면 해당 모델이 해당 기기 한계 초과
- [ ] **스로틀링**: `sudo powermetrics --samplers smc -i 2000 | grep -E "temp|throttle"` 별도 터미널에서 모니터링
- [ ] **OOM**: 235B 같은 대형 모델은 64GB 기기에서 실패 가능 — 실패 로그도 결과임
- [ ] **디스크 I/O**: 모델 로딩 중 SSD 쓰기 과다하면 메모리 스왑 발생 중

### 실행 후
- [ ] `results/{hostname}_*.json` 생성 확인
- [ ] `results/raw/` 에 장문 답변 파일 생성 확인
- [ ] 실패한 케이스 로그 (`error_log` 필드) 검토
- [ ] Git commit & push → 다른 기기에서 비교 가능

---

## 📂 디렉토리 구조

```
local_llm_benchmark/
├── CLAUDE.md                 # AI 작업 규칙 + 현재 상태
├── PLAN.md                   # 상세 계획 (테스트 설계 전체)
├── README.md                 # 본 문서
├── setup.sh                  # 최초 1회: venv + 의존성 설치
├── pull_models.sh            # 기기 티어에 맞는 HF 모델 사전 다운로드
├── start.sh                  # '시작' 진입점 (powermetrics sudo 래핑)
├── run_benchmark.py          # 메인 벤치마크 로직 (MLX 서버 경유)
├── requirements.txt          # mlx-lm, httpx, pypdf, huggingface_hub, hf_transfer
├── .gitignore                # venv/results/bak 제외
├── merge_results.py          # 4대 결과 병합 (※ 아직 미구현)
├── prompts/
│   ├── l1_recall.json
│   ├── l2_reasoning.json
│   ├── l3_longctx.json
│   ├── l4_complex.json
│   └── contexts/
│       ├── samsung_governance_2024_sec3.md    # ~6K tok
│       ├── samsung_governance_2024_sec3_4.md  # ~12K tok
│       └── samsung_governance_2024_full.md    # ~24–32K tok
├── tools/
│   └── extract_samsung_pdf.py                 # L3 컨텍스트 생성기
└── results/                                  # 실행마다 새 per-run 폴더
    ├── {hostname}_{YYYYMMDD_HHMMSS}/         # git ignore
    │   ├── run.{jsonl,log,json,md}          # 해당 실행 결과
    │   ├── raw/                             # 장문 답변 전문
    │   └── server_logs/                     # mlx_lm.server stdout/stderr (크래시 진단)
    └── COMPARISON.md                         # 메인 기기에서 통합 (git 커밋 대상)
```

---

## 📊 최종 점수 공식

```
최종 = 정확도(0.30) + TPS(0.25) + 동시성(0.20) + 안정성(0.15) + 효율성(0.10)
```

- **정확도**: Claude Opus 루브릭 채점 (Mode B)
- **TPS**: L2 지속 tok/s (기준)
- **동시성**: 유효 동접 사용자 수
- **안정성**: L4 연속부하 후 TPS 저하율 (10% 이내 우수)
- **효율성**: TPS / Watt

---

## 🔧 트러블슈팅

| 증상 | 원인 | 대응 |
|------|------|------|
| `mlx_lm` import 에러 | Python venv 활성화 안됨 | `source .venv/bin/activate` |
| HF 다운로드 느림 | 네트워크 | `HF_HUB_ENABLE_HF_TRANSFER=1` |
| 모델 로딩 중 크래시 | OOM | 자동 감지 → `model_server_dead` 기록 후 다음 모델로. `results/{run}/server_logs/server_{model}.log` 에서 원인 확인 |
| Phase B 가 p50=0 / fail=100% | 서버 사망 | reliable=false 로 표시. 해당 버스트 무효 |
| 서버 포트 충돌 | 8080 사용중 | `./start.sh --port 18080` |
| powermetrics 권한 | sudo 필요 | `./start.sh` 가 자동 sudo 래핑. `--no-power` 로 스킵 가능 |
| peak 전력 비현실적 | plist 파싱 오류 | 500W 초과 샘플 자동 드롭 (M4~30W / Studio 256~300W) |

---

## 🔗 관련 문서
- [PLAN.md](./PLAN.md) — 테스트 설계 전체 + 점수 공식 근거
- Apple MLX: https://github.com/ml-explore/mlx
- mlx-lm: https://github.com/ml-explore/mlx-lm

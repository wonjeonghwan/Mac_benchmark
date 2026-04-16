# 로컬 LLM 벤치마크 계획서

> **'시작' 명령 시**: 이 문서를 읽고 현재 기기 RAM을 감지해 해당 티어의 모델만 자동 실행 → 결과 리포트 출력.
> 런타임: **MLX** (`mlx-lm`). Ollama 미사용.

---

## 1. 테스트 기기 및 모델 매트릭스

> 2026-04-15 개정: Qwen3 → Qwen3.5 전면 교체. Gemma 4, Qwen3-Coder-Next 추가로 벤더·특화 다양성 확보.

| # | 기기 | RAM | 실행 모델 (티어 누적) |
|---|------|-----|----------------------|
| 1 | Mac mini M4 | 24GB | Qwen3.5-9B Q4, Qwen3.5-35B-A3B Q4 |
| 2 | Mac Studio | 32GB | 위 2개 + Gemma-4-26B Q4, Qwen3.5-27B-Claude-Distilled Q4 |
| 3 | Mac Studio | 64GB | 위 4개 + Qwen3-Coder-Next-80B Q4 |
| 4 | Mac Studio | 256GB | 위 5개 + Qwen3.5-397B-A17B Q4 (플래그십 MoE) |

### MLX 모델 경로 및 대략 크기

| 경로 | 크기 | 비고 |
|------|------|------|
| `mlx-community/Qwen3.5-9B-MLX-4bit` | ~5GB | 소형 속도 상한 |
| `mlx-community/Qwen3.5-35B-A3B-4bit` | ~20GB | MoE 주력 (활성 3.3B / 전체 35B) |
| `mlx-community/Gemma-4-26B-4bit` | ~14GB | Google 벤더, Apache 2.0 |
| `mlx-community/Qwen3.5-27B-Claude-4.6-Opus-Distilled-MLX-4bit` | ~15GB | Opus 증류 (품질 상한 참고) |
| `mlx-community/Qwen3-Coder-Next-80B-4bit` | ~42GB | 코딩 특화 MoE, L4 과제에 유리 여부 측정 |
| `mlx-community/Qwen3.5-397B-A17B-4bit` | ~200GB | 256GB 전용 플래그십 |

⚠ Gemma-4, Qwen3-Coder-Next 의 repo 명명은 pull 시 HF 에서 실제 경로 검증 후 반영.

---

## 2. 테스트 구조

> **실행 아키텍처**: `mlx_lm.server` **1회 기동** → Phase A/B 모두 HTTP 요청으로 통일. 모델당 로드 1회, OpenAI 호환 엔드포인트 (`localhost:8080/v1`) 공유.

### Phase A — 품질/속도 (4단계 프롬프트)

| 레벨 | 목적 | 프롬프트 예 | 출력 크기 | 주요 측정 |
|------|------|-------------|-----------|-----------|
| L1 단순 회상 | 로딩/TTFT | "대한민국 수도는?", "물 화학식은?" | 1~3문장 | TTFT, 초기 로딩 |
| L2 논리 추론 | 지속 TPS | 수학 문장제, 삼단논법, 코드 디버깅 | max 6,144 tok | Sustained TPS, CPU/GPU 점유, 스로틀링 |
| L3 장문 맥락 | 메모리 대역폭 | **6K / 12K / 24–32K** 문서 3종 요약·추출·표정리 | max 12,288 tok | Prefill 속도, DRAM 피크, KV cache 압박 |
| L4 복합 창작 | 최대 부하 | 아키텍처 설계서, 멀티파일 코드 스캐폴드 | 1,500–3,000 tok | 연속 부하 TPS 저하율, 열/전력, 구조 일관성 |

**L3 콘텐츠 출처**: 삼성전자 2024 기업지배구조 보고서 ([원문 PDF](https://images.samsung.com/kdp/ir/corporate-governance-report/Corporate_Governance_fy_2024_kor.pdf)) — 같은 문서의 다른 섹션을 다른 분량으로 분할 사용. PDF 텍스트 추출은 `pypdf` (실패 시 `pdfplumber`). repo 에는 추출된 `.md` 만 커밋.

**L4 재설계** (단순 텍스트 출력이 아닌 구조 설계):
- L4_001 아키텍처 설계서 (컴포넌트·API·스키마·장애 시나리오)
- L4_002 멀티파일 프로젝트 스캐폴드 (폴더 트리 + 파일별 핵심 코드)

### thinking / answer 토큰 분리 측정

Qwen3.5 계열은 `<think>...</think>` CoT 출력이 길어 "정답까지 시간"이 모델별로 크게 다름. 아래 4 지표를 분리 수집:

| 지표 | 의미 |
|------|------|
| `total_output_tokens` | 전체 출력 (thinking 포함) |
| `thinking_tokens` | `<think>...</think>` 내부 |
| `answer_tokens` | 사용자 의미 있는 답변만 |
| `time_to_answer_ms` | `</think>` 이후 첫 토큰까지 (체감 TTFT) |

→ 평가 시 **전체 TPS** + **effective_tps (answer / time_to_answer)** 둘 다 집계.

### Phase B — 동시 사용자

- 실행: (A 에서 기동한 동일 서버 재사용) 비동기 HTTP 요청
- 동시 세션: **1 / 2 / 4 / 8 / 16**
- 프롬프트: **L2_001 고정** (사과 상자 계산 문제)
- `max_tokens`: **think_mode=off → 512** (L2_001 실측 answer 255 토큰 → 2배 여유) / **think_mode=on → 2048** (thinking 985+ 여유분). 2026-04-15 off=200 → 512 로 상향 (답변 잘림 해소).
- 지속시간: **각 N 레벨당 180초 고정** (모든 모델·기기 동일 관찰창)
- **N 사이 캐시 정리**:
  - 모델 ≤40GB: **서버 재기동** (명확한 초기화)
  - 모델 \>40GB: 60초 idle + `/v1/cache/clear` 시도 (재기동 비용 과다 → 타협)
- 측정:
  - 총 처리량 (aggregate TPS)
  - p50 / p95 응답시간
  - 실패율
  - 메모리 압박 시점
- **"유효 동시 사용자 수"** = `min(p95 ≤ 2×싱글레이턴시를 유지하는 최대 N, 8)` — 상한 8

---

## 3. 측정 지표

### 정량
| 지표 | 설명 | 합격선 |
|------|------|--------|
| TTFT | 첫 토큰까지 | < 500ms 우수 |
| time_to_answer (t2a) | `</think>` 이후 첫 토큰까지 (체감 TTFT) | 짧을수록 좋음 |
| tps_total | 전체 생성 TPS (thinking 포함) | 모델별 상대 비교 |
| tps_effective | 답변 TPS (`answer_tokens / t2a`) | thinking 체감 영향 반영 |
| **solve** | Phase A 레벨별 `answer_tokens > 0` 건수 | 예: 3/3 → cap hit 없음, 0/3 → thinking 폭주 |
| agg_tps | Phase B 총 처리량 (초당 총 토큰) | N 증가 시 **상승**해야 확장성 있음 |
| ok_samples | Phase B 성공 요청 수 | <10 이면 `reliable=false` (fake 데이터 방지) |
| 지속 TPS 저하율 | L4 연속 후 L1 대비 | ≤ 10% 안정 |
| 메모리 피크 | RAM 최대 사용 | 한도 대비 % |
| 전력 효율 | tps_total / avg_watts | 높을수록 우수 |
| 온도 안정성 | peak_temp_c (smc 센서) | 스로틀링 없음 |
| 유효 동시 사용자 | reliable 버스트 기반, `min(p95 ≤ 2×single, 8)` | 기기별 절대치 |

### 정성
- 정확도 (L1~2 정답/오답, L3~4 루브릭 채점)
- 일관성 (동일 질문 5회 반복 → BLEU/ROUGE)
- 지시 이행도 (체크리스트 0~5점)
- L4 전후 품질 저하 여부

---

## 4. 최종 점수 (동시성 반영 개정)

```
최종 = 정확도(0.30) + 속도/TPS(0.25) + 안정성(0.15) + 효율성(0.10) + 동시성(0.20)
```

| 항목 | 가중치 | 근거 |
|------|--------|------|
| 정확도 | 0.30 | 품질이 최우선 |
| 속도/TPS | 0.25 | 체감 성능 |
| 동시성 | 0.20 | 서빙 용도 차별화 (신규) |
| 안정성 | 0.15 | 장시간 운용 |
| 효율성 | 0.10 | TPS/Watt |

> *기존 0.35/0.30/0.20/0.15 → 동시성 0.20 확보 위해 정확도·속도·안정성에서 각 0.05씩 이양.*

### 정규화 방식 (0–1 매핑)

비교 대상은 **같은 모델 × 여러 기기**. 모델 품질은 기기 간 동일해야 정상이며, 차이 나면 구현 버그로 취급.

| 항목 | 매핑 |
|------|------|
| 정확도 | L1/L2 자동채점(정답 포함 여부) + L3/L4 Opus 루브릭 0–5점 → 100점 만점 환산 후 /100 |
| 속도/TPS | 30 TPS = 1.0 (체감 기준 절대 임계). 60 TPS 이상은 1.0 클램프 |
| 동시성 | 유효 동시 사용자 N / 8 (상한 8) |
| 안정성 | L4 연속 부하 후 L1 대비 TPS 저하율 ≤10% = 1.0, 30% 이상 = 0.0 선형 |
| 효율성 | 24GB Mac mini 베이스라인 TPS/Watt 대비 배율. 동일 = 1.0, 2배 이상 = 1.0 클램프 |

**`--no-power` 시**: 효율성 가중치 0.10을 속도 +0.05, 동시성 +0.05 로 재배분.

---

## 5. 자동화 실행 흐름 ('시작' 트리거)

```
1. 기기 감지: sysctl hw.memsize → RAM 용량 파악
2. 티어별 모델 목록 선택 (§1 표 기반)
3. --resume 처리: 기존 jsonl 에서 model_done 이벤트 있는 모델 스킵
4. powermetrics 백그라운드 샘플링 시작 (전력/온도)
5. 각 모델에 대해:
   a. HF 경로 검증 → 없으면 스킵
   b. mlx_lm.server 기동 (로드 1회)
   c. **think_mode = off 먼저** → on 나중 (서버는 재로드 안 함, 같은 서버 재사용):
      - Phase A: L1 → L2 → L3 → L4 순차 HTTP 요청 (스트림). total/thinking/answer 토큰 분리 집계
      - Phase B: N = 1/2/4/8/16 버스트 (≤40GB: N 사이 서버 재기동 / >40GB: 60초 idle + cache_clear)
   d. 서버 종료 + 모델 언로드
   *off 단계에서 서버 크래시 감지되면 on 은 스킵하고 다음 모델로*
6. jsonl → json + md 최종 정리 (finalize_results)
7. 요약 콘솔 출력
```

### 재실행 정책 (2026-04-15 per-run 폴더 전환 반영)
- **기본 (`--resume` 동작)**: 최근 `{hostname}_*` 폴더가 미완료(`run_done` 없거나 `aborted=True`)면 그 폴더의 `run.jsonl` 에 append. 이미 `model_done` 있는 (model, think_mode) 쌍 자동 스킵. 완료된 폴더만 있으면 새 timestamp 폴더 생성.
- **`--fresh`**: Resume 무시하고 항상 새 `{hostname}_{YYYYMMDD_HHMMSS}/` 폴더 생성.
- **CTRL+C**: 현재 모델에 `model_aborted` 기록 후 정상 종료 → 다음 `--resume` 이 같은 폴더 이어감.
- **서버 크래시**: `model_server_dead` 이벤트 기록 후 해당 모델 스킵, 다음 모델 시작. 다른 모델에 전염 X.

---

## 6. 산출물 구조

```
/Users/gv/AI/local_llm_benchmark/
├── PLAN.md                     # 본 문서
├── CLAUDE.md                   # AI 작업 규칙 + 현재 상태
├── README.md                   # 사용자 매뉴얼
├── run_benchmark.py            # 메인 실행 (MLX 서버 경유 통일)
├── scorer.py                   # 품질 채점 (Mode B, Opus 루브릭) — 미작성
├── merge_results.py            # 4대 결과 통합 + matplotlib 차트 4장 — 미작성
├── setup.sh / pull_models.sh / start.sh
├── prompts/
│   ├── l1_recall.json
│   ├── l2_reasoning.json
│   ├── l3_longctx.json
│   ├── l4_complex.json
│   └── contexts/               # L3 실제 문서 본문 (.md)
│       ├── samsung_governance_2024_sec3.md       # ~6K tok
│       ├── samsung_governance_2024_sec3_4.md     # ~12K tok
│       └── samsung_governance_2024_full.md       # ~24-32K tok
└── results/                                # 실행마다 per-run 폴더 생성
    ├── {hostname}_{YYYYMMDD_HHMMSS}/
    │   ├── run.jsonl                       # 이벤트 스트림 (증분 append)
    │   ├── run.log                         # 사람용 타임스탬프 로그
    │   ├── run.json                        # 최종 구조화 결과
    │   ├── run.md                          # 요약 리포트
    │   ├── raw/{model}_{mode}_{prompt_id}.md   # 장문 출력 전문
    │   └── server_logs/server_{model}.log  # mlx_lm.server stdout/stderr (크래시 진단)
    ├── COMPARISON.md                       # 4대 통합 리포트 (merge 산출물)
    └── charts/                             # matplotlib PNG 4장
        ├── tps_heatmap.png
        ├── phase_b_tps_vs_n.png
        ├── l4_degradation.png
        └── final_score_stacked.png
```

**Resume 정책**: 다음 실행 시 최근 `{hostname}_*` 폴더가 **미완료**(run_done 없거나 aborted=True) 이면 재사용. 완료됐으면 새 timestamp 폴더 생성. `--fresh` 로 강제 새로 시작.

---

## 7. 다음 단계 (체크리스트)

- [x] **Step 0**: PLAN.md 저장
- [x] **Step 1a**: 프롬프트셋 4개 JSON 작성
- [x] **Step 1b**: `run_benchmark.py` 뼈대 작성
- [x] **Step 1c**: 설계 방향성 8건 확정 (A–H)
- [x] **Step 2**: `run_benchmark.py` 내부 구현 — 완료 (detect_device, load_prompts, send_one_request, PowerMonitor, burst_test, ServerManager, finalize_results, --resume 전부)
- [x] **Step 2b**: L3 context 추출 — 완료 (삼성 PDF 섹션 3종)
- [x] **Step 4**: `setup.sh` / `pull_models.sh` / `start.sh` — 완료
- [x] **Step 5**: Mac mini 24GB 첫 실행 — 완료 (9B 성공, 35B-A3B OOM 확인)
- [x] **Step 5a** (2026-04-15 보강): 서버 크래시 감지/fail-fast + PowerMonitor 버그 수정 + per-run 폴더 + unreliable 플래그 + solve_rate 지표
- [ ] **Step 5b**: Mac mini 에서 보강 반영 후 재실행 (데이터 품질 확인)
- [ ] **Step 6**: Studio 32 / 64 / 256GB 순차 실행
- [ ] **Step 3**: `scorer.py` — Mode B Opus 루브릭 채점 (모든 기기 끝난 후)
- [ ] **Step 7**: 4대 통합 비교 리포트 (`merge_results.py` + matplotlib 차트 4장)

---

## 8. 운용 메모

- `mlx_lm.server` 는 OpenAI 호환 엔드포인트 (`localhost:8080/v1`)
- **Qwen3.5-397B-A17B** (~200GB) 는 **256GB 전용**. 64GB 기기엔 아예 안 넣음.
- Qwen3-Coder-Next-80B (~42GB) 는 32GB 에선 OOM 가능 → 64GB 이상부터 실행
- Distilled 모델 해석 주의: 측정 대상이 Qwen 베이스가 아니라 증류 소스(Opus)의 능력을 내재화한 버전임. 단순 "Qwen 품질" 지표로 오해하지 말 것.
- 재현성 위해 모든 실행은 `temperature=0`, `seed=42` 고정

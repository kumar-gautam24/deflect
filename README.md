# Deflect

Answers FastAPI support questions from the official documentation with citations, and
escalates to a human when its own confidence signals say it should not guess.

Next.js, FastAPI, Postgres with pgvector, Gemini for generation and judging, local
models for embedding and reranking.

## Why the refusal matters

A support assistant that answers everything is worse than one that answers less. A
confidently wrong answer costs a support team more than no answer, because someone has
to discover it and undo it. Deflect measures both rates rather than optimising one.

The interesting part of this project is not the retrieval pipeline. It is the eval
harness that tells you when the pipeline is wrong, and it gates CI.

## Results

All numbers below are measured against the ingested corpus (fastapi/fastapi at
`95f8322`, `docs/en/docs`: 155 documents, 2,370 chunks) and the 80-item golden dataset
in `evals/golden.yaml`. Reproduce them with the scripts named under each table.

### Retrieval ablation

`services/api/scripts/ablate.py`, over the 65 answerable items.

| variant | hit@5 | MRR | precision@5 |
| --- | --- | --- | --- |
| dense only | 0.892 | 0.744 | 0.206 |
| lexical only | 0.323 | 0.291 | 0.126 |
| hybrid | **0.892** | **0.762** | 0.203 |
| hybrid + rerank | 0.862 | 0.706 | 0.194 |

Two honest readings of this table:

**Hybrid retrieval earns its place, modestly.** Lexical search alone is poor, but
fusing it with dense search lifts MRR by 1.8 points at identical hit@5. It contributes
rank signal on exact tokens such as `422` and `Depends` that embeddings blur.

**Reranking makes retrieval worse.** It costs 3 points of hit@5 and 5.6 of MRR. RRF
already orders the top 20 well, and reranking reshuffles them and keeps 5, so any
correct document the cross-encoder ranks sixth or lower is pushed out of the window.
Three stronger cross-encoders were tried; none beat plain hybrid on MRR:

| reranker | hit@5 | MRR |
| --- | --- | --- |
| none (hybrid) | 0.892 | **0.762** |
| `ms-marco-MiniLM-L-6-v2` | 0.862 | 0.706 |
| `ms-marco-MiniLM-L-12-v2` | 0.892 | 0.736 |
| `BAAI/bge-reranker-base` | 0.877 | 0.746 |
| `jina-reranker-v1-turbo-en` | **0.908** | 0.739 |

### Why the reranker stays anyway

`services/api/scripts/gate_separation.py`

| score source | answerable median | unanswerable median | separation |
| --- | --- | --- | --- |
| RRF fused (no rerank) | 0.0164 | 0.0164 | 0.0000 |
| cross-encoder rerank | 4.6359 | -1.0445 | **5.6804** |

Reciprocal Rank Fusion scores carry no relevance information at all. The top-ranked
chunk always scores `1/(k+1)` regardless of whether it answers the question, which is
why every query in the dataset produces the same 0.0164. There is nothing to threshold
on.

The cross-encoder is not in this pipeline to improve retrieval. It is the only stage
that produces a calibrated relevance score, and the escalation gate is built on it.
Removing it would cost 3 points of hit@5 and remove the ability to refuse at all.

`ms-marco-MiniLM-L-6-v2` is kept over `jina-turbo` despite jina's better hit@5: its
median separation is 5.68 against jina's 0.78, which makes the threshold far less
sensitive to where it is set.

### Choosing the operating point

`services/api/scripts/sweep_thresholds.py`. Abridged; the script prints the full sweep
from -8.0 to +8.0.

| min_top_score | answered | wrongly refused | wrongly answered |
| --- | --- | --- | --- |
| -1.00 | 0.92 | 0.08 | 0.47 |
| 0.00 | 0.91 | 0.09 | 0.33 |
| 1.50 | 0.85 | 0.15 | 0.13 |
| **2.00** | **0.83** | **0.17** | **0.13** |
| 3.50 | 0.71 | 0.29 | 0.07 |
| 5.00 | 0.45 | 0.55 | 0.07 |
| 8.00 | 0.08 | 0.92 | 0.00 |

The operating point is **2.0**: it answers 83% of answerable questions while passing
only 13% of unanswerable ones through to the second check.

Two caveats that belong with this number rather than buried:

- The sweep holds `grounded=True` to isolate the retrieval signal. In production the
  model must also report that its answer is supported by the retrieved passages, so
  the end-to-end wrongly-answered rate is lower than this table shows. Retrieval score
  is the coarse filter; groundedness is the fine one.
- With 15 unanswerable items, each one is 6.7 percentage points. The resolution of the
  right-hand column is coarse, and 0.07 means a single question.

## Architecture

```
apps/web         Next.js: ask, evals and traces surfaces
services/api     FastAPI
  ingest/        markdown -> heading-aware chunks -> local embeddings -> pgvector
  retrieval/     dense + lexical search, RRF fusion, cross-encoder rerank
  answer/        prompt assembly, citations, confidence gate
  evals/         golden dataset, metrics, LLM-as-judge, run storage
  llm/           provider-agnostic client (Gemini, Ollama)
db               Postgres with pgvector
```

The web app never calls a model. It proxies an SSE stream from the FastAPI service, so
provider keys stay server-side and the eval harness exercises the same answer code path
the live app does. An eval that tests a different pipeline than production measures
nothing.

Chunking follows markdown headings rather than a fixed window, and each chunk keeps its
heading path (`Tutorial > Dependencies > Sub-dependencies`) so a citation names
something a human can navigate to.

## Evals

`evals/golden.yaml` holds 80 items: 65 answerable, and 15 that no document in the
corpus answers, which must be refused. A test verifies every `expected_sources` path
against the ingested corpus, because a typo there would look like a permanent
retrieval regression.

Metrics are split into two families:

- **Retrieval**, deterministic and LLM-free: hit@5, MRR, precision@5
- **Generation**, LLM-as-judge: faithfulness, answer relevance, context relevance,
  plus escalation precision and recall

The split is the point. When a run regresses, the deterministic metrics say
immediately whether retrieval or generation broke. Runs are stored with their commit,
prompt version, model and retrieval config, and the dashboard diffs any two.

CI runs a 10-item smoke set on every pull request and fails the build when
faithfulness drops. The full dataset runs nightly.

## Running it

```bash
docker compose up -d db
createdb deflect_test  # or: psql -c "CREATE DATABASE deflect_test"

cd services/api
uv sync
uv run alembic upgrade head
DATABASE_URL=...deflect_test uv run alembic upgrade head

git clone --depth 1 https://github.com/fastapi/fastapi /tmp/fastapi-src
uv run python scripts/ingest.py /tmp/fastapi-src/docs/en/docs "$(git -C /tmp/fastapi-src rev-parse HEAD)"

uv run uvicorn deflect.main:app --reload
```

```bash
cd apps/web
npm install
npm run dev
```

Set `GEMINI_API_KEY` in `.env` to answer questions and run evals. Everything else,
including the ablation and the threshold sweep, runs without a model provider.

### Tests

```bash
cd services/api && uv run pytest      # 89 tests
cd apps/web && npm test               # 8 tests
```

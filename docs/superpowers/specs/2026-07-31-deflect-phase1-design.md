# Deflect — Phase 1 Design

Date: 2026-07-31
Status: approved

## Problem

Support teams answer the same documented questions repeatedly. A retrieval system can
answer most of them, but only if it reliably refuses the ones it cannot answer — a
confidently wrong support answer costs more than no answer at all.

Deflect answers support questions from a documentation corpus with citations, and
escalates to a human when its own confidence signals say it should not guess.

## Goals

1. Answer documented questions with correct, cited answers.
2. Refuse and escalate undocumented questions rather than hallucinate.
3. Measure both of the above continuously, and catch regressions before merge.

Goal 3 is the primary one. The retrieval and answering pipeline exists to be measured;
the eval harness is the substance of the project, not a supporting script.

## Non-goals (Phase 1)

- Tool-calling agent (Phase 2)
- Human-in-the-loop review feeding back into the dataset (Phase 3)
- Authentication and multi-tenancy
- Multiple document corpora

The schema must not preclude these. Nothing else about them is built.

## Corpus

FastAPI's documentation, ingested from markdown source. Chosen because the source is
clean and version-pinned, the content is verifiable by any Python interviewer on the
spot, and it is familiar enough to write golden answers quickly.

The ingested snapshot is pinned to a commit SHA so eval runs stay comparable.

## Architecture

```
apps/web         Next.js (App Router, TypeScript, Tailwind + shadcn)
services/api     FastAPI, async Python
  ingest/        markdown -> chunk -> embed -> pgvector
  retrieval/     hybrid search, RRF fusion, cross-encoder rerank
  answer/        prompt assembly, streaming, citations, confidence gate
  evals/         golden dataset runner, LLM-as-judge, run storage
  llm/           provider-agnostic client
  telemetry/     OpenTelemetry spans, token and cost accounting
db               Neon Postgres with pgvector
```

The web app never calls an LLM. It talks to FastAPI over HTTP and proxies the SSE
stream through a route handler. Two consequences, both deliberate:

- API keys stay server-side.
- The eval harness and the live app execute the same code path. An eval that tests a
  different pipeline than production measures nothing.

### Models

| Role | Model | Rationale |
|---|---|---|
| Generation | Gemini Flash | Available, fast enough for streaming, cheap enough to re-run evals |
| Judge | Gemini Pro | Stronger judgment where it matters; runs on a bounded dataset |
| Embeddings | `bge-small` (local, fastembed) | No API cost on re-ingest, enables embedding-model ablation |
| Reranker | local cross-encoder | Same |

The LLM client is an interface with two implementations, Gemini and Ollama. It earns
the abstraction: it enables provider comparison inside the eval dashboard and keeps
local inference viable. No other component gets an interface for one implementation.

## Retrieval pipeline

Each stage exists because it is measured, and is removable in config so the ablation
is reproducible.

1. **Chunking** — structure-aware on markdown headings rather than fixed size. Each
   chunk retains its heading path (`Tutorial > Dependencies > Sub-dependencies`), which
   both grounds the citation and gives the model positional context. Size and overlap
   are configurable for sweeps.
2. **Hybrid retrieval** — pgvector cosine similarity and Postgres full-text search, run
   concurrently. Dense retrieval alone misses exact tokens (`Depends`, `422`); lexical
   alone misses paraphrase.
3. **Reciprocal Rank Fusion** — merges the two ranked lists. Chosen over weighted score
   blending because it needs no per-corpus tuning and is stable across score scales.
4. **Cross-encoder rerank** — top 20 down to top 5.

The README carries the ablation table: vector-only, +hybrid, +rerank, each with hit@5
and MRR, so every stage is justified by a number.

## Confidence gate

Three signals, combined into one decision:

- reranker top score
- margin between the top and second chunk
- a groundedness self-check: the generation call returns a structured field asserting
  whether every claim is supported by the provided chunks

The groundedness signal rides on the existing generation call as structured output
rather than a second LLM round trip, keeping the gate off the latency path.

Below threshold, the system refuses, states why, and writes an escalation record.

The threshold is not chosen by intuition. It is swept against the golden dataset to
produce a deflection-rate vs. incorrect-answer-rate curve, and the operating point is
selected from that curve. The curve is a stated output of the project.

## Eval harness

### Golden dataset

Roughly 80 hand-labeled items in version-controlled YAML. Each item carries the
question, an ideal answer, the expected source document(s), and whether it should
escalate. About 15 items are deliberately unanswerable from the corpus, exercising the
refusal path.

### Metrics

Two families, kept separate:

- **Retrieval** (deterministic, no LLM): hit@k, MRR, precision@k against expected
  sources.
- **Generation** (LLM-as-judge): faithfulness, answer relevance, context relevance,
  plus escalation precision and recall.

The separation is the point. When a run regresses, the deterministic retrieval metrics
say immediately whether retrieval or generation broke.

### Run storage

Every run persists its git SHA, prompt version, model identifiers, and retrieval
config. The dashboard diffs any two runs and drills into individual failures with the
retrieved chunks visible. Prompts are versioned files, not inline strings, so a
regression can be traced to a specific prompt change.

## Data model

- `documents` — source path, pinned SHA, title
- `chunks` — text, heading path, embedding vector, FTS index, FK to document
- `traces` — request, latency per stage, retrieved chunk ids with scores, tokens, cost
- `escalations` — question, reason, confidence signals, timestamp
- `eval_items` — the golden dataset, mirrored from YAML
- `eval_runs` — SHA, prompt version, model, config, aggregate metrics
- `eval_results` — per-item outcome, scores, judge rationale, FK to run

## Web surfaces

1. **Ask** — streaming answer, inline citations linking to the doc section, confidence
   badge, escalation card on refusal.
2. **Evals** — run history, per-run metrics, side-by-side run diff, failure drill-down.
3. **Traces** — per-request timeline with retrieval latency, chunk scores, token counts
   and cost.

## Testing

- **pytest** for the API. Chunking, RRF fusion, and the confidence gate are pure
  functions tested against a fake LLM client: fast, deterministic, no tokens spent.
  Integration tests run against a Neon branch created per run.
- **Vitest and Testing Library** for the eval dashboard, rendering against fixture run
  data. The dashboard's diffing and regression highlighting are the logic worth
  testing; the Ask and Traces surfaces are covered by the end-to-end demo path.
- **GitHub Actions** — lint and tests on every push; a 10-item eval smoke set on every
  pull request that fails the build on regression; the full 80-item run nightly.

Evals gating CI is the single strongest signal this project carries. It is not
optional scope.

## Deployment

Vercel for the web app, Render for the FastAPI container, Neon for Postgres. Docker
Compose for local development, with parity to the deployed services. No Kubernetes:
it would add weeks of configuration that no reviewer inspects, and reads as
over-engineering on a project this size.

## Code standards

These are enforceable review criteria, not preferences. The repository is read by
humans evaluating the author.

- Comments explain why, never what.
- No emoji in code, commits, or documentation.
- No exception handling that swallows errors. Fail loudly where failing is correct.
- No abstraction without a second caller.
- No placeholder functions, no deferred TODOs, no unused configuration flags.
- Docstrings on public module boundaries only.
- Tests assert behavior, never tautologies.
- Commits are incremental and describe intent. No generated co-author trailers.
- README is factual: architecture, ablation table, tradeoff curve, run instructions.

Target size for Phase 1 is 2,500-3,500 lines. Exceeding it is treated as a signal of
unearned surface area and triggers a cut, not an exception.

## Success criteria

- Ablation table shows measured improvement from hybrid retrieval and from reranking.
- Deflection-rate vs. incorrect-answer-rate curve is produced, and an operating point
  is chosen from it with stated reasoning.
- CI fails on an intentionally introduced prompt regression.
- The demo path runs end to end on the deployed URL.

## Open decisions deferred to implementation

- Exact chunk size and overlap: chosen by sweep, not up front.
- Confidence threshold value: chosen from the curve.
- Reranker model selection: two candidates compared in the ablation.

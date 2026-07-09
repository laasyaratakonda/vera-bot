# Vera Challenge Bot — submission

## Approach

`composer.py` is a **deterministic, rule-based composer** — no LLM in the hot
path. It's a dispatch table keyed on `TriggerContext.kind` (25+ kinds
covered, including all 30 that appear in `test_pairs.json`). Each handler
pulls facts *only* from the four contexts it's given (category digest /
peer_stats / offer_catalog, merchant performance / signals / offers, trigger
payload, customer relationship), so the bot never invents a stat, citation,
offer, or competitor name (brief §5.8 / §11). Category taboo vocabulary is
stripped automatically; Hindi-English code-mix kicks in when the merchant's
or customer's language preference includes `hi`. Every message ends in
exactly one CTA.

`bot.py` is a thin FastAPI wrapper exposing the 5 required endpoints
(`/v1/context`, `/v1/tick`, `/v1/reply`, `/v1/healthz`, `/v1/metadata`) plus
optional `/v1/teardown`. State (contexts, conversations, sent bodies) lives
in memory, per the brief.

`/v1/reply` handles the three "Open Challenges" the brief calls out:
- **Auto-reply detection**: same/near-canned message → one gentle nudge, then
  graceful `end` on the second occurrence (matches brief Pattern B).
- **Intent handoff**: an affirmative message ("haan", "yes", "let's do it")
  routes straight to `send`/action, never back to a qualifying question
  (avoids Pattern D).
- **Anti-repetition**: every conversation tracks bodies already sent; a
  would-be duplicate gets a distinguishing suffix instead of resending
  verbatim (testing-brief §10 penalty).

## Why deterministic-first

The challenge brief itself says (Step 2 of your instructions): *"Start
small and deterministic: handle trigger, merchant, and category context
correctly."* A rule-based composer is:
- 100% reproducible (`temperature=0` requirement, trivially satisfied)
- fast (well under the 30s budget, no API latency/cost/rate-limit risk)
- easy for you to extend — swap any handler's body for an LLM call later
  without touching the endpoint layer, if you want richer prose for
  extra-credit categories.

## Tradeoffs

- Prose is templated, not LLM-generated — less varied phrasing than a model
  would produce, but zero hallucination risk and 100% determinism.
- A handful of trigger kinds arrive from the generator with placeholder
  payloads (`{"placeholder": true, ...}`); these fall back to a generic
  handler that still grounds itself in a real merchant `signal` rather than
  inventing detail.
- Multi-turn state resets if the process restarts (in-memory only) — fine
  for the 60-minute test window per the brief, not production-durable.

## What additional context would have helped most

- A merchant-level "preferred CTA style" flag (binary vs open-ended) would
  remove some guesswork per trigger kind.
- Real (non-placeholder) payloads for the long tail of generated triggers
  would let every handler be as specific as the seed-trigger ones.

## Files

| File | Purpose |
|---|---|
| `composer.py` | Deterministic `compose()` — the core logic |
| `bot.py` | FastAPI server exposing the 5 endpoints |
| `generate_submission.py` | Builds `submission.jsonl` from `test_pairs.json` |
| `submission.jsonl` | 30 composed messages, one per canonical test pair |
| `requirements.txt` / `Dockerfile` | For deploying anywhere |

## Run locally

```bash
pip install -r requirements.txt
uvicorn bot:app --host 0.0.0.0 --port 8080
```

Self-test against the official harness (needs your own LLM API key — edit
the CONFIGURATION block at the top of `judge_simulator.py`):

```bash
export BOT_URL=http://localhost:8080
python judge_simulator.py
```

## Deploying to get a public URL (needed for submission)

Pick any of these — all give you a `https://...` base URL:

**Render (free tier, easiest)**
1. Push this folder to a GitHub repo.
2. render.com → New → Web Service → connect the repo.
3. Environment: Docker (it will detect the `Dockerfile`) — or set
   Build Command `pip install -r requirements.txt` and Start Command
   `uvicorn bot:app --host 0.0.0.0 --port $PORT`.
4. Deploy. Your URL: `https://<service-name>.onrender.com`.

**Railway**
1. railway.app → New Project → Deploy from GitHub repo.
2. It auto-detects the `Dockerfile`. Deploy.
3. Settings → Networking → Generate Domain.

**Fly.io**
```bash
fly launch   # detects Dockerfile, follow prompts
fly deploy
```

**ngrok (fastest for local testing, not for the real submission window)**
```bash
uvicorn bot:app --host 0.0.0.0 --port 8080 &
ngrok http 8080
```

Once deployed, verify before submitting:
```bash
curl https://<your-host>/v1/healthz
curl https://<your-host>/v1/metadata
```

Then submit `https://<your-host>` (no trailing `/v1/...`) as your base URL
via the challenge's submission portal.

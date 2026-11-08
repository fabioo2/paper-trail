# 5. Database-backed job queue with leases and a GPU availability probe

Date: 2026-11-06

## Status

Accepted

## Context

The LLM host (RTX 3080 PC) is not always on and has no Wake-on-LAN. Uploads must be accepted at any time and processed when the GPU is reachable. We already have one SQLite database and want no extra infrastructure (Redis, RabbitMQ, Celery).

## Decision

- A `jobs` table implements the `JobQueue` port. Claiming is a single atomic `UPDATE ... WHERE id = (SELECT ... ORDER BY available_at, id LIMIT 1) RETURNING *` that sets `status='running'`, `locked_by` and `lease_expires_at`. Expired leases are reclaimable, so a crashed worker never strands a job.
- `fail()` increments `attempts` and either reschedules with exponential backoff or marks the job `failed` after `max_attempts`. `release()` returns a job without consuming an attempt (used when the GPU is unavailable).
- The worker probes Ollama (`GET /api/tags`, short timeout, required models present) before claiming LLM work. While unavailable it backs off exponentially with jitter (15 s up to 5 min), records the probe result for the UI, and leaves CPU-only steps (OCR) running.

## Consequences

- No new service; queue state is backed up with the rest of the data.
- Polling latency (about 1 s) is irrelevant for this workload.
- One worker process is the expected deployment; the lease design still allows more.

## Amendment (2026-11-06): overnight GPU window

The GPU PC is also the owner's gaming PC; a model call during a game slowed both (a 1.5 s vision call took 211 s). Queued LLM work therefore runs only inside `WORKER__GPU_WINDOW` (default `02:00-06:00`, local time). Outside it, GPU steps are released without consuming an attempt and the receipt shows `SCHEDULED`; CPU steps such as OCR still run at upload. The probe is not run outside the window. Manual `paper-trail process` runs ignore the window because the owner starts them deliberately. The PC must be awake in the window (Windows sleep disabled or a wake timer, Docker Desktop set to start at sign-in); otherwise jobs wait for the next window, which is the existing `WAITING_FOR_GPU` behaviour.

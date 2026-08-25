"""Batch runner for Terminal-Bench 2.1 via the harbor pool eval line.

Usage (workdir = examples/bot_project, bot venv):
    python -m bot.eval.harbor.tb21_batch --run-id tb21-full-1 \
        --dataset ../../.data/terminal-bench-2-1

Persistence-first design (per user requirement "content must survive"):
- One harbor trial per task, sequentially, through the SAME machinery as the
  smoke gate (run_trial + ModexHarborAgent pool mode + verdict score collect).
- Per-task checkpoint (append-only JSONL) => interrupted runs resume.
- Per-task evidence JSON + rolling aggregate report regenerated after EVERY
  task (evals/evidence/tb21/<run-id>/).
- Live observability: a task-start JSON (status=running) at trial start, a
  periodic refresher (elapsed_s + report.json + dashboard.html every 30s), and
  status=interrupted markers on cancellation - the evidence dir is observable
  during multi-hour runs, not only after completion.
- Harbor job dirs (trials, trajectories, verifier outputs) are never deleted -
  they ARE the local artifacts, under .data/tb21-runs/<run-id>/jobs.
- Langfuse retention is the trial pipeline itself (spans + 13 metrics + verdict
  scores are exported live during each trial).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import signal
import time
from enum import StrEnum
from pathlib import Path
from typing import Any, Final

import typer

from bot.eval.evalenv import LangfuseCredentials
from bot.eval.harbor.host_cli import SubprocessExecutionPlane
from bot.eval.harbor.host_runtime import (
    RunTrialRequest,
    mint_stable_experiment_id,
    run_trial,
)
from bot.eval.harbor.smoke_runtime import _OVERLAY_PATH
from modex_agent.trace.semconv import GenAiAttr, SpanName


class TbTaskStatus(StrEnum):
    """Checkpoint row statuses. The JSONL checkpoint is the resume contract:
    a typo in any value silently breaks task-skip on resume and report
    partitioning, so every writer/reader goes through this enum."""

    RUNNING = "running"
    COMPLETED = "completed"
    VERIFIER_ERROR = "verifier_error"
    AGENT_ERROR = "agent_error"
    INFRA_ERROR = "infra_error"
    INTERRUPTED = "interrupted"


_DONE_STATUSES = frozenset(
    {
        TbTaskStatus.COMPLETED,
        TbTaskStatus.VERIFIER_ERROR,
        TbTaskStatus.AGENT_ERROR,
        TbTaskStatus.INFRA_ERROR,
    }
)

# Injected stop reason for trials whose agent result.json never landed — the
# reliable signature of a wall-clock timeout kill.
_TIMEOUT_KILL_REASON: Final = "timeout_kill"


def _evidence_paths(run_id: str, repo_root: Path) -> tuple[Path, Path, Path]:
    base = repo_root / ".data" / "tb21-runs" / run_id
    jobs = base / "jobs"
    evidence = repo_root / "examples" / "bot_project" / "evals" / "evidence" / "tb21" / run_id
    for p in (jobs, evidence):
        p.mkdir(parents=True, exist_ok=True)
    return base, jobs, evidence


def _load_done(checkpoint: Path) -> set[str]:
    done: set[str] = set()
    if checkpoint.exists():
        for line in checkpoint.read_text(encoding="utf-8").splitlines():
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("status") in _DONE_STATUSES:
                done.add(str(rec["task"]))
    return done


def _reward_from_job(job_dir: Path) -> tuple[float | None, str | None]:
    """Extract the official reward + failure tail from a harbor job dir."""
    for trial in sorted(job_dir.glob("*/")):
        reward = trial / "verifier" / "reward.txt"
        if reward.exists():
            try:
                return float(reward.read_text(encoding="utf-8").strip()), None
            except ValueError:
                pass
        exc = trial / "exception.txt"
        if exc.exists():
            text = exc.read_text(encoding="utf-8", errors="replace")
            tail = text.strip().splitlines()[-1] if text.strip() else ""
            return None, tail[:300]
    return None, None


def _append(checkpoint: Path, record: dict[str, Any]) -> None:
    with checkpoint.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")


def _atomic_write(path: Path, text: str) -> None:
    """Temp file + os.replace so a reader never sees a half-written file."""
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _read_task_json(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _sum_span_usage(trial: Path) -> tuple[int | None, int | None]:
    """Sum gen_ai usage over every chat span in a trial's spans.jsonl files.

    Spans files reach tens of MB, so lines stream one at a time — never a
    whole-file load. Returns (None, None) when no chat span was seen at all.
    """
    input_total = 0
    output_total = 0
    seen_chat = False
    for spans_path in sorted(trial.glob("agent/**/spans.jsonl")):
        try:
            with spans_path.open(encoding="utf-8") as spans_file:
                for line in spans_file:
                    try:
                        span = json.loads(line)
                        if not isinstance(span, dict) or span.get("name") != SpanName.CHAT:
                            continue
                        attrs = span.get("attributes")
                        if not isinstance(attrs, dict):
                            continue
                        input_total += int(attrs.get(GenAiAttr.USAGE_INPUT_TOKENS, 0))
                        output_total += int(attrs.get(GenAiAttr.USAGE_OUTPUT_TOKENS, 0))
                        seen_chat = True
                    except (json.JSONDecodeError, ValueError, TypeError):
                        continue
        except OSError:
            continue
    return (input_total, output_total) if seen_chat else (None, None)


def _job_metrics(job_dir: Path) -> dict[str, Any]:
    """Best-effort cost/stop evidence for one harbor job dir; never raises.

    Primary source is the first trial's agent ``result.json`` (stop_reason,
    spent_usd) and ``usage.json`` (token counts). Timeout-killed trials write
    neither: tokens then fall back to the streaming spans sum, and the stop
    reason is recorded as ``timeout_kill``.
    """
    metrics: dict[str, Any] = {
        "stop_reason": None,
        "spent_usd": None,
        "input_tokens": None,
        "output_tokens": None,
    }
    try:
        trials = sorted(job_dir.glob("*/"))
        if not trials:
            return metrics
        trial = trials[0]
        result_paths = sorted(trial.glob("agent/**/result.json"))
        usage_paths = sorted(trial.glob("agent/**/usage.json"))
        result = _read_task_json(result_paths[0]) if result_paths else None
        usage = _read_task_json(usage_paths[0]) if usage_paths else None
        if result is not None:
            metrics["stop_reason"] = result.get("stop_reason")
            metrics["spent_usd"] = result.get("spent_usd")
        else:
            metrics["stop_reason"] = _TIMEOUT_KILL_REASON
        if usage is not None:
            metrics["input_tokens"] = usage.get("input_tokens")
            metrics["output_tokens"] = usage.get("output_tokens")
        else:
            metrics["input_tokens"], metrics["output_tokens"] = _sum_span_usage(trial)
    except Exception:  # noqa: BLE001 - evidence collection is best-effort per task
        pass
    return metrics


def _refresh_running_elapsed(
    running: set[str],
    started_at: dict[str, float],
    evidence_dir: Path,
) -> None:
    """Update elapsed_s on every in-flight task's evidence JSON (live heartbeat)."""
    now = time.time()
    for name in sorted(running):
        path = evidence_dir / f"{name}.json"
        data = _read_task_json(path)
        if data is None or data.get("status") != TbTaskStatus.RUNNING:
            continue
        started = started_at.get(name)
        if started is None:
            continue
        data["elapsed_s"] = round(now - started, 1)
        _atomic_write(path, json.dumps(data, ensure_ascii=False, indent=2))


def _mark_interrupted(
    running: set[str],
    started_at: dict[str, float],
    evidence_dir: Path,
) -> None:
    """Stamp status=interrupted onto in-flight task JSONs so evidence never lies."""
    now = time.time()
    for name in sorted(running):
        path = evidence_dir / f"{name}.json"
        data = _read_task_json(path)
        if data is None:
            continue
        data["status"] = TbTaskStatus.INTERRUPTED
        started = started_at.get(name)
        if started is not None:
            data["elapsed_s"] = round(now - started, 1)
        _atomic_write(path, json.dumps(data, ensure_ascii=False, indent=2))
    running.clear()


def _write_dashboard(
    checkpoint: Path,
    evidence_dir: Path,
    total: int,
    running: set[str],
    started_at: dict[str, float],
    concurrency: int,
) -> None:
    """Self-refreshing HTML progress board: one row per task, live status."""
    rows: list[dict[str, Any]] = []
    if checkpoint.exists():
        for line in checkpoint.read_text(encoding="utf-8").splitlines():
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    solved = [r for r in rows if r.get("reward") == 1.0]
    failed = [r for r in rows if r.get("reward") == 0.0]
    errored = [r for r in rows if r.get("reward") is None]
    done_tasks = {r["task"] for r in rows}
    now = time.time()

    def _fmt(r: dict[str, Any]) -> str:
        reward = r.get("reward")
        if reward == 1.0:
            return "✅ PASS"
        if reward == 0.0:
            return "❌ FAIL"
        return "⚠️ " + str(r.get("status", "error"))

    body_rows = []
    for r in sorted(rows, key=lambda x: x.get("started_at", "")):
        err = r.get("error") or ""
        err_short = err.replace("<", "&lt;")[:140]
        body_rows.append(
            f"<tr><td>{r['task']}</td><td>{_fmt(r)}</td>"
            f"<td>{r.get('elapsed_s', '')}s</td><td>{err_short}</td></tr>"
        )
    for name in sorted(running - done_tasks):
        elapsed = int(now - started_at.get(name, now))
        body_rows.append(
            f"<tr class='running'><td>{name}</td><td>🔄 running</td>"
            f"<td>{elapsed}s</td><td></td></tr>"
        )

    acc = f"{100 * len(solved) / len(rows):.1f}%" if rows else "—"
    html = f"""<!DOCTYPE html><html><head><meta charset="utf-8">
<meta http-equiv="refresh" content="10">
<title>TB 2.1 — {checkpoint.parent.name}</title>
<style>
body{{font-family:Consolas,monospace;background:#111;color:#ddd;margin:24px}}
h1{{font-size:18px}} .stats{{font-size:16px;margin:12px 0}}
.stat{{display:inline-block;margin-right:24px;padding:6px 12px;border-radius:6px}}
.pass{{background:#143d1f;color:#7ee787}} .fail{{background:#4a1420;color:#ff7b72}}
.err{{background:#4a3a10;color:#e3b341}} .acc{{background:#1f2937;color:#79c0ff}}
table{{border-collapse:collapse;width:100%;margin-top:16px;font-size:13px}}
td,th{{border:1px solid #333;padding:4px 8px;text-align:left}}
tr.running td{{color:#79c0ff}} th{{background:#1f2937}}
</style></head><body>
<h1>Terminal-Bench 2.1 — run {checkpoint.parent.name} (concurrency {concurrency})</h1>
<div class="stats">
<span class="stat acc">accuracy {acc}</span>
<span class="stat pass">✅ solved {len(solved)}</span>
<span class="stat fail">❌ failed {len(failed)}</span>
<span class="stat err">⚠️ errored {len(errored)}</span>
<span class="stat">progress {len(rows)}/{total}</span>
<span class="stat">🔄 live {len(running - done_tasks)}</span>
</div>
<table><tr><th>task</th><th>outcome</th><th>elapsed</th><th>error / note</th></tr>
{chr(10).join(body_rows)}
</table></body></html>"""
    (evidence_dir / "dashboard.html").write_text(html, encoding="utf-8")


def _write_report(checkpoint: Path, evidence_dir: Path, total: int) -> Path:
    rows: list[dict[str, Any]] = []
    if checkpoint.exists():
        for line in checkpoint.read_text(encoding="utf-8").splitlines():
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    solved = [r for r in rows if r.get("reward") == 1.0]
    failed = [r for r in rows if r.get("reward") == 0.0]
    errored = [r for r in rows if r.get("reward") is None]
    spent_values = [
        float(r["spent_usd"]) for r in rows if isinstance(r.get("spent_usd"), (int, float))
    ]
    input_values = [r["input_tokens"] for r in rows if isinstance(r.get("input_tokens"), int)]
    output_values = [r["output_tokens"] for r in rows if isinstance(r.get("output_tokens"), int)]
    stop_reason_histogram: dict[str, int] = {}
    for reason in (r.get("stop_reason") for r in rows):
        if isinstance(reason, str):
            stop_reason_histogram[reason] = stop_reason_histogram.get(reason, 0) + 1
    report = {
        "total_tasks": total,
        "attempted": len(rows),
        "solved": len(solved),
        "failed": len(failed),
        "errored": len(errored),
        "accuracy": round(len(solved) / len(rows), 4) if rows else None,
        "solved_tasks": [r["task"] for r in solved],
        "failed_tasks": [r["task"] for r in failed],
        "error_tasks": [r["task"] for r in errored],
        "total_spent_usd": round(sum(spent_values), 6) if spent_values else None,
        "total_input_tokens": sum(input_values) if input_values else None,
        "total_output_tokens": sum(output_values) if output_values else None,
        "stop_reason_histogram": stop_reason_histogram,
        "rows": rows,
    }
    path = evidence_dir / "report.json"
    _atomic_write(path, json.dumps(report, ensure_ascii=False, indent=2))
    return path


async def _create_tb_dataset(dataset_dir: Path, experiment_name: str) -> tuple[str, dict[str, str]]:
    """Create one Langfuse dataset item per task dir; return (dataset_id, task->item_id)."""
    from langfuse import Langfuse

    credentials = LangfuseCredentials.from_env()
    if credentials is None:
        raise KeyError("Langfuse credentials are required")
    host = credentials.host if credentials.host is not None else "http://localhost:3000"
    client = Langfuse(
        base_url=host,
        public_key=credentials.public_key,
        secret_key=credentials.secret_key,
        timeout=10,
        tracing_enabled=False,
    )
    try:
        dataset = client.create_dataset(
            name=experiment_name,
            description=f"Terminal-Bench 2.1 full run ({experiment_name}).",
        )
        item_ids: dict[str, str] = {}
        for task in sorted(p for p in dataset_dir.iterdir() if p.is_dir()):
            item_ids[task.name] = client.create_dataset_item(
                dataset_name=experiment_name,
                input={"task": task.name, "path": task.as_posix()},
            ).id
        return dataset.id, item_ids
    finally:
        client.shutdown()


async def _run_batch(
    run_id: str,
    dataset_dir: Path,
    repo_root: Path,
    timeout_multiplier: float,
    concurrency: int = 1,
    refresh_interval_s: float = 30.0,
) -> None:
    from bot.eval.harbor.host_cli import collect_job
    from bot.eval.harbor.model_source import (
        inject_model_env,
        resolve_model_settings,
    )
    from bot.eval.harbor.verdict_collector import (
        read_official_results,
        read_trial_trace_map,
    )

    base, jobs_dir, evidence_dir = _evidence_paths(run_id, repo_root)
    checkpoint = base / "checkpoint.jsonl"
    done = _load_done(checkpoint)
    settings = resolve_model_settings()
    inject_model_env(settings)  # LLM_API_KEY/BASE_URL/... must reach the container env
    model = settings.model

    experiment_name = f"terminalbench.tb21.{run_id}"
    os.environ["MODEX_AGENT_MODE"] = "pool"

    dataset_id, item_ids = await _create_tb_dataset(dataset_dir, experiment_name)
    typer.echo(
        f"[tb21] run={run_id} model={model} dataset_id={dataset_id} "
        f"items={len(item_ids)} done={len(done)} jobs={jobs_dir} concurrency={concurrency}"
    )

    running_tasks: set[str] = set()
    task_started_at: dict[str, float] = {}

    def _refresh_dashboard() -> None:
        _write_dashboard(
            checkpoint,
            evidence_dir,
            total=len(item_ids),
            running=running_tasks,
            started_at=task_started_at,
            concurrency=concurrency,
        )

    async def _run_one(name: str, plane: SubprocessExecutionPlane) -> None:
        item_id = item_ids[name]
        job_name = name
        record: dict[str, Any] = {
            "task": name,
            "status": TbTaskStatus.INFRA_ERROR,
            "reward": None,
            "verdict_injected": False,
            "trace_id": None,
            "elapsed_s": None,
            "job_dir": str(jobs_dir / job_name),
            "error": None,
            "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "stop_reason": None,
            "spent_usd": None,
            "input_tokens": None,
            "output_tokens": None,
        }
        started = time.time()
        task_started_at[name] = started
        running_tasks.add(name)
        _atomic_write(
            evidence_dir / f"{name}.json",
            json.dumps(
                {
                    "task": name,
                    "status": TbTaskStatus.RUNNING,
                    "started_at": record["started_at"],
                    "elapsed_s": None,
                },
                ensure_ascii=False,
                indent=2,
            ),
        )
        _refresh_dashboard()
        typer.echo(f"[run ] {name} ...")
        try:
            trial = await run_trial(
                RunTrialRequest(
                    task_path=dataset_dir / name,
                    jobs_dir=jobs_dir,
                    job_name=job_name,
                    experiment_name=experiment_name,
                    dataset_id=dataset_id,
                    item_id=item_id,
                    model=model,
                    memory_namespace=experiment_name,
                    timeout_multiplier=timeout_multiplier,
                    compose_overlay=_OVERLAY_PATH,
                ),
                plane,
                mint_stable_experiment_id,
            )
            record["elapsed_s"] = round(time.time() - started, 1)
            record["exit_code"] = trial.result.exit_code
            reward, failure = _reward_from_job(jobs_dir / job_name)
            record["reward"] = reward
            record["error"] = failure
            record.update(_job_metrics(jobs_dir / job_name))
            record["status"] = (
            TbTaskStatus.COMPLETED if reward is not None else TbTaskStatus.VERIFIER_ERROR
        )
            try:
                job_dir = jobs_dir / job_name
                await collect_job(job_dir, "terminalbench.tb21.v1", job_dir.as_posix())
                mapping = read_trial_trace_map(job_dir)
                if mapping.entries:
                    record["trace_id"] = mapping.entries[0].trace_id
                results = read_official_results(job_dir)
                record["official_results"] = [r.value for r in results]
                record["verdict_injected"] = True
            except Exception as exc:  # noqa: BLE001 - verdict inject is best-effort per task
                record["verdict_error"] = f"{type(exc).__name__}: {exc}"[:200]
        except Exception as exc:  # noqa: BLE001 - batch must survive per-task failures
            record["elapsed_s"] = round(time.time() - started, 1)
            record["error"] = f"{type(exc).__name__}: {exc}"[:300]
            record["status"] = TbTaskStatus.INFRA_ERROR
        _append(checkpoint, record)
        running_tasks.discard(name)
        _atomic_write(
            evidence_dir / f"{name}.json",
            json.dumps(record, ensure_ascii=False, indent=2),
        )
        _write_report(checkpoint, evidence_dir, total=len(item_ids))
        _refresh_dashboard()
        outcome = record["reward"] if record["reward"] is not None else record["status"]
        typer.echo(f"[done] {name} -> {outcome} ({record['elapsed_s']}s)")

    pending = [name for name in sorted(item_ids) if name not in done]
    for name in sorted(item_ids):
        if name in done:
            typer.echo(f"[skip] {name}")

    async def _evidence_refresher() -> None:
        while True:
            _refresh_running_elapsed(running_tasks, task_started_at, evidence_dir)
            _write_report(checkpoint, evidence_dir, total=len(item_ids))
            _refresh_dashboard()
            await asyncio.sleep(refresh_interval_s)

    refresher = asyncio.create_task(_evidence_refresher())
    try:
        if concurrency <= 1 or len(pending) <= 1:
            plane = SubprocessExecutionPlane()
            for name in pending:
                await _run_one(name, plane)
        else:
            queue: asyncio.Queue[str] = asyncio.Queue()
            for name in pending:
                queue.put_nowait(name)

            async def _worker() -> None:
                plane = SubprocessExecutionPlane()
                while True:
                    try:
                        name = queue.get_nowait()
                    except asyncio.QueueEmpty:
                        return
                    try:
                        await _run_one(name, plane)
                    finally:
                        queue.task_done()

            workers = [asyncio.create_task(_worker()) for _ in range(concurrency)]
            await asyncio.gather(*workers)
    finally:
        refresher.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await refresher
        _mark_interrupted(running_tasks, task_started_at, evidence_dir)
        _write_report(checkpoint, evidence_dir, total=len(item_ids))
        _refresh_dashboard()

    typer.echo(f"[tb21] finished. report: {evidence_dir / 'report.json'}")


def _translate_sigterm_to_interrupt() -> None:
    """SIGTERM -> KeyboardInterrupt so the finally-block evidence flush still runs."""

    def _raise_interrupt(signum: int, frame: object) -> None:
        raise KeyboardInterrupt

    with contextlib.suppress(ValueError, OSError, NotImplementedError):
        signal.signal(signal.SIGTERM, _raise_interrupt)


_REPO_ROOT_DEFAULT = Path(__file__).resolve().parents[5]
_RUN_ID_OPTION = typer.Option(..., "--run-id")
_DATASET_OPTION = typer.Option(..., "--dataset")
_TIMEOUT_MULTIPLIER_OPTION = typer.Option(6.0, "--timeout-multiplier")
_CONCURRENCY_OPTION = typer.Option(1, "--concurrency", min=1, max=10)
_REPO_ROOT_OPTION = typer.Option(_REPO_ROOT_DEFAULT, "--repo-root")


def main(
    run_id: str = _RUN_ID_OPTION,
    dataset: Path = _DATASET_OPTION,
    timeout_multiplier: float = _TIMEOUT_MULTIPLIER_OPTION,
    concurrency: int = _CONCURRENCY_OPTION,
    repo_root: Path = _REPO_ROOT_OPTION,
) -> None:
    _translate_sigterm_to_interrupt()
    asyncio.run(_run_batch(run_id, dataset, repo_root, timeout_multiplier, concurrency))


if __name__ == "__main__":
    typer.run(main)

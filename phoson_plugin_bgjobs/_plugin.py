"""Background-jobs plugin: one-shot commands that re-activate the agent.

Exposes ``run_bg_job``/``list_bg_jobs``/``stop_bg_job``/``wait_bg_jobs`` tools
so the agent can launch long commands (builds, tests, training, deploys)
*without blocking the run* and be woken up when they finish, carrying the
exit code, an output tail and the duration.

Design (issue #217), mirroring ``phoson_plugin_monitor``:

- **Disk is the source of truth.** The registry (``jobs.json``) and the wake
  queue (``wakes.jsonl``) live under ``data_dir`` and survive process
  restarts. In-memory asyncio tasks are a cache: they die when the host
  rebuilds its engine or exits, and ``ensure_started()`` reconciles every job
  still marked ``running`` from disk.
- **Wakes go to a persistent queue first.** A host with a live event loop
  may additionally pass an ``on_wake`` callable (called fire-and-forget on
  every completion); the CLI drains the queue into the next user turn.
- **No new engine contract.** The plugin only uses the canonical ``Plugin``
  lifecycle plus ``@tool``; hosts integrate via duck-typed methods
  (``ensure_started``/``drain_pending_wakes``/``pending_wakes``).

Security note: launching a job is a tool call the model makes, so it passes
the host's permission gate with the exact command visible. Because the job is
detached (``start_new_session``) it keeps running after the run ends; treat
``run_bg_job`` as approved shell access until ``stop_bg_job``.
"""

import os
import re
import time
import signal
import asyncio
import logging
import contextlib
from typing import Any, cast
from dataclasses import replace
from collections.abc import Callable, Awaitable

from phoson_agent import (
    Plugin,
    AgentTool,
    KeyValueBlock,
    CliCommandSpec,
    CliCommandContext,
    CliCommandInvocation,
    tool,
)

from .storage import JobDef, JobStore, WakeEvent, WakeQueue

logger = logging.getLogger(__name__)

_DEFAULT_DATA_DIR = "~/.phoson/bgjobs"
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_DEFAULT_MAX_OUTPUT_CHARS = 4000
_DEFAULT_WATCHDOG_SECONDS = 0.0


def _read_output_tail(log_path: str, max_chars: int) -> str:
    """Return the last ``max_chars`` of ``log_path`` (best effort)."""
    if not log_path:
        return ""
    try:
        size = os.path.getsize(log_path)
    except OSError:
        return ""
    try:
        with open(log_path, "rb") as fh:
            if size > max_chars:
                fh.seek(size - max_chars)
            data = fh.read(max_chars)
    except OSError:
        return ""
    return data.decode(errors="replace")


def _process_group_alive(pgid: int | None) -> bool:
    """True when the process group ``pgid`` still has at least one member."""
    if not pgid:
        return False
    try:
        os.killpg(pgid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        # The group exists but belongs to another user: it is alive.
        return True
    except OSError:
        return False


def render_wake_message(events: list[WakeEvent]) -> str:
    """Render consumed wake events as a user-message header (pure).

    The host prepends this to the user's input so the model sees the
    completed-job result in context.
    """
    if not events:
        return ""
    lines = [
        "[BACKGROUND JOB EVENTS] A background job finished while you were "
        "idle. Review the result below and act on it."
    ]
    for event in events:
        lines.append("")
        state = event.payload.get("state", "completed")
        returncode = event.payload.get("returncode")
        lines.append(f"[{event.name}] state={state} fired_at={event.fired_at}")
        lines.append(f"  job_id: {event.job_id}")
        if returncode is not None:
            lines.append(f"  returncode: {returncode}")
        if event.payload.get("duration_ms") is not None:
            lines.append(f"  duration_ms: {event.payload['duration_ms']}")
        if event.payload.get("timed_out"):
            lines.append("  timed_out: true")
        command = event.payload.get("command")
        if command:
            lines.append(f"  command: {command}")
        output_tail = event.payload.get("output_tail") or ""
        if output_tail:
            rendered = output_tail.split("\n")
            lines.append(f"  output_tail: {rendered[0]}")
            for cont in rendered[1:]:
                lines.append(f"    {cont}" if cont else "")
    return "\n".join(lines)


class BgJobsPlugin(Plugin):
    """One-shot background jobs that persist and re-activate the agent.

    Configuration (via ``configure``):
        data_dir: State directory (default ``~/.phoson/bgjobs``).
        on_wake: Optional callable (async or sync) invoked as
            ``on_wake(WakeEvent)`` whenever a job finishes, in addition to
            the persistent queue.
        max_pending_wakes: Pending events kept per job before the oldest is
            dropped (anti-storm, default 5).
        default_session_id: Session id stamped on wakes when the host did
            not inject a ``session_id_provider`` into the agent context.
        max_output_chars: Default cap on the captured output tail (4000).
        watchdog_seconds: Optional per-job kill timeout (0 disables it).
    """

    def __init__(self) -> None:
        self._data_dir: str = _DEFAULT_DATA_DIR
        self._on_wake: Callable[[WakeEvent], Awaitable[None] | None] | None = None
        self._max_pending_wakes = 5
        self._default_session_id = ""
        self._default_max_output_chars = _DEFAULT_MAX_OUTPUT_CHARS
        self._watchdog_seconds = _DEFAULT_WATCHDOG_SECONDS

        self._store: JobStore | None = None
        self._queue: WakeQueue | None = None
        self._tasks: dict[str, asyncio.Task] = {}
        self._processes: dict[str, asyncio.subprocess.Process] = {}
        self._started_at: dict[str, float] = {}
        self._stopping: set[str] = set()
        self._wake_lock = asyncio.Lock()

    # ── Plugin contract ───────────────────────────────────────────────────

    @property
    def name(self) -> str:
        return "phoson-plugin-bgjobs"

    @property
    def version(self) -> str:
        return "0.1.0"

    @property
    def description(self) -> str:
        return "Run shell commands in the background and wake the agent on completion"

    def configure(self, config: dict[str, Any]) -> None:
        """Merge config; keys absent from ``config`` keep their current value."""
        if "data_dir" in config:
            self._data_dir = str(config["data_dir"])
        if "on_wake" in config:
            on_wake = config["on_wake"]
            if on_wake is not None and not callable(on_wake):
                raise ValueError("on_wake must be a callable (or null to disable)")
            self._on_wake = cast(
                Callable[[WakeEvent], Awaitable[None] | None] | None, on_wake
            )
        if "max_pending_wakes" in config:
            self._max_pending_wakes = int(config["max_pending_wakes"])
        if "default_session_id" in config:
            self._default_session_id = str(config["default_session_id"] or "")
        if "max_output_chars" in config:
            self._default_max_output_chars = max(64, int(config["max_output_chars"]))
        if "watchdog_seconds" in config:
            self._watchdog_seconds = max(0.0, float(config["watchdog_seconds"]))

    def initialize(self) -> None:
        """Load (or create) the on-disk registry and wake queue. Idempotent."""
        if self._store is not None and self._queue is not None:
            return
        self._store = JobStore(self._data_dir)
        self._queue = WakeQueue(self._data_dir, self._max_pending_wakes)
        logger.debug(
            "BgJobs plugin initialized: %d job(s), %d pending wake(s) in %s",
            len(self._store.list()),
            self._queue.pending_count(),
            self._data_dir,
        )

    async def aclose(self) -> None:
        """Cancel reaper/poller tasks without persisting a state change.

        Running jobs are **not** killed: they are detached by design and
        survive the host. On the next start, ``ensure_started()`` reconciles
        them (alive -> ``orphaned`` poller, dead -> ``orphaned``).
        """
        tasks = [t for t in self._tasks.values() if not t.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()
        self._processes.clear()

    def cleanup(self) -> None:
        """Sync fallback: best-effort cancel when no loop can await us."""
        for task in self._tasks.values():
            if not task.done():
                task.cancel()
        self._tasks.clear()
        self._processes.clear()

    # ── Host-facing duck-typed hooks (documented, not engine contract) ────

    def render_wake_message(self, events: list[WakeEvent]) -> str:
        """Duck-typed renderer so the CLI styles this provider's wakes.

        Delegates to the module-level function (the CLI prefers a plugin's
        own renderer over the monitor fallback when composing providers).
        """
        return render_wake_message(events)

    async def ensure_started(self) -> None:
        """Reconcile jobs left ``running`` on disk with this process.

        Idempotent; safe to call on engine rebuild or from a tool handler.
        A job that belongs to this process (has a live reaper) is left alone.
        A job from a previous process cannot be reaped (we are not its
        parent): if its process group is still alive we poll it until it
        exits; otherwise we mark it ``orphaned`` immediately.
        """
        if self._store is None or self._queue is None:
            self.initialize()
        assert self._store is not None and self._queue is not None
        for job in self._store.list():
            if not job.is_running:
                continue
            task = self._tasks.get(job.job_id)
            if task is not None and not task.done():
                continue
            if task is not None:
                self._tasks.pop(job.job_id, None)
            if _process_group_alive(job.pgid):
                self._spawn_task(job.job_id, self._poll_orphan(job))
            else:
                await self._mark_orphaned(job, alive=False)
        # Resume reaping for jobs this process actually owns.
        for job_id, proc in list(self._processes.items()):
            task = self._tasks.get(job_id)
            if task is None or task.done():
                self._spawn_task(job_id, self._reap(job_id, proc))

    def drain_pending_wakes(self, session_id: str | None) -> list[WakeEvent]:
        """Consume and return pending wakes for ``session_id`` (None = all)."""
        if self._queue is None:
            self.initialize()
        assert self._queue is not None
        return self._queue.consume([e.id for e in self._queue.pending(session_id)])

    def pending_wakes(self, session_id: str | None) -> list[WakeEvent]:
        """Non-destructive view of pending wakes (None = all sessions)."""
        if self._queue is None:
            self.initialize()
        assert self._queue is not None
        return self._queue.pending(session_id)

    def monitor_status(self) -> str | None:
        """Short status string for running jobs, or ``None`` when none.

        Duck-typed host hook (the CLI surfaces it next to monitors). Plain
        text; in-memory only, safe to call on every paint.
        """
        active = self._active_jobs()
        if not active:
            return None
        shown = ", ".join(j.name for j in active[:4])
        extra = len(active) - min(len(active), 4)
        if extra > 0:
            shown = f"{shown} +{extra}"
        return f"⚙ {shown}"

    # ── CLI extension: /jobs ──────────────────────────────────────────────

    def get_commands(self) -> list[CliCommandSpec]:
        return [
            CliCommandSpec(
                names=("/jobs",),
                help="List background jobs, their state and pending wakes",
                handler="handle_jobs",
                category="Plugins",
            )
        ]

    async def handle_jobs(
        self, command: CliCommandInvocation, context: CliCommandContext
    ) -> bool:
        self.initialize()
        assert self._store is not None and self._queue is not None
        args = command.args.strip()

        if args.lower() in ("pending", "wakes"):
            events = self._queue.pending(context.session_id or None)
            context.notify(
                "info",
                f"{len(events)} pending background-job wake(s) for this session."
                if events
                else "No pending background-job wakes for this session.",
            )
            return True

        if args:
            target = self._store.get(args)
            jobs = [target] if target is not None else []
            if not jobs:
                context.notify("warn", f"No job with id {args!r}.")
                return True
        else:
            jobs = self._store.list()

        items: list[tuple[str, str]] = []
        for job in jobs:
            items.append((job.name, f"{job.state} · {job.job_id}"))
            detail = f"command: {job.command}"
            if job.returncode is not None:
                detail += f" · exit: {job.returncode}"
            items.append(("", detail))
        pending = self._queue.pending(context.session_id or None)
        items.append(("", f"pending wakes (this session): {len(pending)}"))

        try:
            context.ui.publish(
                KeyValueBlock(
                    id="bgjobs-plugin:status",
                    title="Background jobs",
                    items=tuple(items),
                )
            )
        except Exception:  # noqa: BLE001 — non-interactive hosts
            logger.debug("plugin_ui unavailable for /jobs", exc_info=True)
        context.notify("info", f"{len(jobs)} job(s) listed.")
        return True

    # ── Tools ─────────────────────────────────────────────────────────────

    def get_tools(self) -> list[AgentTool]:
        plugin = self

        @tool(inject=["session_id_provider"])
        async def run_bg_job(
            command: str,
            name: str = "",
            cwd: str = "",
            env: dict | None = None,
            max_output_chars: int = 0,
            *,
            session_id_provider: Callable[[], str] | None = None,
        ) -> str:
            """Start a shell command as a background job and return immediately.

            The job runs detached (its own process group) and keeps running
            after this run ends. When it finishes, a wake with its exit code,
            an output tail and the duration is delivered to this session, so
            you can act on the result without polling.

            Use it for long jobs (builds, tests, training, deploys) and then
            keep working. Monitor or stop it with list_bg_jobs/stop_bg_job.

            Args:
                command: Shell command to run (a single string).
                name: Optional human-friendly name (defaults to the job id).
                cwd: Working directory (defaults to the host's cwd).
                env: Extra environment variables to merge into the host env.
                max_output_chars: Cap on the captured output tail (0 = default).
            """
            return await plugin._run_bg_job(
                command, name, cwd, env, max_output_chars, session_id_provider
            )

        @tool
        async def list_bg_jobs() -> str:
            """List background jobs with state, elapsed time and exit code."""
            return await plugin._list_bg_jobs()

        @tool
        async def stop_bg_job(job_id: str) -> str:
            """Stop a running background job (kills its whole process group).

            The completion wake is still delivered, with state ``stopped``.
            """
            return await plugin._stop_bg_job(job_id)

        @tool
        async def wait_bg_jobs(
            job_ids: list | None = None, timeout_seconds: int = 60
        ) -> str:
            """Block until one of the given jobs finishes, or the timeout hits.

            Args:
                job_ids: Job ids to wait on (default: all running jobs).
                timeout_seconds: Maximum seconds to block before returning.
            """
            return await plugin._wait_bg_jobs(job_ids, timeout_seconds)

        return [run_bg_job, list_bg_jobs, stop_bg_job, wait_bg_jobs]

    # ── Tool implementations ──────────────────────────────────────────────

    async def _run_bg_job(
        self,
        command: str,
        name: str,
        cwd: str,
        env: dict | None,
        max_output_chars: int,
        session_id_provider: Callable[[], str] | None,
    ) -> str:
        self.initialize()
        assert self._store is not None

        if not isinstance(command, str) or not command.strip():
            return "Error: command must be a non-empty string."
        job_id = os.urandom(6).hex()
        job_name = (name or job_id).strip() or job_id
        if name and not _NAME_RE.match(name):
            return (
                f"Error: invalid job name {name!r}. Use 1-64 chars from "
                "[A-Za-z0-9._-], starting with a letter or digit."
            )

        session_id = ""
        if callable(session_id_provider):
            try:
                session_id = str(session_id_provider() or "")
            except Exception:  # noqa: BLE001 — a broken provider is not fatal
                logger.warning("session_id_provider failed", exc_info=True)
        if not session_id:
            session_id = self._default_session_id

        resolved_cwd = os.path.expanduser(cwd) if cwd else os.getcwd()
        if not os.path.isdir(resolved_cwd):
            return f"Error: cwd {resolved_cwd!r} is not a directory."
        cap = (
            max_output_chars
            if max_output_chars and max_output_chars > 0
            else (self._default_max_output_chars)
        )

        job_dir = os.path.join(self._data_dir_expanded(), "jobs", job_id)
        try:
            os.makedirs(job_dir, exist_ok=True)
        except OSError as exc:
            return f"Error: could not create job directory: {exc}"
        log_path = os.path.join(job_dir, "output.log")

        child_env = dict(os.environ)
        if env:
            if not isinstance(env, dict):
                return "Error: env must be an object of string values."
            child_env.update({str(k): str(v) for k, v in env.items()})

        new_session = os.name != "nt"
        spawn_kw: dict[str, Any] = {
            "stdout": None,
            "stderr": asyncio.subprocess.STDOUT,
            "cwd": resolved_cwd,
            "env": child_env,
        }
        if new_session:
            spawn_kw["start_new_session"] = True

        try:
            with open(log_path, "wb") as log_fh:
                spawn_kw["stdout"] = log_fh
                proc = await asyncio.create_subprocess_shell(command, **spawn_kw)
        except OSError as exc:
            return f"Error: failed to start process: {exc}"

        try:
            pgid = os.getpgid(proc.pid) if new_session else proc.pid
        except (ProcessLookupError, OSError):
            pgid = proc.pid

        job = JobDef(
            job_id=job_id,
            name=job_name,
            command=command,
            cwd=resolved_cwd,
            session_id=session_id,
            state="running",
            pid=proc.pid,
            pgid=pgid,
            max_output_chars=cap,
            log_path=log_path,
        )
        try:
            self._store.add(job)
        except ValueError as exc:
            return f"Error: {exc}"

        self._processes[job_id] = proc
        self._started_at[job_id] = time.monotonic()
        self._spawn_task(job_id, self._reap(job_id, proc))
        return (
            f"Background job {job_id} started (name={job_name!r}, pid={proc.pid}). "
            f"It runs detached; you will be woken with its exit code and output "
            f"when it finishes. Inspect it with list_bg_jobs, stop it with "
            f"stop_bg_job({job_id!r})."
        )

    async def _list_bg_jobs(self) -> str:
        self.initialize()
        assert self._store is not None and self._queue is not None
        jobs = self._store.list()
        if not jobs:
            return "No background jobs."
        lines = ["Background jobs:"]
        for job in jobs:
            elapsed = ""
            if job.state == "running" and job.job_id in self._started_at:
                taken = time.monotonic() - self._started_at[job.job_id]
                elapsed = f" · elapsed: {taken:.1f}s"
            exit_part = (
                f" · exit: {job.returncode}" if job.returncode is not None else ""
            )
            pending = len(self._queue.pending(job.session_id))
            lines.append(
                f"- {job.job_id} {job.name!r} [{job.state}] pid={job.pid}"
                f"{elapsed}{exit_part} · cmd={job.command!r} · pending wakes: {pending}"
            )
        return "\n".join(lines)

    async def _stop_bg_job(self, job_id: str) -> str:
        self.initialize()
        assert self._store is not None
        job = self._store.get(job_id)
        if job is None:
            return f"Error: job {job_id!r} does not exist."
        if not job.is_running:
            return f"Job {job_id!r} is already {job.state}."

        self._stopping.add(job_id)
        proc = self._processes.get(job_id)
        self._kill_job(job, proc)
        task = self._tasks.get(job_id)
        if task is not None:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        return f"Job {job_id!r} stopped (process group killed)."

    async def _wait_bg_jobs(self, job_ids: list | None, timeout_seconds: int) -> str:
        self.initialize()
        assert self._store is not None
        if job_ids:
            wanted = [str(j) for j in job_ids]
        else:
            wanted = [j.job_id for j in self._store.list() if j.is_running]
        running = [
            self._tasks.get(j)
            for j in wanted
            if (task := self._tasks.get(j)) is not None and not task.done()
        ]
        running = [t for t in running if t is not None]
        if not running:
            return "No running jobs to wait for."
        timeout = max(1, int(timeout_seconds))
        done, _pending = await asyncio.wait(running, timeout=timeout)
        if not done:
            return f"Timed out after {timeout}s; {len(running)} job(s) still running."
        finished = [j.job_id for j in self._store.list() if not j.is_running]
        return (
            "Job(s) finished: " + ", ".join(finished)
            if finished
            else "Job(s) finished."
        )

    # ── Job lifecycle ─────────────────────────────────────────────────────

    async def _reap(self, job_id: str, proc: asyncio.subprocess.Process) -> None:
        """Await ``proc`` (with optional watchdog) and finalize the job."""
        timed_out = False
        try:
            if self._watchdog_seconds > 0:
                try:
                    await asyncio.wait_for(proc.wait(), timeout=self._watchdog_seconds)
                except TimeoutError:
                    timed_out = True
                    await self._kill_process(job_id, proc)
                    await proc.wait()
            else:
                await proc.wait()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — a broken reaper must not kill the host
            logger.exception("Reaper for job %r crashed", job_id)
            return

        job = self._store.get(job_id) if self._store is not None else None
        if job is None:
            return
        assert self._store is not None
        stopped = job_id in self._stopping
        state = "stopped" if stopped else "completed"
        duration_ms = None
        if job_id in self._started_at:
            duration_ms = int((time.monotonic() - self._started_at.pop(job_id)) * 1000)
        finished = replace(
            job,
            state=state,
            finished_at=_utc_now_iso(),
            returncode=proc.returncode,
            timed_out=timed_out,
            duration_ms=duration_ms,
            output_tail=_read_output_tail(job.log_path, job.max_output_chars),
        )
        try:
            self._store.replace(finished)
        except (KeyError, ValueError):
            return
        self._processes.pop(job_id, None)
        self._tasks.pop(job_id, None)
        self._stopping.discard(job_id)
        await self._fire_wake(job, finished)

    async def _poll_orphan(self, job: JobDef) -> None:
        """Poll a job inherited from a previous process until it is gone."""
        while _process_group_alive(job.pgid):
            await asyncio.sleep(2.0)
        await self._mark_orphaned(job, alive=False)

    async def _mark_orphaned(self, job: JobDef, *, alive: bool) -> None:
        if self._store is None:
            return
        current = self._store.get(job.job_id)
        if current is None or not current.is_running:
            return
        orphaned = replace(
            current,
            state="orphaned",
            finished_at=_utc_now_iso(),
            output_tail=_read_output_tail(current.log_path, current.max_output_chars),
        )
        try:
            self._store.replace(orphaned)
        except (KeyError, ValueError):
            return
        self._tasks.pop(job.job_id, None)
        await self._fire_wake(current, orphaned)

    async def _fire_wake(self, job: JobDef, finished: JobDef) -> None:
        assert self._store is not None and self._queue is not None
        async with self._wake_lock:
            event = WakeEvent.create(
                job_id=finished.job_id,
                name=finished.name,
                session_id=finished.session_id,
                payload={
                    "state": finished.state,
                    "command": finished.command,
                    "returncode": finished.returncode,
                    "timed_out": finished.timed_out,
                    "duration_ms": finished.duration_ms,
                    "output_tail": finished.output_tail,
                },
            )
            stored = self._queue.append(event)
        if stored is None:
            return
        if self._on_wake is not None:
            with contextlib.suppress(RuntimeError):
                asyncio.get_running_loop().create_task(self._invoke_on_wake(stored))

    async def _invoke_on_wake(self, event: WakeEvent) -> None:
        on_wake = self._on_wake
        if on_wake is None:
            return
        try:
            result = on_wake(event)
            if asyncio.iscoroutine(result):
                await result
        except Exception:  # noqa: BLE001 — never lose the queued event
            logger.exception("on_wake callback failed for wake %s", event.id)

    # ── Task/process management ───────────────────────────────────────────

    def _spawn_task(self, job_id: str, coro) -> None:
        self._tasks[job_id] = asyncio.create_task(coro, name=f"bgjob:{job_id}")

    def _kill_job(self, job: JobDef, proc: asyncio.subprocess.Process | None) -> None:
        if proc is not None:
            self._kill_process_sync(job, proc)

    def _kill_process_sync(self, job: JobDef, proc: asyncio.subprocess.Process) -> None:
        try:
            if job.pgid and os.name != "nt":
                os.killpg(job.pgid, signal.SIGKILL)
            else:
                proc.kill()
        except (ProcessLookupError, PermissionError, OSError):
            with contextlib.suppress(ProcessLookupError):
                proc.kill()

    async def _kill_process(
        self, job_id: str, proc: asyncio.subprocess.Process
    ) -> None:
        job = self._store.get(job_id) if self._store is not None else None
        if job is not None:
            self._kill_process_sync(job, proc)
            return
        with contextlib.suppress(ProcessLookupError):
            proc.kill()

    def _active_jobs(self) -> list[JobDef]:
        if self._store is None:
            self.initialize()
        assert self._store is not None
        return [j for j in self._store.list() if j.is_running]

    def _data_dir_expanded(self) -> str:
        return os.path.expanduser(self._data_dir)


def _utc_now_iso() -> str:
    import datetime

    return datetime.datetime.now(datetime.UTC).isoformat()


def create_plugin() -> BgJobsPlugin:
    """Factory for the path-based loader.

    Style: ``path:./phoson_plugin_bgjobs/_plugin.py``.
    """
    return BgJobsPlugin()


__all__ = [
    "BgJobsPlugin",
    "create_plugin",
    "render_wake_message",
]

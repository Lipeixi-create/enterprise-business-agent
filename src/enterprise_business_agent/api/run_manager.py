from __future__ import annotations

import logging
import os
import socket
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any, Callable

from ..agent import AgentRuntime
from ..config import Settings
from ..security import Principal
from .run_store import PersistentRunStore, RunClaim, RunClaimLost

TERMINAL_STATUSES = frozenset(
    {
        "completed",
        "completed_with_warnings",
        "no_evidence",
        "no_data",
        "failed",
        "budget_exhausted",
        "timeout",
        "llm_unavailable",
        "llm_timeout",
        "invalid_action",
        "repeated_tool_call",
        "tool_failure",
        "cancelled",
        "interrupted",
    }
)

# A run may be resumed only when the stop was caused by an interruption or a
# transient dependency. Permission, budget, duplicate-call and user-cancel
# outcomes are deliberate terminal decisions and are not silently retried.
RECOVERABLE_TERMINAL_STATUSES = frozenset({"failed", "llm_unavailable", "llm_timeout", "tool_failure"})
UNRECOVERABLE_STOP_REASONS = frozenset(
    {
        "permission_denied",
        "tool_budget_exhausted",
        "token_budget_exhausted",
        "time_budget_exhausted",
        "repeated_tool_call",
        "cancelled_by_user",
        "cancelled_before_start",
    }
)


class RunResumeError(Exception):
    code = "run_not_resumable"

    def __init__(
        self,
        code: str,
        message: str,
        *,
        detail: dict[str, Any] | None = None,
    ):
        self.code = code
        self.message = message
        self.detail = detail or {}
        super().__init__(message)


RESUMABLE_STATUSES = frozenset({"interrupted", "failed", "llm_unavailable", "llm_timeout", "tool_failure"})


class _LeaseHeartbeat:
    """Renew a DB lease while an attempt is alive.

    If renewal fails, the lease has been taken over. The heartbeat sets the
    runtime's cancel event and records ``lost``; the final fenced write then
    refuses to overwrite the new owner's result.
    """

    def __init__(
        self,
        *,
        store: PersistentRunStore,
        claim: RunClaim,
        lease_seconds: float,
        cancel_event: threading.Event,
        logger: logging.Logger,
    ):
        self.store = store
        self.claim = claim
        self.lease_seconds = lease_seconds
        self.cancel_event = cancel_event
        self.logger = logger
        self.lost = threading.Event()
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._run,
            name=f"eba-lease-{claim.run_id[:8]}",
            daemon=True,
        )

    def start(self) -> "_LeaseHeartbeat":
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=max(1.0, self.lease_seconds))

    def _run(self) -> None:
        interval = max(0.2, self.lease_seconds / 3)
        while not self._stop.wait(interval):
            try:
                renewed = self.store.renew_claim(self.claim, self.lease_seconds)
            except Exception:
                renewed = False
            if not renewed:
                self.lost.set()
                self.cancel_event.set()
                self.logger.error(
                    "run_claim_lost",
                    extra={
                        "fields": {
                            "run_id": self.claim.run_id,
                            "claim_version": self.claim.claim_version,
                        }
                    },
                )
                return


def _is_resumable(document: dict[str, Any]) -> tuple[bool, str]:
    status = str(document.get("status") or "")
    stop_reason = str(document.get("stop_reason") or "")
    if status == "interrupted":
        return True, ""
    if status in RECOVERABLE_TERMINAL_STATUSES:
        if stop_reason in UNRECOVERABLE_STOP_REASONS:
            return False, "the run stopped on a non-recoverable decision"
        return True, ""
    return False, f"status {status!r} is not resumable"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class RunManager:
    """Owns the lifecycle of long-running agent tasks.

    The API request returns a run id immediately; the LLM loop runs in a small
    bounded thread pool. Cancellation is cooperative (the runtime checks an
    event between decisions and before each tool call) and only works in the
    process that owns the task. A restart marks unfinished rows ``interrupted``
    instead of pretending they are still running.
    """

    def __init__(
        self,
        *,
        runtime: AgentRuntime,
        store: PersistentRunStore,
        settings: Settings,
        logger: logging.Logger,
        max_workers: int = 4,
        worker_id: str | None = None,
    ):
        self.runtime = runtime
        self.store = store
        self.settings = settings
        self.logger = logger
        self.worker_id = worker_id or (f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}")
        self._executor = ThreadPoolExecutor(max_workers=max(1, max_workers), thread_name_prefix="eba-agent-run")
        self._cancel_events: dict[str, threading.Event] = {}
        self._lock = threading.Lock()

    def submit(
        self,
        *,
        question: str,
        principal: Principal,
        request_id: str,
        max_tool_calls: int | None,
        max_seconds: float | None,
        idempotency_key: str | None = None,
        trace_context: Any = None,
    ) -> tuple[dict[str, Any], bool]:
        if idempotency_key is not None:
            existing = self.store.find_by_idempotency(principal.principal_id, idempotency_key)
            if existing is not None:
                return existing, False
        run_id = str(uuid.uuid4())
        queued = self._queued_document(
            run_id=run_id,
            request_id=request_id,
            question=question,
            principal=principal,
        )
        payload, created = self.store.create(
            run_id=run_id,
            request_id=request_id,
            owner=principal.principal_id,
            status="queued",
            question=question,
            payload=queued,
            idempotency_key=idempotency_key,
        )
        if not created:
            return payload, False
        cancel_event = threading.Event()
        with self._lock:
            self._cancel_events[run_id] = cancel_event
        self.store.append_event(
            event_type="run_created",
            run_id=run_id,
            request_id=request_id,
            principal_id=principal.principal_id,
            detail={"question_length": len(question)},
        )
        self._executor.submit(
            self._execute,
            run_id,
            question,
            principal,
            request_id,
            max_tool_calls,
            max_seconds,
            cancel_event,
            trace_context,
        )
        return payload, True

    def cancel(self, run_id: str, principal: Principal, *, owner: str | None = None) -> tuple[bool, str]:
        with self._lock:
            event = self._cancel_events.get(run_id)
        document = self.store.get(run_id, owner=owner)
        if document is None:
            return False, "not_found"
        status = str(document.get("status"))
        if status in TERMINAL_STATUSES:
            return False, "already_finished"
        if event is None:
            return False, "not_active_in_this_process"
        event.set()
        self.store.update_status(run_id, "cancelling", owner=owner)
        self.store.append_event(
            event_type="cancel_requested",
            run_id=run_id,
            request_id=str(document.get("request_id") or ""),
            principal_id=principal.principal_id,
            detail={},
        )
        return True, "cancelling"

    def events(self, run_id: str, principal: Principal, *, owner: str | None = None) -> list[dict[str, Any]]:
        return self.store.list_events(run_id, owner=owner)

    def _lifecycle_budget(self, run_id: str) -> tuple[float, dict[str, Any]]:
        """Return remaining lifecycle seconds and the persisted counters.

        The budget is a whole-run wall-clock limit: both accumulated execution
        time and time since creation count. A queue wait or a crash therefore
        cannot be reset by resuming.
        """

        state = self.store.lifecycle_state(run_id)
        if state is None:
            return 0.0, {}
        created_at = str(state.get("created_at") or "")
        wall_seconds = 0.0
        try:
            created = datetime.fromisoformat(created_at)
            if created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
            wall_seconds = max(0.0, (datetime.now(timezone.utc) - created).total_seconds())
        except ValueError:
            wall_seconds = 0.0
        consumed = max(float(state.get("execution_seconds") or 0.0), wall_seconds)
        remaining = max(0.0, float(self.settings.agent_run_max_lifecycle_seconds) - consumed)
        return remaining, {
            "consumed_seconds": round(consumed, 3),
            "execution_seconds": round(float(state.get("execution_seconds") or 0.0), 3),
            "wall_seconds": round(wall_seconds, 3),
            "remaining_seconds": round(remaining, 3),
            "resume_count": int(state.get("resume_count") or 0),
        }

    def _lifecycle_payload(self, run_id: str) -> dict[str, Any]:
        state = self.store.lifecycle_state(run_id) or {}
        return {
            "execution_seconds": round(float(state.get("execution_seconds") or 0.0), 3),
            "lifecycle_limit_seconds": float(self.settings.agent_run_max_lifecycle_seconds),
            "resume_count": int(state.get("resume_count") or 0),
            "last_resumed_at": state.get("last_resumed_at"),
        }

    def resume(
        self,
        run_id: str,
        principal: Principal,
        *,
        owner: str | None,
        request_id: str | None = None,
        max_seconds: float | None = None,
        trace_context: Any = None,
    ) -> dict[str, Any]:
        """Resume a checkpointed run under a database claim.

        The claim must be acquired before the checkpointer is read. A second
        process holding an unexpired lease is rejected with
        ``run_claim_conflict``; after a crash the lease expires and a new
        worker can take over. Every state write carries the fencing version.
        """

        document = self.store.get(run_id, owner=owner)
        if document is None:
            raise RunResumeError("agent_run_not_found", "no stored agent run for this run_id")
        with self._lock:
            if run_id in self._cancel_events:
                raise RunResumeError("run_already_active", "the run is still active in this process")
        resumable, reason = _is_resumable(document)
        if not resumable:
            raise RunResumeError("run_not_resumable", reason)

        remaining, budget = self._lifecycle_budget(run_id)
        if remaining <= 0:
            self.store.append_event(
                event_type="run_lifecycle_exhausted",
                run_id=run_id,
                request_id=request_id,
                principal_id=principal.principal_id,
                detail=budget,
            )
            raise RunResumeError(
                "run_lifecycle_exhausted",
                "the run has consumed its whole-lifecycle wall-clock budget",
                detail=budget,
            )

        attempt = self.store.acquire_claim(
            run_id,
            self.worker_id,
            self.settings.run_lease_seconds,
            allowed_statuses=RESUMABLE_STATUSES,
        )
        if attempt.claim is None:
            if attempt.reason == "lease_held":
                raise RunResumeError(
                    "run_claim_conflict",
                    "another worker holds an unexpired lease for this run",
                    detail={
                        "lease_expires_at": attempt.lease_expires_at,
                        "held_by": attempt.held_by,
                    },
                )
            if attempt.reason == "not_found":
                raise RunResumeError("agent_run_not_found", "no stored agent run for this run_id")
            raise RunResumeError(
                "run_not_resumable",
                "the run changed state before the claim could be acquired",
            )
        claim = attempt.claim

        previous_status = str(document.get("status") or "")
        previous_reason = document.get("stop_reason")
        request_id = request_id or str(document.get("request_id") or run_id)
        cancel_event = threading.Event()
        with self._lock:
            self._cancel_events[run_id] = cancel_event
        heartbeat = _LeaseHeartbeat(
            store=self.store,
            claim=claim,
            lease_seconds=self.settings.run_lease_seconds,
            cancel_event=cancel_event,
            logger=self.logger,
        ).start()
        started = time.monotonic()
        try:
            self.store.update_status(run_id, "running", owner=owner, fence=claim)
            self.store.append_event(
                event_type="run_resumed",
                run_id=run_id,
                request_id=request_id,
                principal_id=principal.principal_id,
                detail={
                    "previous_status": previous_status,
                    "previous_stop_reason": previous_reason,
                    "role": principal.role,
                    "worker_id": self.worker_id,
                    "claim_version": claim.claim_version,
                    "lifecycle": budget,
                },
                fence=claim,
            )
            effective_seconds = min(
                max_seconds or self.settings.agent_max_seconds,
                self.settings.agent_max_seconds,
                remaining,
            )
            trace_sink = self._sink(run_id, principal, request_id, fence=claim)
            response = self.runtime.resume(
                run_id,
                principal,
                request_id=request_id,
                max_seconds=effective_seconds,
                cancel_event=cancel_event,
                trace_sink=trace_sink,
                trace_context=trace_context,
            )
            elapsed = time.monotonic() - started
            self.store.record_execution(claim, elapsed, resumed=True)
            result = response.model_dump(mode="json")
            result.update(self._lifecycle_payload(run_id))
            self.store.put(
                run_id,
                result,
                owner=principal.principal_id,
                request_id=request_id,
                status=response.status.value,
                question=response.question,
                fence=claim,
            )
            self.store.append_event(
                event_type="run_finished",
                run_id=run_id,
                request_id=request_id,
                principal_id=principal.principal_id,
                detail={
                    "status": response.status.value,
                    "stop_reason": response.stop_reason,
                    "resumed": True,
                    "worker_id": self.worker_id,
                    "attempt_seconds": round(elapsed, 3),
                },
                fence=claim,
            )
            return result
        except RunClaimLost as exc:
            raise RunResumeError(
                "run_claim_lost",
                "another worker took over this run; the stale result was not persisted",
                detail={
                    "worker_id": self.worker_id,
                    "claim_version": claim.claim_version,
                },
            ) from exc
        except Exception as exc:  # resume must end with a durable status
            elapsed = time.monotonic() - started
            try:
                self.store.record_execution(claim, elapsed, resumed=True)
                failed = self._failed_document(
                    run_id,
                    request_id,
                    str(document.get("question") or ""),
                    principal,
                    exc,
                )
                failed.update(self._lifecycle_payload(run_id))
                self.store.put(
                    run_id,
                    failed,
                    owner=principal.principal_id,
                    request_id=request_id,
                    status="failed",
                    question=str(document.get("question") or ""),
                    fence=claim,
                )
                self.store.append_event(
                    event_type="run_failed",
                    run_id=run_id,
                    request_id=request_id,
                    principal_id=principal.principal_id,
                    detail={"error_type": type(exc).__name__, "stage": "resume"},
                    fence=claim,
                )
            except RunClaimLost as lost:
                raise RunResumeError(
                    "run_claim_lost",
                    "another worker took over this run; the stale failure was not persisted",
                    detail={
                        "worker_id": self.worker_id,
                        "claim_version": claim.claim_version,
                    },
                ) from lost
            self.logger.error(
                "agent_run_resume_failed",
                extra={
                    "fields": {
                        "run_id": run_id,
                        "error_type": type(exc).__name__,
                    }
                },
            )
            raise
        finally:
            heartbeat.stop()
            self.store.release_claim(claim)
            with self._lock:
                self._cancel_events.pop(run_id, None)

    def shutdown(self) -> None:
        with self._lock:
            for event in self._cancel_events.values():
                event.set()
            self._cancel_events.clear()
        self._executor.shutdown(wait=False, cancel_futures=True)

    def _execute(
        self,
        run_id: str,
        question: str,
        principal: Principal,
        request_id: str,
        max_tool_calls: int | None,
        max_seconds: float | None,
        cancel_event: threading.Event,
        trace_context: Any = None,
    ) -> None:
        attempt = self.store.acquire_claim(
            run_id,
            self.worker_id,
            self.settings.run_lease_seconds,
            allowed_statuses={"queued", "cancelling"},
        )
        if attempt.claim is None:
            self.logger.error(
                "agent_run_claim_failed",
                extra={"fields": {"run_id": run_id, "reason": attempt.reason}},
            )
            with self._lock:
                self._cancel_events.pop(run_id, None)
            return
        claim = attempt.claim
        heartbeat = _LeaseHeartbeat(
            store=self.store,
            claim=claim,
            lease_seconds=self.settings.run_lease_seconds,
            cancel_event=cancel_event,
            logger=self.logger,
        ).start()
        started = time.monotonic()
        try:
            if cancel_event.is_set():
                self._finish_cancelled(run_id, principal, request_id, fence=claim)
                return
            remaining, budget = self._lifecycle_budget(run_id)
            if remaining <= 0:
                exhausted = self._queued_document(
                    run_id=run_id,
                    request_id=request_id,
                    question=question,
                    principal=principal,
                )
                exhausted["status"] = "budget_exhausted"
                exhausted["stop_reason"] = "time_budget_exhausted"
                exhausted["finished_at"] = _now()
                exhausted.update(self._lifecycle_payload(run_id))
                self.store.put(
                    run_id,
                    exhausted,
                    owner=principal.principal_id,
                    request_id=request_id,
                    status="budget_exhausted",
                    question=question,
                    fence=claim,
                )
                self.store.append_event(
                    event_type="run_finished",
                    run_id=run_id,
                    request_id=request_id,
                    principal_id=principal.principal_id,
                    detail={"status": "budget_exhausted", "lifecycle": budget},
                    fence=claim,
                )
                return
            self.store.update_status(run_id, "running", owner=principal.principal_id, fence=claim)
            effective_seconds = min(
                max_seconds or self.settings.agent_max_seconds,
                self.settings.agent_max_seconds,
                remaining,
            )
            trace_sink = self._sink(run_id, principal, request_id, fence=claim)
            response = self.runtime.run(
                question,
                principal,
                max_tool_calls=max_tool_calls,
                max_seconds=effective_seconds,
                request_id=request_id,
                cancel_event=cancel_event,
                trace_sink=trace_sink,
                run_id=run_id,
                trace_context=trace_context,
            )
            elapsed = time.monotonic() - started
            self.store.record_execution(claim, elapsed, resumed=False)
            document = response.model_dump(mode="json")
            document.update(self._lifecycle_payload(run_id))
            self.store.put(
                run_id,
                document,
                owner=principal.principal_id,
                request_id=request_id,
                status=response.status.value,
                question=question,
                fence=claim,
            )
            self.store.append_event(
                event_type="run_finished",
                run_id=run_id,
                request_id=request_id,
                principal_id=principal.principal_id,
                detail={
                    "status": response.status.value,
                    "stop_reason": response.stop_reason,
                    "worker_id": self.worker_id,
                    "attempt_seconds": round(elapsed, 3),
                },
                fence=claim,
            )
        except RunClaimLost:
            self.logger.error(
                "agent_run_claim_lost",
                extra={
                    "fields": {
                        "run_id": run_id,
                        "claim_version": claim.claim_version,
                    }
                },
            )
        except Exception as exc:  # the task must end with a durable status
            self.logger.error(
                "agent_run_failed",
                extra={
                    "fields": {
                        "run_id": run_id,
                        "error_type": type(exc).__name__,
                    }
                },
            )
            try:
                elapsed = time.monotonic() - started
                self.store.record_execution(claim, elapsed, resumed=False)
                document = self._failed_document(run_id, request_id, question, principal, exc)
                document.update(self._lifecycle_payload(run_id))
                self.store.put(
                    run_id,
                    document,
                    owner=principal.principal_id,
                    request_id=request_id,
                    status="failed",
                    question=question,
                    fence=claim,
                )
                self.store.append_event(
                    event_type="run_failed",
                    run_id=run_id,
                    request_id=request_id,
                    principal_id=principal.principal_id,
                    detail={"error_type": type(exc).__name__},
                    fence=claim,
                )
            except RunClaimLost:
                self.logger.error(
                    "agent_run_claim_lost",
                    extra={
                        "fields": {
                            "run_id": run_id,
                            "claim_version": claim.claim_version,
                        }
                    },
                )
        finally:
            heartbeat.stop()
            self.store.release_claim(claim)
            with self._lock:
                self._cancel_events.pop(run_id, None)

    def _finish_cancelled(
        self,
        run_id: str,
        principal: Principal,
        request_id: str,
        *,
        fence: RunClaim | None = None,
    ) -> None:
        document = self.store.get(run_id, owner=principal.principal_id) or {}
        document["status"] = "cancelled"
        document["stop_reason"] = "cancelled_before_start"
        document["finished_at"] = _now()
        document.update(self._lifecycle_payload(run_id))
        self.store.put(
            run_id,
            document,
            owner=principal.principal_id,
            status="cancelled",
            fence=fence,
        )
        self.store.append_event(
            event_type="run_finished",
            run_id=run_id,
            request_id=request_id,
            principal_id=principal.principal_id,
            detail={"status": "cancelled", "stop_reason": "cancelled_before_start"},
            fence=fence,
        )

    def _sink(
        self,
        run_id: str,
        principal: Principal,
        request_id: str,
        *,
        fence: RunClaim | None = None,
    ) -> Callable[[dict[str, Any]], None]:
        def emit(event: dict[str, Any]) -> None:
            event_type = str(event.pop("event_type", "agent_event"))
            tool_call_id = event.get("call_id") or event.get("tool_call_id")
            detail = {key: value for key, value in event.items() if key not in {"call_id", "tool_call_id"}}
            self.store.append_event(
                event_type=event_type,
                run_id=run_id,
                request_id=request_id,
                principal_id=principal.principal_id,
                tool_call_id=str(tool_call_id) if tool_call_id else None,
                detail=detail,
                fence=fence,
            )
            if event_type == "tool_call_finished" and event.get("status") == "permission_denied":
                self.store.append_event(
                    event_type="permission_denied",
                    run_id=run_id,
                    request_id=request_id,
                    principal_id=principal.principal_id,
                    tool_call_id=str(tool_call_id) if tool_call_id else None,
                    detail={
                        "tool": event.get("tool"),
                        "error_code": event.get("error_code"),
                    },
                    fence=fence,
                )
            self.logger.info(
                "agent_trace",
                extra={
                    "fields": {
                        "run_id": run_id,
                        "event_type": event_type,
                        "tool": event.get("tool"),
                        "status": event.get("status"),
                    }
                },
            )

        return emit

    def _queued_document(
        self,
        *,
        run_id: str,
        request_id: str,
        question: str,
        principal: Principal,
    ) -> dict[str, Any]:
        return {
            "run_id": run_id,
            "request_id": request_id,
            "question": question,
            "status": "queued",
            "stop_reason": None,
            "planner": "langgraph",
            "planner_model": getattr(self.runtime.llm, "model", None),
            "principal_id": principal.principal_id,
            "role": principal.role,
            "final_answer": None,
            "verified_findings": [],
            "open_questions": [],
            "warnings": [],
            "errors": [],
            "steps": [],
            "tool_calls": [],
            "evidence": [],
            "state": {
                "goal": question,
                "executed_steps": [],
                "observations": [],
                "verified_findings": [],
                "open_questions": [],
                "errors": [],
                "tool_calls_used": 0,
                "max_tool_calls": 0,
                "remaining_budget": 0,
                "stop_reason": None,
            },
            "started_at": _now(),
            "finished_at": None,
            "llm_latency_ms": None,
            "llm_tokens": {},
        }

    def _failed_document(
        self,
        run_id: str,
        request_id: str,
        question: str,
        principal: Principal,
        exc: Exception,
    ) -> dict[str, Any]:
        document = self._queued_document(
            run_id=run_id,
            request_id=request_id,
            question=question,
            principal=principal,
        )
        document["status"] = "failed"
        document["stop_reason"] = "unhandled_run_error"
        document["errors"] = [
            {
                "code": "agent_error",
                "message": f"{type(exc).__name__}: run failed",
                "stage": "run_manager",
            }
        ]
        document["finished_at"] = _now()
        return document

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Literal

import anyio
from fastapi import (
    APIRouter,
    Depends,
    Header,
    HTTPException,
    Query,
    Request,
    Response,
)
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text

from .. import __version__
from ..agent import AgentRunRequest, AgentRunResponse
from ..analysis.contracts import AnalysisStatus, MetricQueryRequest
from ..analysis.engine import query_metric
from ..evidence import (
    ApprovalNotFound,
    ApprovalSeparationViolation,
    ApprovalStale,
    InvalidApprovalTransition,
)
from ..evidence import (
    report_fingerprint as compute_report_fingerprint,
)
from ..mcp import MCP_PROTOCOL_VERSIONS
from ..nl.contracts import NaturalLanguageRequest, NaturalLanguageResponse, StructuredQueryRequest
from ..nl.service import NaturalLanguageAnalyzer
from ..observability.alerts import evaluate_alerts
from ..observability.setup import current_otel_context
from ..security import AuthorizationError, Principal
from ..security.audit import SECURITY_EVENT_TYPES, record_security_event
from ..security.auth import RESOURCE_SCOPES
from .auth import principal_from_request
from .contracts import ErrorResponse, HealthResponse, StructuredMetricQueryResponse
from .run_manager import TERMINAL_STATUSES, RunResumeError
from .run_metrics import metrics_snapshot

router = APIRouter()


def _request_id(request: Request) -> str:
    return getattr(request.state, "request_id", str(uuid.uuid4()))


def _metric_definition(request: Request, name: str):
    resolver = getattr(request.app.state, "metric_resolver", None)
    return resolver.get(name) if resolver is not None else None


def _forbidden(exc: AuthorizationError) -> HTTPException:
    return HTTPException(
        status_code=403,
        detail={
            "code": "forbidden",
            "message": str(exc),
            "stage": "authorization",
            "detail": exc.detail,
        },
    )


def _not_found(resource: str, run_id: str) -> HTTPException:
    return HTTPException(
        status_code=404,
        detail={
            "code": f"{resource}_not_found",
            "message": f"no {resource} is available for this run_id",
            "stage": "authorization",
            "request_id": run_id,
        },
    )


def _require_resource(request: Request, principal: Principal, resource: str, run_id: str) -> None:
    authorization = request.app.state.authorization
    scope = RESOURCE_SCOPES.get(resource)
    if scope is None:
        raise _not_found(resource, run_id)
    try:
        authorization.require_scope(principal, scope)
    except AuthorizationError as exc:
        raise _forbidden(exc) from exc
    owner = request.app.state.run_store.owner_of(run_id)
    if owner is None or not authorization.resource_allowed(principal, resource, owner):
        raise HTTPException(
            status_code=404,
            detail={
                "code": "agent_run_not_found",
                "message": "no stored agent run for this run_id",
                "stage": "run_store",
                "request_id": run_id,
            },
        )


async def _run_in_thread(request: Request, func):
    timeout = request.app.state.settings.api_timeout_seconds
    request_id = _request_id(request)
    try:
        with anyio.fail_after(timeout):
            return await anyio.to_thread.run_sync(func, abandon_on_cancel=True)
    except TimeoutError as exc:
        raise HTTPException(
            status_code=504,
            detail={
                "code": "api_timeout",
                "message": f"request exceeded API timeout of {timeout:.1f}s",
                "stage": "api",
                "request_id": request_id,
            },
        ) from exc


@router.get("/health", response_model=HealthResponse)
def health(request: Request, response: Response) -> HealthResponse:
    app = request.app
    request_id = _request_id(request)
    engine = getattr(app.state, "engine", None)
    database = "ok"
    details: dict[str, str] = {}
    try:
        if engine is None:
            raise RuntimeError("database engine is not configured")
        with engine.connect() as connection:
            connection.execute(text("SELECT 1")).scalar_one()
    except Exception as exc:
        database = "unavailable"
        details["database_error"] = type(exc).__name__
    status = "ok" if database == "ok" else "degraded"
    if status != "ok":
        response.status_code = 503
    return HealthResponse(
        status=status,
        request_id=request_id,
        database=database,
        dialect=app.state.dialect,
        readonly=True,
        llm_configured=app.state.llm_client is not None,
        agent_available=(getattr(app.state, "agent_runtime", None) is not None and app.state.llm_client is not None),
        auth_enabled=bool(app.state.settings.api_keys),
        anonymous_allowed=app.state.settings.allow_anonymous_api,
        version=__version__,
        details=details,
    )


def _analyze(request: Request, payload: NaturalLanguageRequest) -> NaturalLanguageResponse:
    request_id = _request_id(request)
    principal = request.state.principal
    with request.app.state.session_factory() as session:
        analyzer = NaturalLanguageAnalyzer(
            session,
            request.app.state.settings,
            llm_client=request.app.state.llm_client,
            principal=principal,
            authorization=request.app.state.authorization,
        )
        return analyzer.analyze(payload, request_id=request_id)


@router.post(
    "/api/v1/analysis/natural-language",
    response_model=NaturalLanguageResponse,
)
async def natural_language_analysis(
    payload: NaturalLanguageRequest,
    request: Request,
    principal: Principal = Depends(principal_from_request),
) -> NaturalLanguageResponse:
    _require_metrics_scope(principal)
    request.state.principal = principal
    response = await _run_in_thread(request, lambda: _analyze(request, payload))
    if response.status == AnalysisStatus.FORBIDDEN:
        detail = dict(response.error.detail) if response.error else {}
        if response.error is not None and response.error.code != "forbidden":
            detail["reason"] = response.error.code
        raise HTTPException(
            status_code=403,
            detail={
                "code": "forbidden",
                "message": response.error.message if response.error else "request is not authorized",
                "stage": "authorization",
                "detail": detail,
            },
        )
    request.app.state.result_store.put(
        response.request_id,
        response.model_dump(mode="json"),
        owner=principal.principal_id,
    )
    return response


def _structured_query(
    request: Request, payload: StructuredQueryRequest, principal: Principal
) -> StructuredMetricQueryResponse:
    settings = request.app.state.settings
    request_id = _request_id(request)
    definition = _metric_definition(request, payload.metric)
    try:
        request.app.state.authorization.require_request(
            principal,
            metric=payload.metric,
            dimensions=payload.dimensions,
            filters=payload.filters,
            metric_domain=getattr(definition, "domain", None),
            metric_source=getattr(definition, "source_table", None),
        )
    except AuthorizationError as exc:
        return StructuredMetricQueryResponse(
            request_id=request_id,
            status=AnalysisStatus.FORBIDDEN,
            error={
                "code": "forbidden",
                "message": str(exc),
                "stage": "authorization",
                "detail": exc.detail,
            },
        )
    try:
        query_request = MetricQueryRequest(
            metric=payload.metric,
            start=payload.start or settings.data_start,
            end=payload.end or settings.data_end,
            grain=payload.grain,
            dimensions=payload.dimensions,
            filters=payload.filters,
            aggregation=payload.aggregation,
        )
    except ValueError as exc:
        return StructuredMetricQueryResponse(
            request_id=request_id,
            status=AnalysisStatus.INVALID_REQUEST,
            error={
                "code": "invalid_request",
                "message": str(exc),
                "stage": "request_build",
                "detail": {},
            },
        )
    with request.app.state.session_factory() as session:
        result = query_metric(session, query_request)
    return StructuredMetricQueryResponse(
        request_id=request_id,
        status=result.status,
        result=result,
        error=None
        if result.status == AnalysisStatus.OK
        else {
            "code": result.status.value,
            "message": result.reason or "metric query did not produce a complete result",
            "stage": "analysis",
            "detail": {},
        },
    )


@router.post(
    "/api/v1/metrics/query",
    response_model=StructuredMetricQueryResponse,
)
async def structured_metric_query(
    payload: StructuredQueryRequest,
    request: Request,
    principal: Principal = Depends(principal_from_request),
) -> StructuredMetricQueryResponse:
    _require_metrics_scope(principal)
    response = await _run_in_thread(request, lambda: _structured_query(request, payload, principal))
    if response.status == AnalysisStatus.FORBIDDEN:
        detail = dict(response.error.detail) if response.error else {}
        if response.error is not None and response.error.code != "forbidden":
            detail["reason"] = response.error.code
        raise HTTPException(
            status_code=403,
            detail={
                "code": "forbidden",
                "message": response.error.message if response.error else "request is not authorized",
                "stage": "authorization",
                "detail": detail,
            },
        )
    request.app.state.result_store.put(
        response.request_id,
        response.model_dump(mode="json"),
        owner=principal.principal_id,
    )
    return response


@router.get(
    "/api/v1/analysis/results/{request_id}",
    responses={404: {"model": ErrorResponse}},
)
def get_analysis_result(
    request_id: str,
    request: Request,
    principal: Principal = Depends(principal_from_request),
) -> dict:
    payload = request.app.state.result_store.get(request_id, owner=principal.principal_id)
    if payload is None:
        raise HTTPException(
            status_code=404,
            detail={
                "code": "result_not_found",
                "message": "no stored result for this request_id, or the result expired",
                "stage": "result_store",
                "request_id": request_id,
            },
        )
    return payload


def _require_agent_scope(principal: Principal) -> None:
    if not principal.has_scope("agent:run"):
        raise HTTPException(
            status_code=403,
            detail={
                "code": "forbidden",
                "message": f"role {principal.role!r} is not allowed to run the agent",
                "stage": "authorization",
                "detail": {"required_scope": "agent:run", "role": principal.role},
            },
        )


def _require_metrics_scope(principal: Principal) -> None:
    """Business data reads require metrics:read, not merely a valid key.

    Approval-only and other non-data roles must not be able to read raw
    metrics through the structured endpoint just because a key authenticated.
    """

    if not principal.has_scope("metrics:read"):
        raise HTTPException(
            status_code=403,
            detail={
                "code": "forbidden",
                "message": f"role {principal.role!r} is not allowed to read business metrics",
                "stage": "authorization",
                "detail": {"required_scope": "metrics:read", "role": principal.role},
            },
        )


def _run_agent(request: Request, payload: AgentRunRequest, principal: Principal):
    settings = request.app.state.settings
    seconds = payload.max_seconds or settings.agent_max_seconds
    seconds = min(
        seconds,
        settings.agent_max_seconds,
        max(0.5, settings.api_timeout_seconds - 1),
    )
    return request.app.state.agent_runtime.run(
        payload.question,
        principal,
        max_tool_calls=payload.max_tool_calls,
        max_seconds=seconds,
        request_id=_request_id(request),
    )


def _idempotency_key(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    if not value:
        return None
    if len(value) > 200:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "invalid_idempotency_key",
                "message": "Idempotency-Key must be 200 characters or fewer",
                "stage": "request",
            },
        )
    return value


def _audit_sync_run(request: Request, response: AgentRunResponse, principal: Principal) -> None:
    store = request.app.state.run_store
    store.append_event(
        event_type="run_finished",
        run_id=response.run_id,
        request_id=response.request_id,
        principal_id=principal.principal_id,
        detail={
            "status": response.status.value,
            "stop_reason": response.stop_reason,
            "tool_calls_used": response.state.tool_calls_used,
            "input_tokens": response.llm_tokens.get("input"),
            "output_tokens": response.llm_tokens.get("output"),
            "latency_ms": response.llm_latency_ms,
        },
    )
    for call in response.tool_calls:
        store.append_event(
            event_type="tool_call",
            run_id=response.run_id,
            request_id=response.request_id,
            principal_id=principal.principal_id,
            tool_call_id=call.call_id,
            detail={
                "tool": call.tool,
                "status": call.status,
                "duration_ms": call.duration_ms,
                "error_code": call.error_code,
                "evidence_ids": call.evidence_ids,
                "arguments": call.arguments,
            },
        )
        if call.status == "permission_denied":
            store.append_event(
                event_type="permission_denied",
                run_id=response.run_id,
                request_id=response.request_id,
                principal_id=principal.principal_id,
                tool_call_id=call.call_id,
                detail={"tool": call.tool, "error_code": call.error_code},
            )


@router.get("/api/v1/me")
def current_principal(principal: Principal = Depends(principal_from_request)) -> dict:
    """Return the authenticated role and scopes for the workbench UI.

    The response never contains the API key or any credential. It is
    intentionally side-effect free so the frontend can decide which controls
    to show without guessing from error responses.
    """

    return {
        "principal_id": principal.principal_id,
        "role": principal.role,
        "authenticated": principal.authenticated,
        "scopes": sorted(principal.scopes),
    }


@router.get("/api/v1/agent/runs")
def list_agent_runs(
    request: Request,
    principal: Principal = Depends(principal_from_request),
    status: str | None = Query(default=None),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> dict:
    """Workbench history, scoped to the caller unless they hold read-any."""

    if not principal.has_scope("run:read"):
        raise HTTPException(
            status_code=403,
            detail={
                "code": "forbidden",
                "message": f"role {principal.role!r} cannot read agent runs",
                "stage": "authorization",
                "detail": {"required_scope": "run:read", "role": principal.role},
            },
        )
    read_any = principal.has_scope("resource:read_any")
    owner = None if read_any else principal.principal_id
    items = request.app.state.run_store.list_runs(
        owner=owner,
        status=status,
        limit=limit + 1,
        offset=offset,
    )
    has_more = len(items) > limit
    items = items[:limit]
    return {
        "items": items,
        "count": len(items),
        "limit": limit,
        "offset": offset,
        "has_more": has_more,
        "scope": "all" if read_any else "self",
    }


@router.post(
    "/api/v1/agent/runs",
    response_model=AgentRunResponse,
)
async def run_agent(
    payload: AgentRunRequest,
    request: Request,
    principal: Principal = Depends(principal_from_request),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> AgentRunResponse:
    _require_agent_scope(principal)
    key = _idempotency_key(idempotency_key)
    store = request.app.state.run_store
    if key is not None:
        existing = store.find_by_idempotency(principal.principal_id, key)
        if existing is not None:
            try:
                return AgentRunResponse.model_validate(existing)
            except ValueError as exc:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "code": "idempotency_conflict",
                        "message": "the idempotency key belongs to an unfinished or incompatible run",
                        "stage": "idempotency",
                    },
                ) from exc
    response = await _run_in_thread(request, lambda: _run_agent(request, payload, principal))
    document = response.model_dump(mode="json")
    store.put(
        response.run_id,
        document,
        owner=principal.principal_id,
        request_id=response.request_id,
        status=response.status.value,
        question=payload.question,
        idempotency_key=key,
    )
    _audit_sync_run(request, response, principal)
    request.app.state.result_store.put(
        response.run_id,
        document,
        owner=principal.principal_id,
    )
    return response


@router.post("/api/v1/agent/runs/async", status_code=202)
async def submit_agent_run(
    payload: AgentRunRequest,
    request: Request,
    response: Response,
    principal: Principal = Depends(principal_from_request),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> dict:
    _require_agent_scope(principal)
    key = _idempotency_key(idempotency_key)
    document, created = request.app.state.run_manager.submit(
        question=payload.question,
        principal=principal,
        request_id=_request_id(request),
        max_tool_calls=payload.max_tool_calls,
        max_seconds=payload.max_seconds,
        idempotency_key=key,
        trace_context=current_otel_context(),
    )
    if not created:
        response.headers["Idempotency-Replayed"] = "true"
    return document


@router.get(
    "/api/v1/agent/runs/{run_id}",
    responses={404: {"model": ErrorResponse}},
)
def get_agent_run(
    run_id: str,
    request: Request,
    principal: Principal = Depends(principal_from_request),
) -> dict:
    _require_resource(request, principal, "run", run_id)
    payload = request.app.state.run_store.get(run_id)
    if payload is None:
        raise _not_found("agent_run", run_id)
    return payload


@router.get(
    "/api/v1/agent/runs/{run_id}/events",
    responses={404: {"model": ErrorResponse}},
)
def get_agent_run_events(
    run_id: str,
    request: Request,
    principal: Principal = Depends(principal_from_request),
) -> dict:
    _require_resource(request, principal, "run", run_id)
    owner = request.app.state.run_store.owner_of(run_id)
    events = request.app.state.run_manager.events(run_id, principal, owner=owner)
    return {"run_id": run_id, "events": events}


@router.post(
    "/api/v1/agent/runs/{run_id}/cancel",
    responses={404: {"model": ErrorResponse}, 409: {"model": ErrorResponse}},
)
def cancel_agent_run(
    run_id: str,
    request: Request,
    principal: Principal = Depends(principal_from_request),
) -> dict:
    _require_agent_scope(principal)
    _require_resource(request, principal, "run", run_id)
    owner = request.app.state.run_store.owner_of(run_id)
    accepted, reason = request.app.state.run_manager.cancel(run_id, principal, owner=owner)
    if reason == "not_found":
        raise HTTPException(
            status_code=404,
            detail={
                "code": "agent_run_not_found",
                "message": "no stored agent run for this run_id",
                "stage": "run_store",
                "request_id": run_id,
            },
        )
    if not accepted:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "cancel_not_possible",
                "message": f"run cannot be cancelled: {reason}",
                "stage": "cancellation",
                "request_id": run_id,
            },
        )
    return {"run_id": run_id, "status": "cancelling"}


@router.post(
    "/api/v1/agent/runs/{run_id}/resume",
    response_model=AgentRunResponse,
    responses={404: {"model": ErrorResponse}, 409: {"model": ErrorResponse}},
)
async def resume_agent_run(
    run_id: str,
    request: Request,
    principal: Principal = Depends(principal_from_request),
    max_seconds: float | None = None,
) -> AgentRunResponse:
    _require_agent_scope(principal)
    _require_resource(request, principal, "run", run_id)
    owner = request.app.state.run_store.owner_of(run_id)
    settings = request.app.state.settings
    seconds = min(
        max_seconds or settings.agent_max_seconds,
        settings.agent_max_seconds,
        max(0.5, settings.api_timeout_seconds - 1),
    )

    def _resume() -> dict:
        return request.app.state.run_manager.resume(
            run_id,
            principal,
            owner=owner,
            request_id=_request_id(request),
            max_seconds=seconds,
            trace_context=current_otel_context(),
        )

    try:
        document = await _run_in_thread(request, _resume)
    except RunResumeError as exc:
        status_code = 404 if exc.code == "agent_run_not_found" else 409
        raise HTTPException(
            status_code=status_code,
            detail={
                "code": exc.code,
                "message": exc.message,
                "stage": "resume",
                "request_id": run_id,
                "detail": getattr(exc, "detail", {}),
            },
        ) from exc
    return AgentRunResponse.model_validate(document)


@router.get(
    "/api/v1/agent/runs/{run_id}/stream",
    responses={404: {"model": ErrorResponse}},
)
async def stream_agent_run(
    run_id: str,
    request: Request,
    principal: Principal = Depends(principal_from_request),
) -> StreamingResponse:
    _require_resource(request, principal, "run", run_id)
    run_store = request.app.state.run_store
    if run_store.get(run_id) is None:
        raise _not_found("agent_run", run_id)

    async def event_stream():
        sent = 0
        last_status: str | None = None
        while True:
            if await request.is_disconnected():
                break
            document = run_store.get(run_id)
            if document is None:
                yield 'event: error\ndata: {"code": "agent_run_not_found"}\n\n'
                break
            events = run_store.list_events(run_id)
            for event in events[sent:]:
                yield (f"event: {event['event_type']}\ndata: {json.dumps(event, ensure_ascii=False, default=str)}\n\n")
            sent = len(events)
            status = str(document.get("status"))
            if status != last_status:
                payload = {
                    "run_id": run_id,
                    "status": status,
                    "stop_reason": document.get("stop_reason"),
                }
                yield f"event: status\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
                last_status = status
            if status in TERMINAL_STATUSES:
                yield "event: done\ndata: {}\n\n"
                break
            await anyio.sleep(0.5)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _phase5(request: Request):
    pipeline = getattr(request.app.state, "phase5_pipeline", None)
    if pipeline is None:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "evidence_store_unavailable",
                "message": "the evidence ledger is disabled because the run store is not persistent",
                "stage": "phase5",
            },
        )
    return pipeline


@router.get(
    "/api/v1/agent/runs/{run_id}/evidence",
    responses={404: {"model": ErrorResponse}, 503: {"model": ErrorResponse}},
)
def list_run_evidence(
    run_id: str,
    request: Request,
    principal: Principal = Depends(principal_from_request),
) -> dict:
    _require_resource(request, principal, "evidence", run_id)
    evidence = _phase5(request).ledger.list(run_id)
    return {
        "run_id": run_id,
        "count": len(evidence),
        "evidence": [item.model_dump(mode="json") for item in evidence],
    }


@router.get(
    "/api/v1/agent/runs/{run_id}/evidence/{evidence_id}",
    responses={404: {"model": ErrorResponse}, 503: {"model": ErrorResponse}},
)
def get_run_evidence(
    run_id: str,
    evidence_id: str,
    request: Request,
    principal: Principal = Depends(principal_from_request),
) -> dict:
    _require_resource(request, principal, "evidence", run_id)
    evidence = _phase5(request).ledger.get(run_id, evidence_id)
    if evidence is None:
        raise HTTPException(
            status_code=404,
            detail={
                "code": "evidence_not_found",
                "message": "no evidence with this id in the run",
                "stage": "evidence_ledger",
                "request_id": run_id,
            },
        )
    return evidence.model_dump(mode="json")


@router.get(
    "/api/v1/agent/runs/{run_id}/findings",
    responses={404: {"model": ErrorResponse}, 503: {"model": ErrorResponse}},
)
def list_run_findings(
    run_id: str,
    request: Request,
    principal: Principal = Depends(principal_from_request),
) -> dict:
    _require_resource(request, principal, "finding", run_id)
    findings = _phase5(request).finding_store.list(run_id)
    return {
        "run_id": run_id,
        "count": len(findings),
        "findings": [item.model_dump(mode="json") for item in findings],
    }


@router.get(
    "/api/v1/agent/runs/{run_id}/report",
    responses={404: {"model": ErrorResponse}, 503: {"model": ErrorResponse}},
)
def get_run_report(
    run_id: str,
    request: Request,
    principal: Principal = Depends(principal_from_request),
    version: int | None = None,
) -> dict:
    _require_resource(request, principal, "report", run_id)
    store = _phase5(request).report_store
    stored = store.get(run_id, version) if version is not None else store.latest(run_id)
    if stored is None:
        raise HTTPException(
            status_code=404,
            detail={
                "code": "report_not_found",
                "message": "no report for this run and version",
                "stage": "report_store",
                "request_id": run_id,
            },
        )
    document = stored.model_dump(mode="json")
    approval = _phase5(request).approval_store.get(stored.report_id, stored.version)
    document["approval"] = approval.model_dump(mode="json") if approval is not None else None
    return document


@router.get(
    "/api/v1/agent/runs/{run_id}/report/versions",
    responses={404: {"model": ErrorResponse}, 503: {"model": ErrorResponse}},
)
def list_run_report_versions(
    run_id: str,
    request: Request,
    principal: Principal = Depends(principal_from_request),
) -> dict:
    _require_resource(request, principal, "report", run_id)
    reports = _phase5(request).report_store.versions(run_id)
    return {
        "run_id": run_id,
        "count": len(reports),
        "versions": [
            {
                "report_id": item.report_id,
                "version": item.version,
                "status": item.status,
                "validation_passed": item.validation.passed,
                "created_at": item.created_at.isoformat(timespec="seconds"),
            }
            for item in reports
        ],
    }


@router.get(
    "/api/v1/agent/runs/{run_id}/report/validation",
    responses={404: {"model": ErrorResponse}, 503: {"model": ErrorResponse}},
)
def get_run_report_validation(
    run_id: str,
    request: Request,
    principal: Principal = Depends(principal_from_request),
    version: int | None = None,
) -> dict:
    _require_resource(request, principal, "report", run_id)
    store = _phase5(request).report_store
    stored = store.get(run_id, version) if version is not None else store.latest(run_id)
    if stored is None:
        raise HTTPException(
            status_code=404,
            detail={
                "code": "report_not_found",
                "message": "no report for this run and version",
                "stage": "report_store",
                "request_id": run_id,
            },
        )
    return {
        "run_id": run_id,
        "report_id": stored.report_id,
        "version": stored.version,
        "status": stored.status,
        "validation": stored.validation.model_dump(mode="json"),
    }


class ApprovalDecisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["approve", "reject", "cancel"]
    # Both fields are mandatory so a decision cannot drift to a newer report
    # version between rendering and submission.
    report_version: int = Field(ge=1)
    report_fingerprint: str = Field(min_length=64, max_length=64)
    comment: str | None = Field(default=None, max_length=2_000)


def _report_version(request: Request, run_id: str, version: int | None):
    pipeline = _phase5(request)
    stored = pipeline.report_store.get(run_id, version) if version is not None else pipeline.report_store.latest(run_id)
    if stored is None:
        raise HTTPException(
            status_code=404,
            detail={
                "code": "report_not_found",
                "message": "no report for this run and version",
                "stage": "report_store",
                "request_id": run_id,
            },
        )
    return stored


def _approval_for(request: Request, stored, *, version: int | None = None):
    record = _phase5(request).approval_store.get(stored.report_id, stored.version)
    if record is None:
        raise HTTPException(
            status_code=404,
            detail={
                "code": "approval_not_found",
                "message": "no approval request exists for this report version",
                "stage": "approval",
                "request_id": stored.run_id,
            },
        )
    return record


def _approval_payload(record, stored, *, duplicate: bool = False) -> dict:
    payload = record.model_dump(mode="json")
    current = compute_report_fingerprint(stored.report)
    payload["current_report_fingerprint"] = current
    payload["stale"] = current != record.report_fingerprint
    payload["duplicate"] = duplicate
    return payload


def _require_approval_decide(principal: Principal) -> None:
    if not principal.has_scope("approval:decide"):
        raise HTTPException(
            status_code=403,
            detail={
                "code": "forbidden",
                "message": f"role {principal.role!r} cannot decide report approvals",
                "stage": "authorization",
                "detail": {"required_scope": "approval:decide", "role": principal.role},
            },
        )


@router.get(
    "/api/v1/agent/runs/{run_id}/report/approval",
    responses={404: {"model": ErrorResponse}, 503: {"model": ErrorResponse}},
)
def get_run_report_approval(
    run_id: str,
    request: Request,
    principal: Principal = Depends(principal_from_request),
    version: int | None = None,
) -> dict:
    _require_resource(request, principal, "report", run_id)
    stored = _report_version(request, run_id, version)
    record = _approval_for(request, stored)
    return _approval_payload(record, stored)


@router.post(
    "/api/v1/agent/runs/{run_id}/report/approval/decision",
    responses={
        403: {"model": ErrorResponse},
        404: {"model": ErrorResponse},
        409: {"model": ErrorResponse},
        503: {"model": ErrorResponse},
    },
)
def decide_run_report_approval(
    run_id: str,
    payload: ApprovalDecisionRequest,
    request: Request,
    principal: Principal = Depends(principal_from_request),
) -> dict:
    _require_resource(request, principal, "report", run_id)
    stored = _report_version(request, run_id, payload.report_version)
    owner = request.app.state.run_store.owner_of(run_id)
    if payload.action == "cancel":
        # The report owner may withdraw their own request. Everyone else needs
        # the explicit approval decision scope.
        if principal.principal_id != owner:
            _require_approval_decide(principal)
    else:
        _require_approval_decide(principal)
        if principal.principal_id == owner:
            raise HTTPException(
                status_code=403,
                detail={
                    "code": "approval_separation_violation",
                    "message": "the report owner cannot approve or reject their own report",
                    "stage": "approval",
                    "detail": {"owner": owner, "actor": principal.principal_id},
                },
            )
    current = compute_report_fingerprint(stored.report)
    if payload.report_fingerprint != current:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "approval_stale",
                "message": "the supplied report fingerprint does not match the stored report",
                "stage": "approval",
                "request_id": run_id,
            },
        )
    record = _approval_for(request, stored)
    try:
        updated, duplicate = request.app.state.phase5_pipeline.approval_store.decide(
            report_id=stored.report_id,
            version=stored.version,
            action=payload.action,
            actor=principal.principal_id,
            current_fingerprint=current,
            comment=payload.comment,
        )
    except ApprovalNotFound as exc:
        raise HTTPException(
            status_code=404,
            detail={
                "code": exc.code,
                "message": str(exc),
                "stage": "approval",
                "request_id": run_id,
            },
        ) from exc
    except ApprovalStale as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "code": exc.code,
                "message": str(exc),
                "stage": "approval",
                "request_id": run_id,
            },
        ) from exc
    except ApprovalSeparationViolation as exc:
        raise HTTPException(
            status_code=403,
            detail={
                "code": exc.code,
                "message": str(exc),
                "stage": "approval",
                "request_id": run_id,
            },
        ) from exc
    except InvalidApprovalTransition as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "code": exc.code,
                "message": str(exc),
                "stage": "approval",
                "request_id": run_id,
            },
        ) from exc
    request.app.state.run_store.append_event(
        event_type=f"approval_{payload.action}",
        run_id=run_id,
        request_id=_request_id(request),
        principal_id=principal.principal_id,
        detail={
            "approval_id": updated.approval_id,
            "report_id": updated.report_id,
            "report_version": updated.report_version,
            "report_fingerprint": updated.report_fingerprint,
            "from_state": record.state,
            "to_state": updated.state,
            "duplicate": duplicate,
        },
    )
    return _approval_payload(updated, stored, duplicate=duplicate)


@router.get(
    "/api/v1/agent/runs/{run_id}/report/approval/events",
    responses={404: {"model": ErrorResponse}, 503: {"model": ErrorResponse}},
)
def get_run_report_approval_events(
    run_id: str,
    request: Request,
    principal: Principal = Depends(principal_from_request),
    version: int | None = None,
) -> dict:
    _require_resource(request, principal, "report", run_id)
    stored = _report_version(request, run_id, version)
    record = _approval_for(request, stored)
    events = _phase5(request).approval_store.events(record.approval_id)
    return {
        "run_id": run_id,
        "approval": record.model_dump(mode="json"),
        "events": [item.model_dump(mode="json") for item in events],
    }


ApprovalStateFilter = Literal["not_required", "pending", "approved", "rejected", "cancelled"]


@router.get(
    "/api/v1/agent/approvals",
    responses={403: {"model": ErrorResponse}, 503: {"model": ErrorResponse}},
)
def list_agent_approvals(
    request: Request,
    principal: Principal = Depends(principal_from_request),
    state: list[ApprovalStateFilter] | None = Query(default=None),
    approver: str | None = None,
    requester: str | None = None,
    created_from: datetime | None = None,
    created_to: datetime | None = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    sort_by: Literal[
        "requested_at",
        "created_at",
        "updated_at",
        "decided_at",
        "state",
        "report_version",
    ] = "requested_at",
    sort_dir: Literal["asc", "desc"] = "desc",
) -> dict:
    """Cross-run approval workbench.

    ``approval:read`` is required. Only holders of ``approval:read_any``
    (admin / business manager by default) see every approval; other approvers
    see only approvals they requested or decided.
    """

    if not principal.has_scope("approval:read"):
        raise HTTPException(
            status_code=403,
            detail={
                "code": "forbidden",
                "message": f"role {principal.role!r} cannot read approvals",
                "stage": "authorization",
                "detail": {"required_scope": "approval:read", "role": principal.role},
            },
        )

    def _iso(value: datetime | None) -> str | None:
        if value is None:
            return None
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.isoformat(timespec="microseconds")

    result = _phase5(request).approval_store.list_approvals(
        states=list(state) if state else None,
        approver=approver,
        requester=requester,
        created_from=_iso(created_from),
        created_to=_iso(created_to),
        visible_to=principal.principal_id,
        read_any=principal.has_scope("approval:read_any"),
        page=page,
        page_size=page_size,
        sort_by=sort_by,
        sort_dir=sort_dir,
    )
    now = datetime.now(timezone.utc)
    items: list[dict] = []
    for record in result["items"]:
        payload = record.model_dump(mode="json")
        try:
            requested = datetime.fromisoformat(record.requested_at)
            if requested.tzinfo is None:
                requested = requested.replace(tzinfo=timezone.utc)
            decided = record.decided_at or now.isoformat(timespec="microseconds")
            end = datetime.fromisoformat(decided)
            if end.tzinfo is None:
                end = end.replace(tzinfo=timezone.utc)
            payload["wait_seconds"] = round(max(0.0, (end - requested).total_seconds()), 3)
        except ValueError:
            payload["wait_seconds"] = None
        items.append(payload)
    return {
        "items": items,
        "total": result["total"],
        "page": result["page"],
        "page_size": result["page_size"],
        "pages": result["pages"],
        "sort_by": result["sort_by"],
        "sort_dir": result["sort_dir"],
        "scope": "all" if principal.has_scope("approval:read_any") else "self",
    }


@router.post(
    "/mcp",
    responses={
        400: {"description": "JSON-RPC parse error"},
        401: {"description": "Missing or invalid API key"},
        404: {"description": "MCP transport disabled"},
    },
)
async def mcp_endpoint(request: Request):
    """MCP JSON-RPC 2.0 transport for the existing read-only tools.

    This endpoint is a transport only. Authentication still resolves an API
    key to a Principal, and ``McpToolServer`` calls
    ``ToolRegistry.execute`` so role scopes, metric/region policy,
    AnalysisGateway, SemanticQueryGuard and SafeQueryExecutor are unchanged.
    """

    settings = request.app.state.settings
    if not settings.mcp_enabled:
        raise HTTPException(
            status_code=404,
            detail={
                "code": "mcp_disabled",
                "message": "MCP transport is disabled; set EBA_MCP_ENABLED=true to expose it",
                "stage": "request",
                "detail": {},
            },
        )
    try:
        principal = principal_from_request(request)
    except HTTPException:
        record_security_event(
            getattr(request.app.state, "run_store", None),
            "mcp_auth_failed",
            request_id=_request_id(request),
            principal_id=None,
            detail={"reason": "invalid_or_missing_api_key"},
            logger=getattr(request.app.state, "logger", None),
        )
        raise
    if settings.mcp_require_api_key and not principal.authenticated:
        record_security_event(
            getattr(request.app.state, "run_store", None),
            "mcp_auth_failed",
            request_id=_request_id(request),
            principal_id=principal.principal_id,
            detail={"reason": "anonymous_access_disabled", "role": principal.role},
            logger=getattr(request.app.state, "logger", None),
        )
        raise HTTPException(
            status_code=401,
            detail={
                "code": "unauthorized",
                "message": "MCP requires a valid X-API-Key header",
                "stage": "authentication",
                "detail": {},
            },
            headers={"WWW-Authenticate": "X-API-Key"},
        )

    try:
        message = await request.json()
    except Exception:
        return JSONResponse(
            status_code=400,
            content={
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": -32700, "message": "Parse error"},
            },
            headers={"Mcp-Protocol-Version": MCP_PROTOCOL_VERSIONS[0]},
        )

    server = request.app.state.mcp_server
    status, body = await _run_in_thread(
        request,
        lambda: server.handle(
            message,
            principal,
            request_id=_request_id(request),
        ),
    )
    headers = {"Mcp-Protocol-Version": MCP_PROTOCOL_VERSIONS[0]}
    if body is None:
        return Response(status_code=status, headers=headers)
    return JSONResponse(status_code=status, content=body, headers=headers)


@router.get(
    "/api/v1/security/audit",
    responses={403: {"model": ErrorResponse}, 422: {"model": ErrorResponse}},
)
def list_security_audit(
    request: Request,
    principal: Principal = Depends(principal_from_request),
    event_type: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=1000),
) -> dict:
    """Admin-only view of authentication, authorization and MCP audit events.

    These events are deliberately run-independent; run-scoped tool and
    approval events remain available through the per-run event endpoint.
    """

    if not principal.has_scope("admin"):
        raise HTTPException(
            status_code=403,
            detail={
                "code": "forbidden",
                "message": f"role {principal.role!r} cannot read security audit events",
                "stage": "authorization",
                "detail": {"required_scope": "admin", "role": principal.role},
            },
        )
    if event_type is not None and event_type not in SECURITY_EVENT_TYPES:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "invalid_event_type",
                "message": f"unknown security event type: {event_type}",
                "stage": "request",
                "detail": {"allowed": sorted(SECURITY_EVENT_TYPES)},
            },
        )
    events = request.app.state.run_store.list_security_events(
        event_type=event_type,
        limit=limit,
    )
    return {
        "count": len(events),
        "events": events,
        "scope": "run_independent_security_events",
    }


@router.get(
    "/api/v1/metrics",
    responses={403: {"model": ErrorResponse}, 503: {"model": ErrorResponse}},
)
def get_operational_metrics(
    request: Request,
    principal: Principal = Depends(principal_from_request),
) -> dict:
    """Operational metrics from the shared run store.

    Aggregated run data is administrative; only the ``admin`` scope can read
    it. The ``observability`` block reports the live tracing status of this
    process, including how many spans were actually exported, so the payload
    never implies that a trace left the process when it did not.
    """

    if not principal.has_scope("admin"):
        raise HTTPException(
            status_code=403,
            detail={
                "code": "forbidden",
                "message": f"role {principal.role!r} cannot read operational metrics",
                "stage": "authorization",
                "detail": {"required_scope": "admin", "role": principal.role},
            },
        )
    return metrics_snapshot(request.app.state.run_store, request.app.state.settings)


@router.get(
    "/api/v1/alerts",
    responses={403: {"model": ErrorResponse}, 503: {"model": ErrorResponse}},
)
def get_operational_alerts(
    request: Request,
    principal: Principal = Depends(principal_from_request),
) -> dict:
    """Evaluate operational alert rules over the shared run store.

    This is a polling interface for an external monitor. No notification is
    sent by this process, and the response never contains credentials or raw
    request payloads.
    """

    if not principal.has_scope("admin"):
        raise HTTPException(
            status_code=403,
            detail={
                "code": "forbidden",
                "message": f"role {principal.role!r} cannot read operational alerts",
                "stage": "authorization",
                "detail": {"required_scope": "admin", "role": principal.role},
            },
        )
    metrics = metrics_snapshot(request.app.state.run_store, request.app.state.settings)
    return evaluate_alerts(metrics, request.app.state.settings)

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy.engine import Engine

from .. import __version__
from ..agent import AgentRuntime, AnalysisGateway, ToolRegistry
from ..agent.checkpoint import SqlAlchemyCheckpointSaver
from ..config import Settings, get_settings
from ..db import make_readonly_engine
from ..evidence import Phase5Pipeline
from ..llm import LLMClient, build_llm_client
from ..mcp import McpToolServer
from ..nl.contracts import ErrorInfo
from ..observability.setup import configure_tracing, shutdown_tracing, tracing_status
from ..security import AuthorizationPolicy
from .middleware import RequestContextMiddleware, configure_logging
from .routes import router
from .run_manager import RunManager
from .run_store import PersistentRunStore
from .store import ResultStore


def _error_response(
    request: Request,
    status_code: int,
    code: str,
    message: str,
    stage: str,
    detail: dict | None = None,
) -> JSONResponse:
    request_id = getattr(request.state, "request_id", "")
    payload = ErrorInfo(
        code=code,
        message=message,
        stage=stage,
        detail=detail or {},
    )
    return JSONResponse(
        status_code=status_code,
        content={
            "request_id": request_id,
            "status": "error",
            "error": payload.model_dump(mode="json"),
        },
    )


_STATUS_BY_HTTP = {
    404: "not_found",
    422: "invalid_request",
    504: "timeout",
}


def create_app(
    settings: Settings | None = None,
    *,
    session_factory=None,
    engine: Engine | None = None,
    llm_client: LLMClient | None = None,
    result_store: ResultStore | None = None,
    run_store: PersistentRunStore | None = None,
    agent_runtime: AgentRuntime | None = None,
) -> FastAPI:
    settings = settings or get_settings()
    logger = configure_logging(settings.log_level)
    tracing = configure_tracing(settings)

    owns_engine = False
    if session_factory is None:
        if engine is None:
            engine = make_readonly_engine(
                settings.application_database_url,
                settings.query_timeout_ms,
            )
            owns_engine = True
        from sqlalchemy.orm import Session, sessionmaker

        session_factory = sessionmaker(
            bind=engine,
            class_=Session,
            expire_on_commit=False,
        )
    elif engine is None:
        engine = getattr(session_factory, "kw", {}).get("bind")

    if llm_client is None:
        llm_client = build_llm_client(settings)
    store = result_store or ResultStore()
    owns_run_store = False
    if run_store is None:
        run_store = PersistentRunStore(settings.run_store_url or "sqlite://")
        owns_run_store = True
    dialect = engine.dialect.name if engine is not None else "unknown"
    authorization = AuthorizationPolicy(settings)
    pipeline = None
    if settings.phase5_enabled and run_store is not None:
        pipeline = Phase5Pipeline(
            run_store.engine,
            llm=llm_client,
            narrative_enabled=settings.phase5_narrative_enabled,
            max_fix_attempts=settings.phase5_max_fix_attempts,
            narrative_max_tokens=settings.phase5_narrative_max_tokens,
        )
    if agent_runtime is None:
        registry = ToolRegistry(
            AnalysisGateway(session_factory, settings),
            authorization,
            settings,
        )
        checkpointer = SqlAlchemyCheckpointSaver(run_store.engine)
        agent_runtime = AgentRuntime(
            llm_client=llm_client,
            registry=registry,
            settings=settings,
            pipeline=pipeline,
            checkpointer=checkpointer,
            tool_cache=run_store,
        )
    run_manager = RunManager(
        runtime=agent_runtime,
        store=run_store,
        settings=settings,
        logger=logger,
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        interrupted = run_store.mark_interrupted()
        cleanup = run_store.cleanup_runs(
            dry_run=settings.run_cleanup_dry_run,
            retention_seconds={
                "completed": settings.run_retention_completed_seconds,
                "failed": settings.run_retention_failed_seconds,
                "cancelled": settings.run_retention_cancelled_seconds,
                "unfinished": settings.run_retention_unfinished_seconds,
            },
            batch_limit=settings.run_cleanup_batch_limit,
        )
        logger.info(
            "api_started",
            extra={
                "fields": {
                    "dialect": dialect,
                    "readonly": True,
                    "llm_configured": llm_client is not None,
                    "agent_available": llm_client is not None,
                    "run_store": run_store.engine.url.get_backend_name(),
                    "worker_id": run_manager.worker_id,
                    "interrupted_runs": interrupted,
                    "cleanup": {
                        "dry_run": cleanup["dry_run"],
                        "run_count": cleanup["run_count"],
                        "status": cleanup["status"],
                    },
                    "otel_enabled": settings.otel_enabled,
                    "otel_initialized": tracing.initialized,
                    "otel_exporter": tracing.exporter_kind,
                    "otel_error": tracing.init_error,
                }
            },
        )
        try:
            yield
        finally:
            run_manager.shutdown()
            if owns_engine and engine is not None:
                engine.dispose()
            if owns_run_store:
                run_store.close()
            # Flush pending spans before the process exits; a lost batch is a
            # lost trace, and shutdown is the only reliable flush point here.
            shutdown_tracing()
            logger.info("api_stopped")

    app = FastAPI(
        title="Enterprise Business Agent API",
        version=__version__,
        description=(
            "Semantic-first business analysis with guarded, read-only SQL fallback. "
            "Phase 3 API: health, natural-language analysis, structured metric query, "
            "and result retrieval."
        ),
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.session_factory = session_factory
    app.state.engine = engine
    app.state.llm_client = llm_client
    app.state.result_store = store
    app.state.run_store = run_store
    app.state.run_manager = run_manager
    app.state.authorization = authorization
    app.state.metric_resolver = getattr(
        getattr(getattr(agent_runtime, "registry", None), "gateway", None),
        "metric_resolver",
        None,
    )
    app.state.agent_runtime = agent_runtime
    app.state.mcp_server = McpToolServer(
        registry=getattr(agent_runtime, "registry", None),
        settings=settings,
        run_store=run_store,
        logger=logger,
    )
    app.state.phase5_pipeline = pipeline
    app.state.dialect = dialect
    app.state.logger = logger
    app.state.tracing = tracing_status()

    app.add_middleware(RequestContextMiddleware)
    app.include_router(router)

    web_dir = Path(__file__).resolve().parent.parent / "web"
    app.mount("/workbench", StaticFiles(directory=web_dir, html=True), name="workbench")

    @app.get("/", include_in_schema=False)
    def workbench_redirect() -> RedirectResponse:
        return RedirectResponse(url="/workbench/")

    @app.exception_handler(HTTPException)
    async def http_exception_handler(request: Request, exc: HTTPException):
        detail = exc.detail
        if isinstance(detail, dict) and "code" in detail:
            return _error_response(
                request,
                exc.status_code,
                str(detail.get("code")),
                str(detail.get("message") or ""),
                str(detail.get("stage") or "api"),
                detail.get("detail") if isinstance(detail.get("detail"), dict) else None,
            )
        return _error_response(
            request,
            exc.status_code,
            _STATUS_BY_HTTP.get(exc.status_code, "http_error"),
            str(detail),
            "api",
        )

    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(request: Request, exc: RequestValidationError):
        errors = [
            {
                "loc": [str(part) for part in error.get("loc", [])],
                "msg": error.get("msg", "invalid value"),
                "type": error.get("type", "value_error"),
            }
            for error in exc.errors()
        ]
        return _error_response(
            request,
            422,
            "invalid_request",
            "request validation failed",
            "validation",
            {"errors": errors},
        )

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(request: Request, exc: Exception):
        logger.error(
            "unhandled_api_error",
            extra={
                "fields": {
                    "request_id": getattr(request.state, "request_id", ""),
                    "method": request.method,
                    "path": request.url.path,
                    "error_type": type(exc).__name__,
                }
            },
        )
        return _error_response(
            request,
            500,
            "execution_error",
            "internal server error",
            "api",
        )

    return app


app = create_app()


def main() -> None:  # pragma: no cover - convenience entry point
    import uvicorn

    settings = get_settings()
    uvicorn.run(
        "enterprise_business_agent.api.app:app",
        host="127.0.0.1",
        port=8000,
        log_level=settings.log_level.lower(),
    )


if __name__ == "__main__":  # pragma: no cover
    main()

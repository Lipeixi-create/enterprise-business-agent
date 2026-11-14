from __future__ import annotations

from collections.abc import Generator

from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker


class Base(DeclarativeBase):
    pass


def _is_sqlite(database_url: str) -> bool:
    return make_url(database_url).get_backend_name() == "sqlite"


def _sqlite_readonly_url(database_url: str) -> str:
    url = make_url(database_url)
    database = url.database
    if not database or database == ":memory:":
        return database_url
    if database.startswith("file:"):
        return database_url
    return f"sqlite+pysqlite:///file:{database}?mode=ro&uri=true"


def _attach_sqlite_guards(engine: Engine, *, read_only: bool) -> None:
    @event.listens_for(engine, "connect")
    def _enable_sqlite_guards(dbapi_connection, _connection_record):
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA foreign_keys=ON")
            if read_only:
                cursor.execute("PRAGMA query_only=ON")
        finally:
            cursor.close()


def make_engine(database_url: str) -> Engine:
    engine = create_engine(database_url, future=True, pool_pre_ping=True)
    if _is_sqlite(database_url):
        _attach_sqlite_guards(engine, read_only=False)
    return engine


def make_readonly_engine(database_url: str, timeout_ms: int = 15_000) -> Engine:
    """Build a connection that cannot write even if Guard is bypassed.

    SQLite opens the file with ``mode=ro`` and also sets ``PRAGMA query_only``.
    PostgreSQL connections set ``default_transaction_read_only`` and statement
    and lock timeouts at session level. The independent read-only role created
    by ``scripts/create_readonly_role.py`` is the primary database-layer control;
    these settings are the second database-layer control.
    """
    if _is_sqlite(database_url):
        engine = create_engine(
            _sqlite_readonly_url(database_url),
            future=True,
            connect_args={"timeout": max(1, timeout_ms // 1000)},
        )
        _attach_sqlite_guards(engine, read_only=True)
        return engine

    options = " ".join(
        [
            "-c default_transaction_read_only=on",
            f"-c statement_timeout={int(timeout_ms)}",
            "-c lock_timeout=3000",
        ]
    )
    engine = create_engine(
        database_url,
        future=True,
        pool_pre_ping=True,
        connect_args={"options": options},
    )

    @event.listens_for(engine, "connect")
    def _enforce_readonly_session(dbapi_connection, _connection_record):
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY")
        finally:
            cursor.close()

    return engine


def make_session_factory(database_url: str):
    return sessionmaker(bind=make_engine(database_url), class_=Session, expire_on_commit=False)


def make_readonly_session_factory(database_url: str, timeout_ms: int = 15_000):
    return sessionmaker(
        bind=make_readonly_engine(database_url, timeout_ms),
        class_=Session,
        expire_on_commit=False,
    )


def session_scope(database_url: str) -> Generator[Session, None, None]:
    factory = make_session_factory(database_url)
    with factory() as session:
        yield session


def check_readonly_connection(engine: Engine) -> None:
    with engine.connect() as connection:
        connection.execute(text("SELECT 1")).scalar_one()

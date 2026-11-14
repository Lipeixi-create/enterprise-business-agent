from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Iterator

from langgraph.checkpoint.base import (
    BaseCheckpointSaver,
    Checkpoint,
    CheckpointMetadata,
    CheckpointTuple,
    PendingWrite,
    get_checkpoint_id,
    get_checkpoint_metadata,
)
from langgraph.checkpoint.serde.base import SerializerProtocol
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from sqlalchemy import (
    Column,
    Index,
    Integer,
    LargeBinary,
    MetaData,
    String,
    Table,
    create_engine,
    delete,
    inspect,
    select,
)
from sqlalchemy.engine import Engine


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


CHECKPOINT_METADATA = MetaData()

_CHECKPOINTS = Table(
    "agent_checkpoints",
    CHECKPOINT_METADATA,
    Column("thread_id", String(128), primary_key=True),
    Column("checkpoint_ns", String(256), primary_key=True, default=""),
    Column("checkpoint_id", String(64), primary_key=True),
    Column("parent_checkpoint_id", String(64), nullable=True),
    Column("checkpoint_type", String(16), nullable=False),
    Column("checkpoint_value", LargeBinary, nullable=False),
    Column("metadata_type", String(16), nullable=False),
    Column("metadata_value", LargeBinary, nullable=False),
    Column("created_at", String(40), nullable=False),
    Index("ix_agent_checkpoints_thread_created", "thread_id", "checkpoint_ns", "created_at"),
)

_WRITES = Table(
    "agent_checkpoint_writes",
    CHECKPOINT_METADATA,
    Column("thread_id", String(128), primary_key=True),
    Column("checkpoint_ns", String(256), primary_key=True, default=""),
    Column("checkpoint_id", String(64), primary_key=True),
    Column("task_id", String(128), primary_key=True),
    Column("idx", Integer, primary_key=True),
    Column("channel", String(128), nullable=False),
    Column("value_type", String(16), nullable=False),
    Column("value", LargeBinary, nullable=False),
    Column("task_path", String(512), nullable=False, default=""),
    Column("created_at", String(40), nullable=False),
    Index("ix_agent_checkpoint_writes_task", "thread_id", "checkpoint_ns", "checkpoint_id"),
)


def create_checkpoint_schema(engine: Engine) -> None:
    CHECKPOINT_METADATA.create_all(engine, checkfirst=True)


def drop_checkpoint_schema(engine: Engine) -> None:
    CHECKPOINT_METADATA.drop_all(engine, checkfirst=True)


def _serde() -> SerializerProtocol:
    # The agent state intentionally contains Pydantic value objects. Listing
    # them explicitly avoids relying on the serializer's deprecated implicit
    # deserialization path.
    return JsonPlusSerializer(
        allowed_msgpack_modules=[
            ("enterprise_business_agent.agent.contracts", "AgentStep"),
            ("enterprise_business_agent.agent.contracts", "ToolCallRecord"),
            ("enterprise_business_agent.agent.contracts", "EvidenceDraft"),
        ]
    )


class SqlAlchemyCheckpointSaver(BaseCheckpointSaver):
    """LangGraph checkpoints in the application's SQLAlchemy database.

    The saver stores the checkpoint graph in ``agent_checkpoints`` and pending
    task writes in ``agent_checkpoint_writes``. It deliberately does not depend
    on ``psycopg_pool`` or the provider-specific LangGraph checkpoint packages,
    so the same implementation works with both the SQLite development store
    and the PostgreSQL runtime role.

    DDL is never issued for PostgreSQL at runtime. A setup role must run
    :func:`create_checkpoint_schema` first; the runtime role only needs
    SELECT/INSERT/UPDATE/DELETE on these two tables.
    """

    def __init__(
        self,
        engine: Engine | str,
        *,
        auto_create: bool | None = None,
        serde: SerializerProtocol | None = None,
    ):
        super().__init__(serde=serde or _serde())
        if isinstance(engine, str):
            engine = create_engine(engine, future=True, pool_pre_ping=True)
        self.engine = engine
        if auto_create is None:
            auto_create = self.engine.url.get_backend_name() == "sqlite"
        if auto_create:
            create_checkpoint_schema(self.engine)
        elif not inspect(self.engine).has_table("agent_checkpoints"):
            raise RuntimeError(
                "checkpoint schema is missing; run scripts/init_runtime_store.py "
                "with the setup role before starting the API with the runtime role"
            )

    # -- configuration helpers -----------------------------------------
    @staticmethod
    def _thread(config: dict[str, Any]) -> tuple[str, str]:
        configurable = config.get("configurable") or {}
        thread_id = configurable.get("thread_id")
        if not thread_id:
            raise ValueError("checkpoint config requires configurable.thread_id")
        return str(thread_id), str(configurable.get("checkpoint_ns") or "")

    @staticmethod
    def _checkpoint_id(config: dict[str, Any]) -> str | None:
        return get_checkpoint_id(config)

    def _config_for(
        self, thread_id: str, checkpoint_ns: str, checkpoint_id: str
    ) -> dict[str, Any]:
        return {
            "configurable": {
                "thread_id": thread_id,
                "checkpoint_ns": checkpoint_ns,
                "checkpoint_id": checkpoint_id,
            }
        }

    # -- BaseCheckpointSaver -------------------------------------------
    def put(
        self,
        config: dict[str, Any],
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: dict[str, Any],
    ) -> dict[str, Any]:
        thread_id, checkpoint_ns = self._thread(config)
        checkpoint_id = str(checkpoint["id"])
        parent_checkpoint_id = self._checkpoint_id(config)
        checkpoint_type, checkpoint_value = self.serde.dumps_typed(checkpoint)
        merged_metadata = get_checkpoint_metadata(config, metadata)
        metadata_type, metadata_value = self.serde.dumps_typed(merged_metadata)
        with self.engine.begin() as connection:
            existing = connection.execute(
                select(_CHECKPOINTS.c.checkpoint_id).where(
                    _CHECKPOINTS.c.thread_id == thread_id,
                    _CHECKPOINTS.c.checkpoint_ns == checkpoint_ns,
                    _CHECKPOINTS.c.checkpoint_id == checkpoint_id,
                )
            ).first()
            values = {
                "parent_checkpoint_id": parent_checkpoint_id,
                "checkpoint_type": checkpoint_type,
                "checkpoint_value": checkpoint_value,
                "metadata_type": metadata_type,
                "metadata_value": metadata_value,
                "created_at": _now(),
            }
            if existing is None:
                connection.execute(
                    _CHECKPOINTS.insert().values(
                        thread_id=thread_id,
                        checkpoint_ns=checkpoint_ns,
                        checkpoint_id=checkpoint_id,
                        **values,
                    )
                )
            else:
                connection.execute(
                    _CHECKPOINTS.update()
                    .where(
                        _CHECKPOINTS.c.thread_id == thread_id,
                        _CHECKPOINTS.c.checkpoint_ns == checkpoint_ns,
                        _CHECKPOINTS.c.checkpoint_id == checkpoint_id,
                    )
                    .values(**values)
                )
        return self._config_for(thread_id, checkpoint_ns, checkpoint_id)

    def put_writes(
        self,
        config: dict[str, Any],
        writes: list[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        thread_id, checkpoint_ns = self._thread(config)
        checkpoint_id = self._checkpoint_id(config)
        if not checkpoint_id:
            raise ValueError("put_writes requires configurable.checkpoint_id")
        rows = []
        for index, (channel, value) in enumerate(writes):
            value_type, value_bytes = self.serde.dumps_typed(value)
            rows.append(
                {
                    "thread_id": thread_id,
                    "checkpoint_ns": checkpoint_ns,
                    "checkpoint_id": checkpoint_id,
                    "task_id": task_id,
                    "idx": index,
                    "channel": channel,
                    "value_type": value_type,
                    "value": value_bytes,
                    "task_path": task_path,
                    "created_at": _now(),
                }
            )
        with self.engine.begin() as connection:
            for row in rows:
                connection.execute(
                    delete(_WRITES).where(
                        _WRITES.c.thread_id == row["thread_id"],
                        _WRITES.c.checkpoint_ns == row["checkpoint_ns"],
                        _WRITES.c.checkpoint_id == row["checkpoint_id"],
                        _WRITES.c.task_id == row["task_id"],
                        _WRITES.c.idx == row["idx"],
                    )
                )
                connection.execute(_WRITES.insert().values(**row))

    def get_tuple(self, config: dict[str, Any]) -> CheckpointTuple | None:
        thread_id, checkpoint_ns = self._thread(config)
        checkpoint_id = self._checkpoint_id(config)
        query = select(_CHECKPOINTS).where(
            _CHECKPOINTS.c.thread_id == thread_id,
            _CHECKPOINTS.c.checkpoint_ns == checkpoint_ns,
        )
        if checkpoint_id:
            query = query.where(_CHECKPOINTS.c.checkpoint_id == checkpoint_id)
        else:
            query = query.order_by(
                _CHECKPOINTS.c.created_at.desc(),
                _CHECKPOINTS.c.checkpoint_id.desc(),
            ).limit(1)
        with self.engine.connect() as connection:
            row = connection.execute(query).first()
            if row is None:
                return None
            writes = connection.execute(
                select(_WRITES)
                .where(
                    _WRITES.c.thread_id == thread_id,
                    _WRITES.c.checkpoint_ns == checkpoint_ns,
                    _WRITES.c.checkpoint_id == row.checkpoint_id,
                )
                .order_by(
                    _WRITES.c.task_path,
                    _WRITES.c.task_id,
                    _WRITES.c.idx,
                )
            ).all()
        checkpoint: Checkpoint = self.serde.loads_typed(
            (row.checkpoint_type, row.checkpoint_value)
        )
        metadata: CheckpointMetadata = self.serde.loads_typed(
            (row.metadata_type, row.metadata_value)
        )
        pending_writes: list[PendingWrite] = [
            (item.task_id, item.channel, self.serde.loads_typed((item.value_type, item.value)))
            for item in writes
        ]
        return CheckpointTuple(
            config=self._config_for(thread_id, checkpoint_ns, row.checkpoint_id),
            checkpoint=checkpoint,
            metadata=metadata,
            parent_config=(
                self._config_for(thread_id, checkpoint_ns, row.parent_checkpoint_id)
                if row.parent_checkpoint_id
                else None
            ),
            pending_writes=pending_writes,
        )

    def list(
        self,
        config: dict[str, Any] | None,
        *,
        filter: dict[str, Any] | None = None,
        before: dict[str, Any] | None = None,
        limit: int | None = None,
    ) -> Iterator[CheckpointTuple]:
        if config is None:
            raise ValueError("checkpoint list requires a config with thread_id")
        thread_id, checkpoint_ns = self._thread(config)
        query = select(_CHECKPOINTS).where(
            _CHECKPOINTS.c.thread_id == thread_id,
            _CHECKPOINTS.c.checkpoint_ns == checkpoint_ns,
        )
        before_id = self._checkpoint_id(before) if before else None
        if before_id:
            query = query.where(_CHECKPOINTS.c.checkpoint_id < before_id)
        query = query.order_by(
            _CHECKPOINTS.c.created_at.desc(),
            _CHECKPOINTS.c.checkpoint_id.desc(),
        )
        with self.engine.connect() as connection:
            rows = connection.execute(query).all()
        emitted = 0
        for row in rows:
            metadata = self.serde.loads_typed((row.metadata_type, row.metadata_value))
            if filter and any(metadata.get(key) != value for key, value in filter.items()):
                continue
            item = self.get_tuple(
                self._config_for(thread_id, checkpoint_ns, row.checkpoint_id)
            )
            if item is None:
                continue
            yield item
            emitted += 1
            if limit is not None and emitted >= limit:
                break

    def delete_thread(self, thread_id: str) -> None:
        with self.engine.begin() as connection:
            connection.execute(
                delete(_WRITES).where(_WRITES.c.thread_id == str(thread_id))
            )
            connection.execute(
                delete(_CHECKPOINTS).where(_CHECKPOINTS.c.thread_id == str(thread_id))
            )

    def latest_checkpoint_id(self, thread_id: str) -> str | None:
        with self.engine.connect() as connection:
            row = connection.execute(
                select(_CHECKPOINTS.c.checkpoint_id)
                .where(_CHECKPOINTS.c.thread_id == str(thread_id))
                .order_by(
                    _CHECKPOINTS.c.created_at.desc(),
                    _CHECKPOINTS.c.checkpoint_id.desc(),
                )
                .limit(1)
            ).first()
        return None if row is None else row.checkpoint_id


__all__ = [
    "CHECKPOINT_METADATA",
    "SqlAlchemyCheckpointSaver",
    "create_checkpoint_schema",
    "drop_checkpoint_schema",
]

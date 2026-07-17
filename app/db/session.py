"""SQLAlchemy engine and session factory."""

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import get_settings


def get_engine():
    settings = get_settings()
    return create_engine(
        settings.database_url,
        pool_pre_ping=True,
        echo=settings.app_env == "development",
        # 2026-07-13: TCP keepalives + statement timeout.
        #
        # Neon serverless can suspend compute mid-query, leaving a
        # half-open socket that never errors — the client blocks forever
        # (observed: rescore-hs-nodes silent hang; the scorer's
        # connection-drop recovery only fires on RAISED exceptions, so a
        # hang is invisible to it).  These libpq params convert a dead
        # connection into an OperationalError within ~1 minute, which the
        # batch-commit recovery path in hs_node_scorer.py already handles
        # (rollback, drop in-flight batch, continue).
        #
        #   connect_timeout      fail connection attempts after 10s
        #   keepalives_*         probe idle sockets: after 30s idle, every
        #                        10s, 3 failures -> error (~60s to detect)
        #
        # NOTE (2026-07-13): no statement_timeout here — Neon's POOLER
        # endpoint (pgbouncer) rejects startup `options` ("unsupported
        # startup parameter"), and session-level SET does not stick under
        # transaction pooling.  If a runaway-query bound is ever needed,
        # run that job against the UNPOOLED endpoint instead.
        connect_args={
            "connect_timeout": 10,
            "keepalives": 1,
            "keepalives_idle": 30,
            "keepalives_interval": 10,
            "keepalives_count": 3,
        },
    )


def get_session_factory():
    return sessionmaker(bind=get_engine(), autocommit=False, autoflush=False, class_=Session)

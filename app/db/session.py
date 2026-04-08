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
    )


def get_session_factory():
    return sessionmaker(bind=get_engine(), autocommit=False, autoflush=False, class_=Session)

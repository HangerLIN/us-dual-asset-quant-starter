from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from platform_core.core import get_settings


def get_engine(database_url: str | None = None):
    return create_engine(database_url or get_settings().database_url, future=True)


def get_session_factory(database_url: str | None = None):
    return sessionmaker(bind=get_engine(database_url), future=True)

from __future__ import annotations

import hashlib
from collections.abc import Iterable
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import (
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    and_,
    create_engine,
    or_,
    select,
)
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker


class Base(DeclarativeBase):
    pass


def utcnow() -> datetime:
    return datetime.utcnow()


class CityBinding(Base):
    __tablename__ = "city_bindings"
    __table_args__ = (UniqueConstraint("channel_id", name="uq_city_bindings_channel_id"),)

    id: int = Column(Integer, primary_key=True)
    guild_id: str = Column(String(32), nullable=False)
    channel_id: str = Column(String(32), nullable=False)
    owner_user_id: str = Column(String(32), nullable=False)
    created_at: datetime = Column(DateTime, default=utcnow, nullable=False)
    updated_at: datetime = Column(
        DateTime,
        default=utcnow,
        onupdate=utcnow,
        nullable=False,
    )


class OAuthState(Base):
    __tablename__ = "oauth_states"
    __table_args__ = (Index("ix_oauth_states_state", "state", unique=True),)

    id: int = Column(Integer, primary_key=True)
    state: str = Column(String(128), nullable=False, unique=True)
    redirect_uri: str = Column(String(255), nullable=False)
    created_at: datetime = Column(DateTime, default=utcnow, nullable=False)
    expires_at: datetime = Column(DateTime, nullable=False)
    consumed_at: datetime | None = Column(DateTime, nullable=True)

    def is_valid(self, now: datetime) -> bool:
        return self.consumed_at is None and self.expires_at >= now


class LocalAppSession(Base):
    __tablename__ = "local_app_sessions"
    __table_args__ = (
        UniqueConstraint("discord_user_id", name="uq_sessions_discord_user_id"),
        Index("ix_local_app_sessions_token_hash", "token_hash", unique=True),
    )

    id: int = Column(Integer, primary_key=True)
    discord_user_id: str = Column(String(32), nullable=False)
    token_hash: str = Column(String(128), nullable=False)
    label: str | None = Column(String(64), nullable=True)
    created_at: datetime = Column(DateTime, default=utcnow, nullable=False)
    last_seen_at: datetime | None = Column(DateTime, nullable=True)
    expires_at: datetime = Column(DateTime, nullable=False)
    revoked_at: datetime | None = Column(DateTime, nullable=True)
    created_by_state_id: int | None = Column(Integer, ForeignKey("oauth_states.id"), nullable=True)


def hash_token(raw_token: str) -> str:
    """Return a deterministic hash for secure token storage."""

    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


@dataclass(slots=True)
class OAuthStateRecord:
    id: int
    state: str
    redirect_uri: str
    created_at: datetime
    expires_at: datetime


@dataclass(slots=True)
class AuthenticatedSession:
    """Runtime metadata for a connected local application."""

    discord_user_id: str
    session_id: int
    label: str | None
    expires_at: datetime

    def is_active(self, now: datetime | None = None) -> bool:
        now = now or utcnow()
        return self.expires_at >= now


@dataclass(slots=True)
class IssuedToken:
    token: str
    session_id: int
    discord_user_id: str
    expires_at: datetime


class BotDatabase:
    """Thin wrapper around SQLAlchemy primitives tailored for the bot service."""

    def __init__(self, database_url: str, *, engine_options: dict[str, Any] | None = None):
        options: dict[str, Any] = {"future": True}
        if engine_options:
            options.update(engine_options)
        self._engine: Engine = create_engine(database_url, **options)
        self._session_factory = sessionmaker(
            bind=self._engine, expire_on_commit=False, class_=Session, future=True
        )

    def migrate(self) -> None:
        """Create tables if they do not exist yet."""

        Base.metadata.create_all(self._engine)

    @contextmanager
    def session(self) -> Iterable[Session]:
        session = self._session_factory()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def find_city_owner(self, channel_id: int | str) -> str | None:
        """Return the owner Discord user ID for a given channel, if any."""

        channel_str = str(channel_id)
        with self.session() as session:
            binding = session.execute(
                select(CityBinding.owner_user_id).where(CityBinding.channel_id == channel_str)
            ).scalar_one_or_none()
        return binding

    def create_oauth_state(self, state: str, redirect_uri: str, ttl: timedelta) -> OAuthStateRecord:
        expires_at = utcnow() + ttl
        with self.session() as session:
            record = OAuthState(state=state, redirect_uri=redirect_uri, expires_at=expires_at)
            session.add(record)
            session.flush()
            return OAuthStateRecord(
                id=record.id,
                state=record.state,
                redirect_uri=record.redirect_uri,
                created_at=record.created_at,
                expires_at=record.expires_at,
            )

    def consume_oauth_state(self, state: str) -> OAuthStateRecord | None:
        now = utcnow()
        with self.session() as session:
            record = session.execute(
                select(OAuthState).where(OAuthState.state == state)
            ).scalar_one_or_none()
            if not record or not record.is_valid(now):
                return None
            record.consumed_at = now
            return OAuthStateRecord(
                id=record.id,
                state=record.state,
                redirect_uri=record.redirect_uri,
                created_at=record.created_at,
                expires_at=record.expires_at,
            )

    def prune_oauth_states(self) -> int:
        now = utcnow()
        stale_threshold = now - timedelta(days=7)
        condition = or_(
            OAuthState.expires_at < now,
            and_(
                OAuthState.consumed_at.isnot(None),
                OAuthState.consumed_at < stale_threshold,
            ),
        )
        with self.session() as session:
            query = session.query(OAuthState).filter(condition)
            count = query.count()
            if count:
                query.delete(synchronize_session=False)
        return count

    def create_session_token(
        self,
        discord_user_id: str,
        raw_token: str,
        *,
        label: str | None,
        expires_at: datetime,
        state_id: int | None = None,
    ) -> IssuedToken:
        digest = hash_token(raw_token)
        now = utcnow()
        with self.session() as session:
            record = session.execute(
                select(LocalAppSession).where(LocalAppSession.discord_user_id == discord_user_id)
            ).scalar_one_or_none()
            if record:
                record.token_hash = digest
                record.label = label
                record.expires_at = expires_at
                record.revoked_at = None
                record.last_seen_at = None
                record.created_at = now
                record.created_by_state_id = state_id
                session_id = record.id
            else:
                record = LocalAppSession(
                    discord_user_id=discord_user_id,
                    token_hash=digest,
                    label=label,
                    expires_at=expires_at,
                    created_by_state_id=state_id,
                )
                session.add(record)
                session.flush()
                session_id = record.id
        return IssuedToken(
            token=raw_token,
            session_id=session_id,
            discord_user_id=discord_user_id,
            expires_at=expires_at,
        )

    def revoke_token(self, raw_token: str) -> bool:
        digest = hash_token(raw_token)
        with self.session() as session:
            record = session.execute(
                select(LocalAppSession).where(LocalAppSession.token_hash == digest)
            ).scalar_one_or_none()
            if not record:
                return False
            record.revoked_at = utcnow()
        return True

    def revoke_tokens_for_user(self, discord_user_id: str) -> int:
        with self.session() as session:
            records = session.execute(
                select(LocalAppSession).where(LocalAppSession.discord_user_id == discord_user_id)
            ).scalars()
            count = 0
            now = utcnow()
            for record in records:
                if record.revoked_at is None:
                    record.revoked_at = now
                    count += 1
        return count

    def prune_expired_sessions(self) -> int:
        now = utcnow()
        with self.session() as session:
            query = session.query(LocalAppSession).filter(LocalAppSession.expires_at < now)
            count = query.count()
            if count:
                query.delete(synchronize_session=False)
        return count

    def authenticate_token(self, raw_token: str) -> AuthenticatedSession | None:
        """Validate a presented token and return the associated session metadata."""

        digest = hash_token(raw_token)
        now = utcnow()
        with self.session() as session:
            record = session.execute(
                select(LocalAppSession).where(LocalAppSession.token_hash == digest)
            ).scalar_one_or_none()
            if not record or record.revoked_at is not None or record.expires_at < now:
                return None
            record.last_seen_at = now
            return AuthenticatedSession(
                discord_user_id=record.discord_user_id,
                session_id=record.id,
                label=record.label,
                expires_at=record.expires_at,
            )

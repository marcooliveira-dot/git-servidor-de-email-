from datetime import datetime, timezone
from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint, create_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker


def now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Base(DeclarativeBase):
    pass


class Admin(Base):
    __tablename__ = 'admins'
    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(100), unique=True)
    password_hash: Mapped[str] = mapped_column(Text)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    session_version: Mapped[int] = mapped_column(Integer, default=1)


class Account(Base):
    __tablename__ = 'accounts'
    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(254), unique=True)
    host: Mapped[str] = mapped_column(String(254))
    port: Mapped[int] = mapped_column(Integer, default=993)
    encrypted_password: Mapped[str] = mapped_column(Text)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    # Activation only through CLI after the pilot, never implicitly by the collector.
    cleanup_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    last_attempt: Mapped[datetime | None] = mapped_column(DateTime)
    last_success: Mapped[datetime | None] = mapped_column(DateTime)
    last_cleanup: Mapped[datetime | None] = mapped_column(DateTime)
    error: Mapped[str | None] = mapped_column(String(200))


class Message(Base):
    __tablename__ = 'messages'
    __table_args__ = (UniqueConstraint('account_id', 'folder', 'uidvalidity', 'uid'),)
    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey('accounts.id'), index=True)
    folder: Mapped[str] = mapped_column(String(512))
    uidvalidity: Mapped[int] = mapped_column(BigInteger)
    uid: Mapped[int] = mapped_column(BigInteger)
    internal_date: Mapped[datetime] = mapped_column(DateTime, index=True)
    subject: Mapped[str] = mapped_column(Text)
    sender: Mapped[str] = mapped_column(Text)
    recipients: Mapped[str] = mapped_column(Text)
    body: Mapped[str] = mapped_column(Text)
    digest: Mapped[str] = mapped_column(String(64))
    size: Mapped[int] = mapped_column(Integer)
    object_key: Mapped[str] = mapped_column(Text)
    primary_version: Mapped[str] = mapped_column(Text)
    backup_version: Mapped[str] = mapped_column(Text)
    archived_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    verified_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime)
    missing_at: Mapped[datetime | None] = mapped_column(DateTime)


class Audit(Base):
    __tablename__ = 'audit'
    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now, index=True)
    actor: Mapped[str] = mapped_column(String(100))
    action: Mapped[str] = mapped_column(String(100))
    details: Mapped[str] = mapped_column(Text, default='')


def database(url):
    engine = create_engine(url, pool_pre_ping=True,
                           connect_args={'check_same_thread': False} if url.startswith('sqlite') else {})
    return engine, sessionmaker(engine, expire_on_commit=False)


def audit(db, actor, action, details=''):
    db.add(Audit(actor=actor, action=action, details=details))

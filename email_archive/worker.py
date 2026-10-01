import fcntl
import logging
import signal
import ssl
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from cryptography.fernet import Fernet
from dateutil.relativedelta import relativedelta
from imapclient import IMAPClient
from sqlalchemy import select

from .archive import IntegrityError, S3Archive, parse_message, sha256
from .config import Settings
from .db import Account, Message, audit, database, now

log = logging.getLogger(__name__)


def utc_naive(value):
    if value.tzinfo is None:
        raise IntegrityError('Data IMAP sem fuso horário.')
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def cutoff(reference=None):
    return (reference or now()) - relativedelta(months=2)


@contextmanager
def connect(account, settings):
    client = IMAPClient(account.host, port=account.port, ssl=True,
                        ssl_context=ssl.create_default_context(), timeout=60)
    try:
        client.login(account.email, Fernet(settings.encryption_key.encode()).decrypt(
            account.encrypted_password.encode()).decode())
        yield client
    finally:
        try:
            client.logout()
        except Exception:
            pass


def fetch_message(client, uid):
    data = client.fetch([uid], ['BODY.PEEK[]', 'INTERNALDATE']).get(uid)
    if not data or not isinstance(data.get(b'BODY[]'), bytes):
        raise IntegrityError('Mensagem IMAP indisponível ou incompleta.')
    date = data.get(b'INTERNALDATE')
    if not isinstance(date, datetime):
        raise IntegrityError('Mensagem IMAP sem data válida.')
    return data[b'BODY[]'], utc_naive(date)


def collect_account(db, account, settings, store, client):
    for flags, delimiter, folder in client.list_folders():
        if b'\\Noselect' in flags or '\\Noselect' in flags:
            continue
        selected = client.select_folder(folder, readonly=True)
        validity = int(selected[b'UIDVALIDITY'])
        # Full UID reconciliation deliberately catches moves and UIDVALIDITY resets.
        present = set(client.search(['ALL']))
        archived = set(db.scalars(select(Message.uid).where(
            Message.account_id == account.id, Message.folder == folder,
            Message.uidvalidity == validity)).all())
        for uid in sorted(present - archived):
            raw, date = fetch_message(client, uid)
            key, digest, primary, backup = store.save(account.id, raw)
            message = Message(account_id=account.id, folder=folder, uidvalidity=validity,
                uid=uid, internal_date=date, object_key=key, digest=digest, size=len(raw),
                primary_version=primary, backup_version=backup, **parse_message(raw))
            store.save_manifest(account, message)
            db.add(message)
            # If commit fails, the IMAP message is untouched. Next collection retries.
            db.commit()
        for message in db.scalars(select(Message).where(
                Message.account_id == account.id, Message.folder == folder,
                Message.deleted_at.is_(None), Message.missing_at.is_(None))):
            if message.uidvalidity != validity or message.uid not in present:
                message.missing_at = now()
        db.commit()


def cleanup_account(db, account, settings, store, client, dry_run=True):
    candidates = db.scalars(select(Message).where(
        Message.account_id == account.id, Message.internal_date < cutoff(),
        Message.deleted_at.is_(None), Message.missing_at.is_(None))).all()
    if dry_run:
        return len(candidates)
    if not settings.cleanup_enabled or not account.cleanup_enabled:
        return 0
    if not client.has_capability('UIDPLUS'):
        raise IntegrityError('Servidor sem UIDPLUS: exclusão seletiva indisponível.')
    deleted = 0
    for message in candidates:
        selected = client.select_folder(message.folder, readonly=False)
        if int(selected[b'UIDVALIDITY']) != message.uidvalidity:
            raise IntegrityError('UIDVALIDITY mudou: coletar novamente antes de limpar.')
        # Re-read both archive copies AND live message before touching any flags.
        store.verify_both(message)
        raw, date = fetch_message(client, message.uid)
        if sha256(raw) != message.digest or date != message.internal_date or date >= cutoff():
            raise IntegrityError('Identidade ou data da mensagem mudou: exclusão bloqueada.')
        audit(db, 'worker', 'cleanup_intent', f'message={message.id}')
        db.commit()
        client.delete_messages([message.uid])
        client.expunge([message.uid])  # IMAPClient emits UID EXPUNGE, never generic EXPUNGE.
        if client.search(['UID', str(message.uid)]):
            raise IntegrityError('Servidor não confirmou a remoção seletiva.')
        message.deleted_at = now()
        message.verified_at = now()
        audit(db, 'worker', 'cleanup_completed', f'message={message.id}')
        db.commit()
        deleted += 1
    account.last_cleanup = now()
    db.commit()
    return deleted


def run_cycle(factory, settings, store, connector=connect, allow_cleanup=False):
    with factory() as db:
        ids = list(db.scalars(select(Account.id).where(Account.enabled.is_(True))))
    for account_id in ids:
        with factory() as db:
            account = db.get(Account, account_id)
            account.last_attempt = now()
            db.commit()
            try:
                with connector(account, settings) as client:
                    collect_account(db, account, settings, store, client)
                    if allow_cleanup:
                        cleanup_account(db, account, settings, store, client, dry_run=False)
                account.last_success = now()
                account.error = None
                audit(db, 'worker', 'collection_completed', f'account={account.id}')
                db.commit()
            except Exception as exc:
                # Store only exception type; protocol errors can contain credentials/content.
                db.rollback()
                account = db.get(Account, account_id)
                account.error = f'Falha ({type(exc).__name__}). Verificar conectividade, credenciais e backup.'
                audit(db, 'worker', 'collection_failed', f'account={account.id};type={type(exc).__name__}')
                db.commit()
                log.error('Falha na conta %s (%s)', account_id, type(exc).__name__)


@contextmanager
def worker_lock(path):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, 'a') as handle:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield


def main():
    logging.basicConfig(level=logging.INFO)
    settings = Settings.from_env()
    engine, factory = database(settings.database_url)
    store = S3Archive(settings)
    stop = False
    def shutdown(*_):
        nonlocal stop
        stop = True
    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    with worker_lock(settings.lock_file):
        store.check_buckets()
        next_cleanup = 0
        while not stop:
            started = time.monotonic()
            clean = settings.cleanup_enabled and started >= next_cleanup
            run_cycle(factory, settings, store, allow_cleanup=clean)
            if clean:
                next_cleanup = started + 86400
            elapsed = time.monotonic() - started
            if elapsed > settings.poll_seconds:
                log.warning('Coleta excedeu o intervalo: %.0f segundos', elapsed)
            # Long runs don't overlap; next cycle starts as soon as possible.
            wait = max(1, settings.poll_seconds - elapsed)
            deadline = time.monotonic() + wait
            while not stop and time.monotonic() < deadline:
                time.sleep(min(1, max(0, deadline - time.monotonic())))
    engine.dispose()


if __name__ == '__main__':
    main()

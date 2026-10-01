import io
from dataclasses import replace
from datetime import datetime, timezone
from email.message import EmailMessage

import pytest
from argon2 import PasswordHasher
from cryptography.fernet import Fernet

from email_archive.archive import IntegrityError, S3Archive, sha256
from email_archive.config import Settings
from email_archive.db import Account, Admin, Base, Message, database
from email_archive.web import create_app
from email_archive.worker import cleanup_account, collect_account, cutoff
from fastapi.testclient import TestClient

RAW = b'From: sender@example.com\r\nTo: employee@example.com\r\nSubject: Documento\r\n\r\nConteudo preservado\r\n'
DATE = datetime(2020, 1, 1, tzinfo=timezone.utc)


@pytest.fixture
def settings(tmp_path):
    return Settings('sqlite:///' + str(tmp_path / 'test.db'), Fernet.generate_key().decode(),
                    's' * 48, 'primary', 'backup', 'sa-east-1', 'http://localhost:8000',
                    True, 300, str(tmp_path / 'worker.lock'), False)


@pytest.fixture
def session(settings):
    engine, factory = database(settings.database_url)
    Base.metadata.create_all(engine)
    with factory() as db:
        account = Account(email='employee@example.com', host='imap.example.com',
            encrypted_password='not-used', cleanup_enabled=True)
        db.add(account)
        db.commit()
        yield db, account
    engine.dispose()


class FakeIMAP:
    def __init__(self, uidplus=True):
        self.uidplus = uidplus
        self.messages = {7: RAW}
        self.validity = 42
        self.date = DATE
        self.deleted = []
        self.expunged = []
        self.readonly = None
    def list_folders(self):
        return [((), '/', 'INBOX')]
    def select_folder(self, folder, readonly=False):
        self.readonly = readonly
        return {b'UIDVALIDITY': self.validity}
    def search(self, criteria):
        if criteria[0] == 'UID':
            return [int(criteria[1])] if int(criteria[1]) in self.messages else []
        return list(self.messages)
    def fetch(self, uids, fields):
        return {uid: {b'BODY[]': self.messages[uid], b'INTERNALDATE': self.date}
                for uid in uids if uid in self.messages}
    def has_capability(self, capability):
        return self.uidplus
    def delete_messages(self, uids):
        assert self.readonly is False
        self.deleted.extend(uids)
    def expunge(self, uids):
        assert uids, 'Nunca executar EXPUNGE genérico'
        self.expunged.extend(uids)
        for uid in uids:
            self.messages.pop(uid, None)


class FakeStore:
    def __init__(self):
        self.raw = None
        self.fail = False
    def save(self, account_id, raw):
        if self.fail:
            raise IntegrityError('backup failed')
        self.raw = raw
        return 'key', sha256(raw), 'primary-version', 'backup-version'
    def verify_both(self, message):
        if self.fail:
            raise IntegrityError('backup corrupt')
        assert sha256(self.raw) == message.digest
        return self.raw
    def save_manifest(self, account, message):
        if self.fail:
            raise IntegrityError('manifest failed')
    def verified_message(self, message):
        return self.verify_both(message)


def collect(session, settings):
    db, account = session
    client, store = FakeIMAP(), FakeStore()
    collect_account(db, account, settings, store, client)
    return db, account, client, store


def test_collect_incremental_never_deletes(session, settings):
    db, account, client, store = collect(session, settings)
    collect_account(db, account, settings, store, client)
    assert db.query(Message).count() == 1
    assert not client.deleted and not client.expunged
    assert client.readonly is True


def test_backup_failure_leaves_mail_unarchived(session, settings):
    db, account = session
    client, store = FakeIMAP(), FakeStore()
    store.fail = True
    with pytest.raises(IntegrityError):
        collect_account(db, account, settings, store, client)
    assert db.query(Message).count() == 0
    assert not client.deleted


@pytest.mark.parametrize('failure', ['backup', 'uidvalidity', 'content', 'date', 'no_uidplus'])
def test_cleanup_blocks_uncertain_mail(session, settings, failure):
    db, account, client, store = collect(session, settings)
    if failure == 'backup': store.fail = True
    if failure == 'uidvalidity': client.validity = 43
    if failure == 'content': client.messages[7] += b'changed'
    if failure == 'date': client.date = datetime.now(timezone.utc)
    if failure == 'no_uidplus': client.uidplus = False
    with pytest.raises(IntegrityError):
        cleanup_account(db, account, settings, store, client, dry_run=False)
    assert not client.deleted and not client.expunged


@pytest.mark.parametrize('global_enabled,account_enabled,dry_run', [(False, True, False), (True, False, False), (True, True, True)])
def test_cleanup_requires_both_flags_and_explicit_execution(session, settings, global_enabled, account_enabled, dry_run):
    db, account, client, store = collect(session, settings)
    account.cleanup_enabled = account_enabled
    cleanup_account(db, account, replace(settings, cleanup_enabled=global_enabled), store, client, dry_run=dry_run)
    assert not client.deleted


def test_cleanup_only_expunge_verified_uid(session, settings):
    db, account, client, store = collect(session, settings)
    client.messages[99] = b'unrelated message previously flagged by another client'
    assert cleanup_account(db, account, settings, store, client, dry_run=False) == 1
    assert client.expunged == [7]
    assert 99 in client.messages
    assert db.query(Message).one().deleted_at is not None


def test_recent_mail_stays(session, settings):
    db, account = session
    client, store = FakeIMAP(), FakeStore()
    client.date = datetime.now(timezone.utc)
    collect_account(db, account, settings, store, client)
    assert cleanup_account(db, account, settings, store, client, dry_run=False) == 0
    assert not client.deleted


def test_two_calendar_months_and_boundary():
    assert cutoff(datetime(2024, 4, 30, 12)) == datetime(2024, 2, 29, 12)
    assert cutoff(datetime(2025, 1, 31)) == datetime(2024, 11, 30)


def test_uidvalidity_change_rearchives_without_wrong_deletion(session, settings):
    db, account, client, store = collect(session, settings)
    client.validity = 43
    collect_account(db, account, settings, store, client)
    old, new = db.query(Message).order_by(Message.id).all()
    assert old.missing_at is not None
    assert new.uidvalidity == 43 and new.missing_at is None


class FakeS3:
    def __init__(self, fail_backup=False):
        self.objects = {}
        self.fail_backup = fail_backup
    def put_object(self, **kwargs):
        if self.fail_backup and kwargs['Bucket'] == 'backup':
            raise RuntimeError('backup unavailable')
        self.objects[(kwargs['Bucket'], kwargs['Key'], 'v1')] = kwargs['Body']
        return {'VersionId': 'v1'}
    def get_object(self, **kwargs):
        return {'Body': io.BytesIO(self.objects[(kwargs['Bucket'], kwargs['Key'], kwargs['VersionId'])])}


def test_s3_verifies_both_bytes_and_versions(settings):
    fake = FakeS3()
    store = S3Archive(settings, client=fake)
    key, digest, pv, bv = store.save(1, RAW)
    fake.objects[('backup', key, bv)] = b'corrupt'
    with pytest.raises(IntegrityError):
        store.verify(key, bv, digest, backup=True)


def test_s3_failed_second_copy_cannot_be_reported_saved(settings):
    with pytest.raises(RuntimeError):
        S3Archive(settings, client=FakeS3(fail_backup=True)).save(1, RAW)


def csrf_token(response):
    import re
    return re.search(r'name="csrf_token" value="([^"]+)"', response.text).group(1)


def test_admin_access_csrf_and_download(session, settings):
    db, account, imap, store = collect(session, settings)
    password = 'A-strong-test-password-123'
    db.add(Admin(username='admin', password_hash=PasswordHasher().hash(password)))
    db.commit()
    app = create_app(settings, store)
    with TestClient(app, base_url=settings.base_url) as client:
        assert client.get('/', follow_redirects=False).status_code == 303
        assert client.get('/messages/1/download', follow_redirects=False).status_code == 303
        login = client.get('/login')
        assert client.post('/login', data={'username':'admin','password':password,'csrf_token':'bad'}).status_code == 403
        response = client.post('/login', data={'username':'admin','password':password,'csrf_token':csrf_token(login)})
        assert response.status_code == 200 and 'Documento' in response.text
        assert client.get('/messages/1/download').content == RAW
        assert client.get('/messages/1').status_code == 200
        assert client.post('/accounts/1/toggle', data={'csrf_token':'bad'}).status_code == 403
        assert client.get('/', headers={'Host':'evil.example'}).status_code == 400
        assert 'no-store' in client.get('/').headers['cache-control']


def test_html_is_escaped_and_attachment_is_download_only(session, settings):
    db, account = session
    mail = EmailMessage()
    mail['From'] = 'sender@example.com'
    mail['Subject'] = '<script>alert(1)</script>'
    mail.set_content('<img src="https://remote.example/tracker">Hello', subtype='html')
    mail.add_attachment(b'sample attachment', maintype='application', subtype='pdf', filename='teste.pdf')
    imap, store = FakeIMAP(), FakeStore()
    imap.messages[7] = mail.as_bytes()
    collect_account(db, account, settings, store, imap)
    db.add(Admin(username='admin', password_hash=PasswordHasher().hash('long-test-password')))
    db.commit()
    with TestClient(create_app(settings, store), base_url=settings.base_url) as client:
        login = client.get('/login')
        client.post('/login', data={'username':'admin','password':'long-test-password','csrf_token':csrf_token(login)})
        response = client.get('/messages/1')
        assert '<script>alert(1)</script>' not in response.text
        assert 'remote.example' not in response.text
        download = client.get('/messages/1/attachments/2')
        assert download.content == b'sample attachment'
        assert download.headers['content-type'] == 'application/octet-stream'
        assert download.headers['content-disposition'].startswith('attachment;')


def test_manifest_failure_blocks_archive_commit(session, settings):
    db, account = session
    class BrokenManifest(FakeStore):
        def save_manifest(self, account, message):
            raise IntegrityError('manifest unavailable')
    client = FakeIMAP()
    with pytest.raises(IntegrityError):
        collect_account(db, account, settings, BrokenManifest(), client)
    assert db.query(Message).count() == 0
    assert not client.deleted


def test_manifest_never_contains_password_and_both_copies_match(session, settings):
    db, account, imap, unused = collect(session, settings)
    fake = FakeS3()
    store = S3Archive(settings, client=fake)
    message = db.query(Message).one()
    store.save_manifest(account, message)
    assert len(fake.objects) == 2
    primary, backup = fake.objects.values()
    assert primary == backup
    assert account.encrypted_password.encode() not in primary
    assert b'encrypted_password' not in primary
    assert b'primary_version' in primary and b'backup_version' in primary


def test_date_search_account_creation_and_credential_encryption(session, settings):
    db, account, imap, store = collect(session, settings)
    db.add(Admin(username='admin', password_hash=PasswordHasher().hash('long-test-password')))
    db.commit()
    with TestClient(create_app(settings, store), base_url=settings.base_url) as client:
        login = client.get('/login')
        client.post('/login', data={'username':'admin','password':'long-test-password','csrf_token':csrf_token(login)})
        assert 'Documento' in client.get('/?start=2020-01-01&end=2020-01-01').text
        assert 'Documento' not in client.get('/?start=2021-01-01&end=2021-12-31').text
        assert client.get('/?start=&end=').status_code == 200
        assert client.get('/?start=not-a-date').status_code == 400
        token = csrf_token(client.get('/accounts'))
        data = {'email':'new@example.com','host':'imap.example.com','password':'mail-password','csrf_token':token}
        assert client.post('/accounts', data=data, headers={'Origin':'https://evil.example'}).status_code == 403
        response = client.post('/accounts', data=data)
        assert response.status_code == 200 and 'mail-password' not in response.text
        db.expire_all()
        created = db.query(Account).filter_by(email='new@example.com').one()
        assert created.encrypted_password != 'mail-password'
        assert Fernet(settings.encryption_key.encode()).decrypt(created.encrypted_password.encode()) == b'mail-password'
        assert created.cleanup_enabled is False


def test_disaster_recovery_restores_search_without_password_or_cleanup(settings, monkeypatch):
    from email_archive import cli
    from sqlalchemy import select
    import sys
    engine, factory = database(settings.database_url)
    Base.metadata.create_all(engine)
    class RestoreStore(FakeStore):
        def manifests(self):
            yield {'schema':1, 'email':'restored@example.com', 'host':'imap.example.com',
                'folder':'INBOX', 'uidvalidity':42, 'uid':7,
                'internal_date':'2020-01-01T00:00:00', 'digest':sha256(RAW),
                'object_key':'old-account/key', 'primary_version':'v1', 'backup_version':'v2', 'size':len(RAW)}
    store = RestoreStore()
    store.raw = RAW
    monkeypatch.setattr(cli.Settings, 'from_env', lambda: settings)
    monkeypatch.setattr(cli, 'S3Archive', lambda unused: store)
    monkeypatch.setattr(sys, 'argv', ['archive', 'restore-index'])
    cli.main()
    cli.main()
    with factory() as db:
        account = db.scalar(select(Account))
        assert not account.enabled and not account.cleanup_enabled
        assert db.query(Message).count() == 1
        assert db.query(Message).one().body.strip() == 'Conteudo preservado'
        assert Fernet(settings.encryption_key.encode()).decrypt(account.encrypted_password.encode()) == b''
    engine.dispose()

import argparse
import getpass
import os
import secrets
from datetime import datetime
from types import SimpleNamespace

from argon2 import PasswordHasher
from cryptography.fernet import Fernet
from sqlalchemy import select

from .archive import IntegrityError, S3Archive, parse_message
from .config import Settings
from .db import Account, Admin, Base, Message, audit, database
from .worker import cutoff, run_cycle, worker_lock


def main():
    parser = argparse.ArgumentParser(description='Administração do arquivo de e-mails')
    subs = parser.add_subparsers(dest='command', required=True)
    env = subs.add_parser('generate-env')
    env.add_argument('--output', required=True)
    subs.add_parser('init-db')
    user = subs.add_parser('admin')
    user.add_argument('username')
    subs.add_parser('check-storage')
    subs.add_parser('collect-once')
    subs.add_parser('cleanup-report')
    subs.add_parser('restore-index')
    clean = subs.add_parser('account-cleanup')
    clean.add_argument('email')
    clean.add_argument('--enable', action='store_true')
    args = parser.parse_args()
    if args.command == 'generate-env':
        text = ('DATABASE_URL="postgresql+psycopg://email_archive:ALTERAR@127.0.0.1/email_archive"\n'
            f'ENCRYPTION_KEY="{Fernet.generate_key().decode()}"\n'
            f'SESSION_SECRET="{secrets.token_urlsafe(48)}"\n'
            'PRIMARY_BUCKET="ALTERAR-arquivo"\nBACKUP_BUCKET="ALTERAR-backup"\n'
            'AWS_REGION="sa-east-1"\nBASE_URL="https://arquivo.example.com"\n'
            'COOKIE_SECURE="true"\nCLEANUP_ENABLED="false"\nPOLL_SECONDS="300"\n'
            'LOCK_FILE="/var/lib/email-archive/worker.lock"\n')
        fd = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w') as handle:
            handle.write(text)
        print('Configuração criada. Edite os campos ALTERAR. Não gere novamente a chave após cadastrar contas.')
        return
    settings = Settings.from_env()
    engine, factory = database(settings.database_url)
    if args.command == 'init-db':
        Base.metadata.create_all(engine)
        print('Banco inicializado. Futuras mudanças de esquema exigirão migrações.')
    elif args.command == 'admin':
        if not args.username or len(args.username) > 100:
            parser.error('Nome de administrador inválido.')
        password = getpass.getpass('Senha (mínimo 14 caracteres): ')
        if len(password) < 14 or password != getpass.getpass('Repita a senha: '):
            parser.error('Senha curta ou confirmação diferente.')
        with factory() as db:
            admin = db.scalar(select(Admin).where(Admin.username == args.username))
            if admin:
                admin.password_hash = PasswordHasher().hash(password)
                admin.session_version += 1
            else:
                db.add(Admin(username=args.username, password_hash=PasswordHasher().hash(password)))
            audit(db, 'cli', 'admin_password_set', f'username={args.username}')
            db.commit()
        print('Administrador configurado; sessões anteriores foram invalidadas.')
    elif args.command == 'check-storage':
        store = S3Archive(settings)
        store.check_buckets()
        key, digest, pv, bv = store.save('installation-check', b'S3 archive installation check\r\n')
        print('Gravação e leitura verificadas nos dois buckets. Objeto de teste preservado.')
    elif args.command == 'collect-once':
        with worker_lock(settings.lock_file):
            store = S3Archive(settings)
            store.check_buckets()
            run_cycle(factory, settings, store, allow_cleanup=False)
        print('Coleta encerrada sem limpeza. Confira o estado de cada conta no painel.')
    elif args.command == 'restore-index':
        # Rebuild archive search from independently saved manifests and EMLs.
        # Accounts are paused and cleanup is disabled, even for existing accounts.
        with worker_lock(settings.lock_file), factory() as db:
            store = S3Archive(settings)
            restored = 0
            for item in store.manifests():
                account = db.scalar(select(Account).where(Account.email == item['email']))
                if not account:
                    account = Account(email=item['email'], host=item['host'],
                        encrypted_password=Fernet(settings.encryption_key.encode()).encrypt(b'').decode(),
                        enabled=False, cleanup_enabled=False)
                    db.add(account)
                    db.flush()
                account.enabled = False
                account.cleanup_enabled = False
                existing = db.scalar(select(Message.id).where(
                    Message.account_id == account.id, Message.folder == item['folder'],
                    Message.uidvalidity == item['uidvalidity'], Message.uid == item['uid']))
                if not existing:
                    reference = SimpleNamespace(**item)
                    raw = store.verify_both(reference)
                    date = datetime.fromisoformat(item['internal_date'])
                    if date.tzinfo is not None:
                        raise IntegrityError('Formato de data inesperado no índice.')
                    db.add(Message(account_id=account.id, folder=item['folder'],
                        uidvalidity=item['uidvalidity'], uid=item['uid'], internal_date=date,
                        digest=item['digest'], object_key=item['object_key'], size=len(raw),
                        primary_version=item['primary_version'], backup_version=item['backup_version'],
                        **parse_message(raw)))
                    restored += 1
                db.commit()
            audit(db, 'cli', 'index_restored', f'messages={restored}')
            db.commit()
        print(f'{restored} registros recuperados. Contas pausadas e limpeza desativada por conta. Revise antes de retomar.')
    elif args.command == 'cleanup-report':
        with factory() as db:
            for account in db.scalars(select(Account)):
                messages = db.scalars(select(Message).where(Message.account_id == account.id,
                    Message.internal_date < cutoff(), Message.deleted_at.is_(None), Message.missing_at.is_(None))).all()
                print(f'{account.id}: {account.email}: {len(messages)} candidatas; '
                      f'conta={account.cleanup_enabled};global={settings.cleanup_enabled}')
        print('Simulação por idade e estado; a integridade é verificada novamente antes de cada exclusão real.')
    elif args.command == 'account-cleanup':
        with factory() as db:
            account = db.scalar(select(Account).where(Account.email == args.email))
            if not account:
                parser.error('Conta não encontrada.')
            if args.enable:
                print('Só habilite após teste de recuperação e revisão da simulação. A exclusão na Locaweb é permanente.')
                if input('Digite o e-mail da conta para confirmar: ').strip() != account.email:
                    parser.error('Confirmação diferente.')
            account.cleanup_enabled = args.enable
            audit(db, 'cli', 'cleanup_policy', f'account={account.id};enabled={args.enable}')
            db.commit()
        print('Política atualizada. A chave global CLEANUP_ENABLED também deve estar habilitada para excluir.')
    engine.dispose()


if __name__ == '__main__':
    main()

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    database_url: str
    encryption_key: str
    session_secret: str
    primary_bucket: str
    backup_bucket: str
    region: str
    base_url: str
    cleanup_enabled: bool
    poll_seconds: int
    lock_file: str
    cookie_secure: bool

    @classmethod
    def from_env(cls):
        required = ('DATABASE_URL', 'ENCRYPTION_KEY', 'SESSION_SECRET', 'PRIMARY_BUCKET', 'BACKUP_BUCKET')
        missing = [name for name in required if not os.environ.get(name)]
        if missing:
            raise RuntimeError('Configuração ausente: ' + ', '.join(missing))
        if os.environ['PRIMARY_BUCKET'] == os.environ['BACKUP_BUCKET']:
            raise RuntimeError('Arquivo e backup devem usar buckets diferentes.')
        secret = os.environ['SESSION_SECRET']
        if len(secret) < 32:
            raise RuntimeError('SESSION_SECRET deve ter pelo menos 32 caracteres.')
        base = os.getenv('BASE_URL', 'https://arquivo.example.com').rstrip('/')
        secure = os.getenv('COOKIE_SECURE', 'true').lower() == 'true'
        if not base.startswith('https://') and secure:
            raise RuntimeError('Use HTTPS; COOKIE_SECURE=false é permitido apenas para desenvolvimento local.')
        if not secure and not base.startswith(('http://localhost:', 'http://127.0.0.1:')):
            raise RuntimeError('Cookies sem HTTPS só são permitidos em localhost.')
        return cls(os.environ['DATABASE_URL'], os.environ['ENCRYPTION_KEY'], secret,
                   os.environ['PRIMARY_BUCKET'], os.environ['BACKUP_BUCKET'],
                   os.getenv('AWS_REGION', 'sa-east-1'), base,
                   os.getenv('CLEANUP_ENABLED', 'false').lower() == 'true',
                   max(300, int(os.getenv('POLL_SECONDS', '300'))),
                   os.getenv('LOCK_FILE', '/var/lib/email-archive/worker.lock'), secure)

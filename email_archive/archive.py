import base64
import hashlib
import json
from email import policy
from email.parser import BytesParser

import boto3
from botocore.config import Config
from bs4 import BeautifulSoup


class IntegrityError(RuntimeError):
    pass


def sha256(raw):
    return hashlib.sha256(raw).hexdigest()


def parse_message(raw):
    message = BytesParser(policy=policy.default).parsebytes(raw)
    parts = []
    for part in message.walk():
        if part.get_content_disposition() == 'attachment' or part.get_filename():
            continue
        if part.get_content_type() in ('text/plain', 'text/html'):
            try:
                text = part.get_content()
            except (LookupError, UnicodeError, ValueError):
                text = (part.get_payload(decode=True) or b'').decode('utf-8', errors='replace')
            if isinstance(text, str):
                if part.get_content_type() == 'text/html':
                    soup = BeautifulSoup(text, 'html.parser')
                    for item in soup(['script', 'style']):
                        item.decompose()
                    text = soup.get_text(' ', strip=True)
                parts.append(text)
    return dict(subject=str(message.get('subject', '(sem assunto)')),
                sender=str(message.get('from', '')),
                recipients=', '.join(str(message.get(k, '')) for k in ('to', 'cc', 'bcc')).strip(', '),
                body='\n'.join(parts))


class S3Archive:
    def __init__(self, settings, client=None):
        self.settings = settings
        self.client = client or boto3.client('s3', region_name=settings.region,
            config=Config(connect_timeout=15, read_timeout=60, retries={'max_attempts': 4}))

    def check_buckets(self):
        for bucket in (self.settings.primary_bucket, self.settings.backup_bucket):
            if self.client.get_bucket_versioning(Bucket=bucket).get('Status') != 'Enabled':
                raise IntegrityError('É necessário habilitar versionamento nos dois buckets.')

    def read(self, key, version, backup=False):
        response = self.client.get_object(
            Bucket=self.settings.backup_bucket if backup else self.settings.primary_bucket,
            Key=key, VersionId=version)
        stream = response['Body']
        try:
            return stream.read()
        finally:
            stream.close()

    def verify(self, key, version, digest, backup=False):
        raw = self.read(key, version, backup)
        if sha256(raw) != digest:
            raise IntegrityError('Conteúdo do arquivo difere do hash registrado.')
        return raw

    def save(self, account_id, raw):
        digest = sha256(raw)
        key = f'accounts/{account_id}/{digest[:2]}/{digest}.eml'
        versions = []
        for backup, bucket in enumerate((self.settings.primary_bucket, self.settings.backup_bucket)):
            response = self.client.put_object(Bucket=bucket, Key=key, Body=raw,
                ContentType='message/rfc822', ServerSideEncryption='AES256',
                ChecksumSHA256=base64.b64encode(hashlib.sha256(raw).digest()).decode(),
                Metadata={'sha256': digest})
            version = response.get('VersionId')
            if not version or version == 'null':
                raise IntegrityError('Arquivo sem versão: limpeza bloqueada.')
            self.verify(key, version, digest, bool(backup))
            versions.append(version)
        return key, digest, versions[0], versions[1]

    def verified_message(self, message):
        return self.verify(message.object_key, message.primary_version, message.digest)

    def verify_both(self, message):
        raw = self.verified_message(message)
        self.verify(message.object_key, message.backup_version, message.digest, backup=True)
        return raw

    def save_manifest(self, account, message):
        """Keep recoverable index data outside the database; never include credentials."""
        folder_hash = sha256(message.folder.encode())
        key = f'accounts/{account.id}/index/{folder_hash}/{message.uidvalidity}-{message.uid}.json'
        metadata = {
            'schema': 1, 'email': account.email, 'host': account.host,
            'folder': message.folder, 'uidvalidity': message.uidvalidity, 'uid': message.uid,
            'internal_date': message.internal_date.isoformat(), 'digest': message.digest,
            'object_key': message.object_key, 'primary_version': message.primary_version,
            'backup_version': message.backup_version, 'size': message.size,
        }
        raw = json.dumps(metadata, ensure_ascii=False).encode()
        digest = sha256(raw)
        for backup, bucket in enumerate((self.settings.primary_bucket, self.settings.backup_bucket)):
            response = self.client.put_object(Bucket=bucket, Key=key, Body=raw,
                ContentType='application/json', ServerSideEncryption='AES256',
                ChecksumSHA256=base64.b64encode(hashlib.sha256(raw).digest()).decode())
            version = response.get('VersionId')
            if not version or version == 'null':
                raise IntegrityError('Índice externo sem versionamento.')
            self.verify(key, version, digest, bool(backup))

    def manifests(self):
        paginator = self.client.get_paginator('list_objects_v2')
        for page in paginator.paginate(Bucket=self.settings.backup_bucket, Prefix='accounts/'):
            for item in page.get('Contents', []):
                key = item['Key']
                if '/index/' not in key or not key.endswith('.json'):
                    continue
                response = self.client.get_object(Bucket=self.settings.backup_bucket, Key=key)
                stream = response['Body']
                try:
                    if item['Size'] > 65536:
                        raise IntegrityError('Manifesto com tamanho inesperado.')
                    data = json.loads(stream.read())
                finally:
                    stream.close()
                if data.get('schema') != 1:
                    raise IntegrityError('Versão de manifesto não suportada.')
                yield data

import hmac
import secrets
from datetime import date, datetime, timedelta
from email import policy
from email.parser import BytesParser
from pathlib import Path
from urllib.parse import quote, urlparse

from argon2 import PasswordHasher
from argon2.exceptions import VerificationError
from cryptography.fernet import Fernet
from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, or_, select
from starlette.middleware.sessions import SessionMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .archive import S3Archive
from .config import Settings
from .db import Account, Admin, Audit, Message, audit, database, now
from .worker import cutoff

hasher = PasswordHasher()
dummy_hash = hasher.hash(secrets.token_urlsafe(24))


def create_app(settings=None, store=None):
    settings = settings or Settings.from_env()
    engine, factory = database(settings.database_url)
    store = store or S3Archive(settings)
    app = FastAPI(title='Arquivo de e-mails', docs_url=None, redoc_url=None, openapi_url=None)
    app.state.factory = factory
    app.add_middleware(SessionMiddleware, secret_key=settings.session_secret,
                       session_cookie='archive_session', max_age=8 * 3600,
                       same_site='strict', https_only=settings.cookie_secure)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=[urlparse(settings.base_url).hostname])
    templates = Jinja2Templates(directory=str(Path(__file__).parent / 'templates'))

    @app.middleware('http')
    async def security_headers(request, call_next):
        response = await call_next(request)
        response.headers['Content-Security-Policy'] = "default-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self'; frame-ancestors 'none'; form-action 'self'; base-uri 'none'"
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Referrer-Policy'] = 'no-referrer'
        response.headers['Cache-Control'] = 'no-store'
        if settings.cookie_secure:
            response.headers['Strict-Transport-Security'] = 'max-age=31536000'
        return response

    def csrf(request):
        if 'csrf' not in request.session:
            request.session['csrf'] = secrets.token_urlsafe(32)
        return request.session['csrf']

    def verify_csrf(request, token):
        origin = request.headers.get('origin')
        if origin and origin != settings.base_url:
            raise HTTPException(403, 'Origem inválida.')
        if not hmac.compare_digest(token, request.session.get('csrf', '')) or not token:
            raise HTTPException(403, 'Sessão inválida. Atualize a página.')

    def admin(request, db):
        user = db.get(Admin, request.session.get('admin_id', -1))
        if not user or not user.active or user.session_version != request.session.get('session_version'):
            raise HTTPException(303, headers={'Location': '/login'})
        return user

    def render(request, template_name, **context):
        return templates.TemplateResponse(request=request, name=template_name,
            context={'csrf': csrf(request), 'cleanup_enabled': settings.cleanup_enabled, **context})

    @app.get('/healthz')
    def health():
        with factory() as db:
            db.execute(select(1))
        return {'status': 'ok'}

    @app.get('/login', response_class=HTMLResponse)
    def login_page(request: Request):
        return render(request, 'login.html', error=None)

    @app.post('/login')
    def login(request: Request, username: str = Form(...), password: str = Form(...),
              csrf_token: str = Form(...)):
        verify_csrf(request, csrf_token)
        username = username[:100]
        # Persistent rate limit survives restarts. Count failures globally and per username.
        with factory() as db:
            failures = db.scalar(select(func.count()).select_from(Audit).where(
                Audit.action == 'login_failed', Audit.created_at > now() - timedelta(minutes=15)))
            per_user = db.scalar(select(func.count()).select_from(Audit).where(
                Audit.actor == username, Audit.action == 'login_failed',
                Audit.created_at > now() - timedelta(minutes=15)))
            if failures >= 100 or per_user >= 5:
                return render(request, 'login.html', error='Muitas tentativas. Aguarde 15 minutos.')
            user = db.scalar(select(Admin).where(Admin.username == username))
            try:
                valid = hasher.verify(user.password_hash if user else dummy_hash, password)
            except VerificationError:
                valid = False
            if not user or not user.active or not valid:
                audit(db, username, 'login_failed')
                db.commit()
                return render(request, 'login.html', error='Usuário ou senha inválidos.')
            request.session.clear()
            request.session.update(admin_id=user.id, session_version=user.session_version,
                                   csrf=secrets.token_urlsafe(32))
            audit(db, user.username, 'login_success')
            db.commit()
        return RedirectResponse('/', status_code=303)

    @app.post('/logout')
    def logout(request: Request, csrf_token: str = Form(...)):
        verify_csrf(request, csrf_token)
        request.session.clear()
        return RedirectResponse('/login', status_code=303)

    @app.get('/', response_class=HTMLResponse)
    def dashboard(request: Request, q: str = '', account_id: int = 0, page: int = 1,
                  start: str = '', end: str = ''):
        q, page = q[:200].strip(), max(1, page)
        try:
            start = date.fromisoformat(start) if start else None
            end = date.fromisoformat(end) if end else None
        except ValueError:
            raise HTTPException(400, 'Período inválido.') from None
        with factory() as db:
            user = admin(request, db)
            accounts = db.scalars(select(Account).order_by(Account.email)).all()
            conditions = []
            if q:
                conditions.append(or_(*(getattr(Message, field).icontains(q, autoescape=True)
                    for field in ('subject', 'sender', 'recipients', 'body'))))
            if account_id:
                conditions.append(Message.account_id == account_id)
            if start:
                conditions.append(Message.internal_date >= datetime.combine(start, datetime.min.time()))
            if end:
                conditions.append(Message.internal_date < datetime.combine(end, datetime.min.time()) + timedelta(days=1))
            # One visible entry per original content within each account, including folder moves.
            ids = select(func.min(Message.id)).where(*conditions).group_by(Message.account_id, Message.digest)
            query = select(Message, Account.email).join(Account).where(Message.id.in_(ids))
            total = db.scalar(select(func.count()).select_from(ids.subquery()))
            entries = db.execute(query.order_by(Message.internal_date.desc(), Message.id.desc())
                                 .offset((page - 1) * 30).limit(30)).all()
            errors = sum(bool(a.error) for a in accounts)
            delayed = sum(a.enabled and (not a.last_success or a.last_success < now() - timedelta(minutes=15)) for a in accounts)
            audit(db, user.username, 'search', f'account={account_id};page={page};query_present={bool(q)}')
            db.commit()
            return render(request, 'dashboard.html', user=user, accounts=accounts, entries=entries,
                total=total, errors=errors, delayed=delayed, q=q, account_id=account_id, page=page,
                start=start, end=end,
                next_url=f'/?q={quote(q)}&account_id={account_id}&page={page+1}' + (f'&start={start}' if start else '') + (f'&end={end}' if end else ''),
                prev_url=f'/?q={quote(q)}&account_id={account_id}&page={page-1}' + (f'&start={start}' if start else '') + (f'&end={end}' if end else ''))

    def get_message(db, message_id):
        message = db.get(Message, message_id)
        if not message:
            raise HTTPException(404, 'Mensagem não encontrada.')
        return message

    def read_message(db, user, message):
        try:
            return store.verified_message(message)
        except Exception:
            try:
                raw = store.verify(message.object_key, message.backup_version, message.digest, backup=True)
                audit(db, user.username, 'backup_read', f'message={message.id}')
                db.commit()
                return raw
            except Exception:
                raise HTTPException(503, 'Arquivo indisponível ou com falha de integridade. Verifique o armazenamento.') from None

    @app.get('/messages/{message_id}', response_class=HTMLResponse)
    def message_page(request: Request, message_id: int):
        with factory() as db:
            user = admin(request, db)
            message = get_message(db, message_id)
            raw = read_message(db, user, message)
            parsed = BytesParser(policy=policy.default).parsebytes(raw)
            attachments = [(i, part.get_filename() or 'anexo', len(part.get_payload(decode=True) or b''))
                           for i, part in enumerate(parsed.walk())
                           if not part.is_multipart() and (part.get_filename() or part.get_content_disposition() == 'attachment')]
            audit(db, user.username, 'message_view', f'message={message_id}')
            db.commit()
            return render(request, 'message.html', user=user, message=message,
                          account=db.get(Account, message.account_id), attachments=attachments)

    @app.get('/messages/{message_id}/download')
    def download(request: Request, message_id: int):
        with factory() as db:
            user = admin(request, db)
            message = get_message(db, message_id)
            raw = read_message(db, user, message)
            audit(db, user.username, 'message_download', f'message={message_id}')
            db.commit()
            return Response(raw, media_type='application/octet-stream',
                headers={'Content-Disposition': f'attachment; filename="email-{message_id}.eml"'})

    @app.get('/messages/{message_id}/attachments/{part_id}')
    def attachment(request: Request, message_id: int, part_id: int):
        with factory() as db:
            user = admin(request, db)
            message = get_message(db, message_id)
            raw = read_message(db, user, message)
            parts = list(BytesParser(policy=policy.default).parsebytes(raw).walk())
            if part_id < 0 or part_id >= len(parts):
                raise HTTPException(404)
            part = parts[part_id]
            if part.is_multipart() or not (part.get_filename() or part.get_content_disposition() == 'attachment'):
                raise HTTPException(404)
            name = (part.get_filename() or 'anexo').replace('\\', '/').split('/')[-1]
            name = ''.join(c for c in name if c.isprintable())[:200] or 'anexo'
            audit(db, user.username, 'attachment_download', f'message={message_id};part={part_id}')
            db.commit()
            return Response(part.get_payload(decode=True) or b'', media_type='application/octet-stream',
                headers={'Content-Disposition': "attachment; filename*=UTF-8''" + quote(name, safe='')})

    @app.get('/accounts', response_class=HTMLResponse)
    def accounts_page(request: Request):
        with factory() as db:
            user = admin(request, db)
            accounts = db.scalars(select(Account).order_by(Account.email)).all()
            return render(request, 'accounts.html', user=user, accounts=accounts, error=None)

    @app.post('/accounts')
    def add_account(request: Request, email: str = Form(...), host: str = Form(...),
                    password: str = Form(...), csrf_token: str = Form(...)):
        verify_csrf(request, csrf_token)
        email, host = email.strip().lower(), host.strip().lower()
        with factory() as db:
            user = admin(request, db)
            if len(email) > 254 or '@' not in email or not host or len(host) > 254 or '/' in host or ':' in host or not password:
                raise HTTPException(400, 'Informe e-mail, hostname IMAP e senha válidos.')
            if db.scalar(select(Account.id).where(Account.email == email)):
                raise HTTPException(400, 'Conta já cadastrada. Use a opção de atualizar a senha.')
            encrypted = Fernet(settings.encryption_key.encode()).encrypt(password.encode()).decode()
            account = Account(email=email, host=host, encrypted_password=encrypted)
            db.add(account)
            db.flush()
            audit(db, user.username, 'account_created', f'account={account.id}')
            db.commit()
        return RedirectResponse('/accounts', status_code=303)

    @app.post('/accounts/{account_id}/password')
    def account_password(request: Request, account_id: int, password: str = Form(...), csrf_token: str = Form(...)):
        verify_csrf(request, csrf_token)
        with factory() as db:
            user = admin(request, db)
            account = db.get(Account, account_id)
            if not account or not password:
                raise HTTPException(400, 'Conta ou senha inválida.')
            account.encrypted_password = Fernet(settings.encryption_key.encode()).encrypt(password.encode()).decode()
            audit(db, user.username, 'account_password_updated', f'account={account.id}')
            db.commit()
        return RedirectResponse('/accounts', status_code=303)

    @app.post('/accounts/{account_id}/toggle')
    def account_toggle(request: Request, account_id: int, csrf_token: str = Form(...)):
        verify_csrf(request, csrf_token)
        with factory() as db:
            user = admin(request, db)
            account = db.get(Account, account_id)
            if not account:
                raise HTTPException(404)
            account.enabled = not account.enabled
            audit(db, user.username, 'account_toggle', f'account={account.id};enabled={account.enabled}')
            db.commit()
        return RedirectResponse('/accounts', status_code=303)

    @app.get('/audit', response_class=HTMLResponse)
    def audit_page(request: Request):
        with factory() as db:
            user = admin(request, db)
            entries = db.scalars(select(Audit).order_by(Audit.id.desc()).limit(200)).all()
            return render(request, 'audit.html', user=user, entries=entries)

    @app.get('/cleanup', response_class=HTMLResponse)
    def cleanup_page(request: Request):
        with factory() as db:
            user = admin(request, db)
            entries = db.execute(select(Account.email, func.count(Message.id)).join(Message).where(
                Message.internal_date < cutoff(), Message.deleted_at.is_(None), Message.missing_at.is_(None)
                ).group_by(Account.email)).all()
            return render(request, 'cleanup.html', user=user, entries=entries, cutoff=cutoff())

    return app

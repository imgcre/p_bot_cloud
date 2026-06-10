import asyncio
import base64
import json
import re
import secrets
import time
from dataclasses import dataclass
from decimal import Decimal
from io import BytesIO
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qsl, urlencode, urlsplit

import aiohttp
import aiosqlite
import qrcode
from mirai import At, GroupMessage, MessageChain
from plugin import Inject, InstrAttr, Plugin, autorun, route, top_instr
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse
from utilities import User

import configs.config as config

from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from plugins.achv import Achv
    from plugins.admin import Admin
    from plugins.check_in import CheckIn
    from plugins.known_groups import KnownGroups
    from plugins.voucher import Voucher


@dataclass
class ConsoleSession:
    id: str
    openid: str
    nickname: str
    avatar_url: str


@route('console')
class Console(Plugin):
    public_base_url = 'https://bot.napluss.cn'
    known_groups: Inject['KnownGroups']
    admin: Inject['Admin']
    voucher: Inject['Voucher']
    check_in: Inject['CheckIn']
    achv: Inject['Achv']

    def __init__(self):
        self._db_ready = asyncio.Event()
        self._db_lock = asyncio.Lock()

    @property
    def db_path(self):
        data_dir = Path('backups') / 'console'
        data_dir.mkdir(parents=True, exist_ok=True)
        return str(data_dir / 'console.sqlite3')

    async def _connect(self):
        await self._db_ready.wait()
        conn = await aiosqlite.connect(self.db_path)
        conn.row_factory = aiosqlite.Row
        return conn

    async def _init_db(self):
        async with self._db_lock:
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute(
                    '''
                    CREATE TABLE IF NOT EXISTS console_bindings (
                        openid TEXT PRIMARY KEY,
                        qq_id INTEGER NOT NULL UNIQUE,
                        nickname TEXT NOT NULL,
                        avatar_url TEXT NOT NULL,
                        created_at INTEGER NOT NULL,
                        updated_at INTEGER NOT NULL
                    )
                    '''
                )
                await db.execute(
                    '''
                    CREATE TABLE IF NOT EXISTS console_bind_codes (
                        code TEXT PRIMARY KEY,
                        openid TEXT NOT NULL,
                        nickname TEXT NOT NULL,
                        avatar_url TEXT NOT NULL,
                        session_id TEXT NOT NULL,
                        created_at INTEGER NOT NULL,
                        expires_at INTEGER NOT NULL,
                        used_at INTEGER
                    )
                    '''
                )
                await db.execute('CREATE INDEX IF NOT EXISTS idx_console_bind_codes_openid ON console_bind_codes(openid)')
                await db.execute('CREATE INDEX IF NOT EXISTS idx_console_bind_codes_session_id ON console_bind_codes(session_id)')
                await db.commit()
        self._db_ready.set()

    def _json(self, data: dict, *, status_code: int = 200):
        return JSONResponse({'code': 0, 'data': data}, status_code=status_code)

    def _error(self, err_msg: str, *, status_code: int = 400):
        return JSONResponse({'code': 1, 'errMsg': err_msg}, status_code=status_code)

    def _oauth_config(self):
        app_id = getattr(config, 'QQ_CONNECT_APP_ID', '')
        app_key = getattr(config, 'QQ_CONNECT_APP_KEY', '')
        redirect_uri = getattr(config, 'QQ_CONNECT_REDIRECT_URI', f'{self.public_base_url}/api/console/auth/qq/callback')
        if not app_id or not app_key:
            raise RuntimeError('QQ Connect OAuth is not configured')
        return app_id, app_key, redirect_uri

    def _frontend_url(self, path: str, params: Optional[dict] = None):
        url = f'{self.public_base_url}{path}'
        if params:
            url += '?' + urlencode(params)
        return url

    def _safe_return_to(self, value: Optional[str]):
        if not value:
            return None
        parsed = urlsplit(value)
        if parsed.scheme or parsed.netloc or not value.startswith('/'):
            return None
        if value.startswith('//') or value.startswith('/api/'):
            return None
        return value

    def _session_cookie(self, request: Request):
        session_id = request.cookies.get('pbot_console_session')
        if not session_id:
            return None
        if not re.fullmatch(r'[A-Za-z0-9_-]{32,96}', session_id):
            return None
        return session_id

    def _make_state(self):
        return secrets.token_urlsafe(24)

    def _make_session_id(self):
        return secrets.token_urlsafe(32)

    def _make_bind_code(self):
        alphabet = 'ABCDEFGHJKLMNPQRSTUVWXYZ23456789'
        raw = ''.join(secrets.choice(alphabet) for _ in range(8))
        return f'{raw[:4]}-{raw[4:]}'

    def _make_qr_data_url(self, text: str):
        qr = qrcode.QRCode(border=1, box_size=8)
        qr.add_data(text)
        qr.make(fit=True)
        image = qr.make_image(fill_color='black', back_color='white').convert('RGB')
        out = BytesIO()
        image.save(out, format='PNG')
        return 'data:image/png;base64,' + base64.b64encode(out.getvalue()).decode('ascii')

    async def _fetchone(self, db, query: str, params: tuple):
        cursor = await db.execute(query, params)
        try:
            return await cursor.fetchone()
        finally:
            await cursor.close()

    async def _exchange_code(self, code: str):
        app_id, app_key, redirect_uri = self._oauth_config()
        async with aiohttp.ClientSession() as session:
            async with session.get(
                'https://graph.qq.com/oauth2.0/token',
                params={
                    'grant_type': 'authorization_code',
                    'client_id': app_id,
                    'client_secret': app_key,
                    'code': code,
                    'redirect_uri': redirect_uri,
                },
            ) as response:
                token_text = await response.text()
            token_data = dict(parse_qsl(token_text))
            access_token = token_data.get('access_token')
            if not access_token:
                raise RuntimeError('QQ OAuth token exchange failed')

            async with session.get(
                'https://graph.qq.com/oauth2.0/me',
                params={'access_token': access_token},
            ) as response:
                me_text = await response.text()
            me_match = re.search(r'\{.*\}', me_text)
            if me_match is None:
                raise RuntimeError('QQ OAuth openid response invalid')
            me_data = json.loads(me_match.group(0))
            openid = me_data.get('openid')
            if not openid:
                raise RuntimeError('QQ OAuth openid missing')

            async with session.get(
                'https://graph.qq.com/user/get_user_info',
                params={
                    'access_token': access_token,
                    'oauth_consumer_key': app_id,
                    'openid': openid,
                },
            ) as response:
                info = await response.json(content_type=None)
            if int(info.get('ret', 1)) != 0:
                raise RuntimeError(info.get('msg') or 'QQ user info failed')

        return {
            'openid': openid,
            'nickname': info.get('nickname') or '',
            'avatar_url': info.get('figureurl_qq_2') or info.get('figureurl_qq_1') or info.get('figureurl_2') or '',
        }

    async def _create_session(self, *, openid: str, nickname: str, avatar_url: str, session_id: Optional[str] = None):
        if session_id is None:
            session_id = self._make_session_id()
        code = self._make_bind_code()
        now = int(time.time())
        expires_at = now + 10 * 60
        async with self._db_lock:
            db = await self._connect()
            try:
                await db.execute('DELETE FROM console_bind_codes WHERE openid = ? AND used_at IS NULL', (openid,))
                while True:
                    row = await self._fetchone(db, 'SELECT code FROM console_bind_codes WHERE code = ?', (code,))
                    if row is None:
                        break
                    code = self._make_bind_code()
                await db.execute(
                    '''
                    INSERT INTO console_bind_codes
                        (code, openid, nickname, avatar_url, session_id, created_at, expires_at, used_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, NULL)
                    ''',
                    (code, openid, nickname, avatar_url, session_id, now, expires_at),
                )
                await db.commit()
            finally:
                await db.close()
        return session_id

    async def _get_session(self, request: Request) -> Optional[ConsoleSession]:
        session_id = self._session_cookie(request)
        if session_id is None:
            return None
        db = await self._connect()
        try:
            row = await self._fetchone(
                db,
                '''
                SELECT openid, nickname, avatar_url, session_id
                FROM console_bind_codes
                WHERE session_id = ?
                ORDER BY created_at DESC
                LIMIT 1
                ''',
                (session_id,),
            )
        finally:
            await db.close()
        if row is None:
            return None
        return ConsoleSession(
            id=session_id,
            openid=row['openid'],
            nickname=row['nickname'],
            avatar_url=row['avatar_url'],
        )

    async def _get_binding_by_openid(self, openid: str):
        db = await self._connect()
        try:
            return await self._fetchone(
                db,
                'SELECT openid, qq_id, nickname, avatar_url, created_at, updated_at FROM console_bindings WHERE openid = ?',
                (openid,),
            )
        finally:
            await db.close()

    async def _get_binding_by_qq(self, qq_id: int):
        db = await self._connect()
        try:
            return await self._fetchone(
                db,
                'SELECT openid, qq_id FROM console_bindings WHERE qq_id = ?',
                (qq_id,),
            )
        finally:
            await db.close()

    async def _bind(self, *, code: str, qq_id: int):
        code = code.upper()
        now = int(time.time())
        async with self._db_lock:
            db = await self._connect()
            try:
                row = await self._fetchone(
                    db,
                    '''
                    SELECT code, openid, nickname, avatar_url, expires_at, used_at
                    FROM console_bind_codes
                    WHERE code = ?
                    ''',
                    (code,),
                )
                if row is None or row['used_at'] is not None or int(row['expires_at']) < now:
                    raise RuntimeError('验证码不存在或已过期')

                existing_openid = await self._fetchone(
                    db,
                    'SELECT qq_id FROM console_bindings WHERE openid = ?',
                    (row['openid'],),
                )
                if existing_openid is not None and int(existing_openid['qq_id']) != qq_id:
                    raise RuntimeError('该网页账号已绑定其他QQ')

                existing_qq = await self._fetchone(
                    db,
                    'SELECT openid FROM console_bindings WHERE qq_id = ?',
                    (qq_id,),
                )
                if existing_qq is not None and existing_qq['openid'] != row['openid']:
                    raise RuntimeError('该QQ已绑定其他网页账号')

                await db.execute(
                    '''
                    INSERT INTO console_bindings (openid, qq_id, nickname, avatar_url, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(openid) DO UPDATE SET
                        qq_id = excluded.qq_id,
                        nickname = excluded.nickname,
                        avatar_url = excluded.avatar_url,
                        updated_at = excluded.updated_at
                    ''',
                    (row['openid'], qq_id, row['nickname'], row['avatar_url'], now, now),
                )
                await db.execute('UPDATE console_bind_codes SET used_at = ? WHERE code = ?', (now, code))
                await db.commit()
            finally:
                await db.close()

    async def _get_primary_member(self, qq_id: int):
        for group_id in self.known_groups:
            member = await self.bot.get_group_member(group_id, qq_id)
            if member is not None:
                return member
        return None

    async def _make_profile(self, binding):
        qq_id = int(binding['qq_id'])
        member = await self._get_primary_member(qq_id)
        if member is None:
            raise RuntimeError('绑定QQ不在已知群内')
        async with self.override(member):
            violation = self.admin.gls_violation.get_data(member.group.id, qq_id)
            morality = 0 if violation is None else -violation.count
            voucher_cnt: Decimal = await self.voucher.get_count()
            is_checked_in: bool = await self.check_in.is_checked_in_today()
            achv_count = len(await self.achv.get_obtained())
        return {
            'qq_id': qq_id,
            'member_name': member.member_name,
            'group_id': member.group.id,
            'group_name': member.group.name,
            'nickname': binding['nickname'],
            'avatar_url': binding['avatar_url'],
            'morality': morality,
            'voucher_cnt': str(voucher_cnt),
            'is_checked_in': is_checked_in,
            'achv_count': achv_count,
        }

    async def auth_start(self, request: Request):
        try:
            app_id, _, redirect_uri = self._oauth_config()
        except RuntimeError as e:
            return RedirectResponse(self._frontend_url('/console', {'error': str(e)}), status_code=302)
        state = self._make_state()
        return_to = self._safe_return_to(request.query_params.get('return_to'))
        params = {
            'response_type': 'code',
            'client_id': app_id,
            'redirect_uri': redirect_uri,
            'state': state,
            'scope': 'get_user_info',
        }
        response = RedirectResponse('https://graph.qq.com/oauth2.0/authorize?' + urlencode(params), status_code=302)
        response.set_cookie('pbot_console_oauth_state', state, max_age=600, httponly=True, secure=True, samesite='lax')
        if return_to:
            response.set_cookie('pbot_console_oauth_return_to', return_to, max_age=600, httponly=True, secure=True, samesite='lax')
        else:
            response.delete_cookie('pbot_console_oauth_return_to')
        return response

    async def auth_callback(self, request: Request):
        code = request.query_params.get('code')
        state = request.query_params.get('state')
        cookie_state = request.cookies.get('pbot_console_oauth_state')
        if not code or not state or state != cookie_state:
            return RedirectResponse(self._frontend_url('/console', {'error': 'QQ授权状态无效'}), status_code=302)
        try:
            info = await self._exchange_code(code)
            session_id = await self._create_session(**info)
        except Exception as e:
            return RedirectResponse(self._frontend_url('/console', {'error': str(e)}), status_code=302)

        return_to = self._safe_return_to(request.cookies.get('pbot_console_oauth_return_to')) or '/console'
        response = RedirectResponse(self._frontend_url(return_to), status_code=302)
        response.delete_cookie('pbot_console_oauth_state')
        response.delete_cookie('pbot_console_oauth_return_to')
        response.set_cookie('pbot_console_session', session_id, max_age=60 * 60 * 24 * 30, httponly=True, secure=True, samesite='lax')
        return response

    async def session(self, request: Request):
        session = await self._get_session(request)
        if session is None:
            return self._json({'authenticated': False})
        binding = await self._get_binding_by_openid(session.openid)
        if binding is None:
            return self._json({
                'authenticated': True,
                'bound': False,
                'nickname': session.nickname,
                'avatar_url': session.avatar_url,
            })
        try:
            profile = await self._make_profile(binding)
        except Exception as e:
            return self._error(str(e), status_code=409)
        return self._json({
            'authenticated': True,
            'bound': True,
            'profile': profile,
        })

    async def bind_code(self, request: Request):
        session = await self._get_session(request)
        if session is None:
            return self._error('未登录', status_code=401)
        binding = await self._get_binding_by_openid(session.openid)
        if binding is not None:
            return self._json({'bound': True})
        db = await self._connect()
        try:
            row = await self._fetchone(
                db,
                '''
                SELECT code, expires_at
                FROM console_bind_codes
                WHERE session_id = ? AND used_at IS NULL
                ORDER BY created_at DESC
                LIMIT 1
                ''',
                (session.id,),
            )
        finally:
            await db.close()
        if row is None or int(row['expires_at']) < int(time.time()):
            await self._create_session(
                openid=session.openid,
                nickname=session.nickname,
                avatar_url=session.avatar_url,
                session_id=session.id,
            )
            return await self.bind_code(request)
        command = f'#绑定控制台 {row["code"]}'
        join_url = 'https://qm.qq.com/q/gOHPUYkkb6'
        return self._json({
            'bound': False,
            'code': row['code'],
            'command': command,
            'expires_at': row['expires_at'],
            'join_url': join_url,
            'join_qr_data_url': self._make_qr_data_url(join_url),
        })

    async def check_in_api(self, request: Request):
        session = await self._get_session(request)
        if session is None:
            return self._error('未登录', status_code=401)
        binding = await self._get_binding_by_openid(session.openid)
        if binding is None:
            return self._error('未绑定', status_code=403)
        qq_id = int(binding['qq_id'])
        member = await self._get_primary_member(qq_id)
        if member is None:
            return self._error('绑定QQ不在已知群内', status_code=409)
        try:
            event = GroupMessage(sender=member, messageChain=MessageChain([]))
            with self.engine.of(event) as ctx, ctx:
                async with self.override(member, User(qq_id)):
                    await self.check_in.do_check_in(raise_error=True, silent=True, skip_feedback=True)
        except Exception as e:
            return self._error(str(e), status_code=409)
        binding = await self._get_binding_by_openid(session.openid)
        return self._json({'profile': await self._make_profile(binding)})

    @top_instr('绑定控制台', InstrAttr.NO_ALERT_CALLER)
    async def bind_console_cmd(self, code: str, event: GroupMessage):
        if event.sender.group.id not in set(self.known_groups):
            return
        await self._bind(code=code, qq_id=event.sender.id)
        return [At(target=event.sender.id), ' 控制台绑定成功']

    @autorun
    async def startup(self):
        await self._init_db()
        self.bot.asgi.add_route('/api/console/auth/qq/start', self.auth_start, ['GET'])
        self.bot.asgi.add_route('/api/console/auth/qq/callback', self.auth_callback, ['GET'])
        self.bot.asgi.add_route('/api/console/session', self.session, ['GET'])
        self.bot.asgi.add_route('/api/console/bind-code', self.bind_code, ['GET'])
        self.bot.asgi.add_route('/api/console/check-in', self.check_in_api, ['POST'])

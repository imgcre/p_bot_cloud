import asyncio
import base64
import hashlib
import json
import re
import time
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Optional

import aiosqlite
import qrcode
from PIL import Image as PILImage
from plugin import Plugin, autorun, route
from starlette.requests import Request
from starlette.responses import JSONResponse


@dataclass(frozen=True)
class CodeShare:
    id: str
    url: str


@route('WebShare')
class WebShare(Plugin):
    public_base_url = 'https://bot.napluss.cn'
    id_pattern = re.compile(r'^[0-9a-z]{8,32}$')

    def __init__(self):
        self._db_ready = asyncio.Event()
        self._db_lock = asyncio.Lock()

    @property
    def db_path(self):
        data_dir = Path('backups') / 'web_share'
        data_dir.mkdir(parents=True, exist_ok=True)
        return str(data_dir / 'code_snippets.sqlite3')

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
                    CREATE TABLE IF NOT EXISTS code_snippets (
                        id TEXT PRIMARY KEY,
                        content_hash TEXT NOT NULL,
                        lang TEXT NOT NULL,
                        content TEXT NOT NULL,
                        source TEXT NOT NULL,
                        metadata_json TEXT NOT NULL,
                        created_at INTEGER NOT NULL
                    )
                    '''
                )
                await db.execute(
                    'CREATE INDEX IF NOT EXISTS idx_code_snippets_content_hash ON code_snippets(content_hash)'
                )
                await db.commit()
        self._db_ready.set()

    def _make_content_hash(self, content: str, lang: str):
        payload = f'{lang}\0{content}'.encode('utf-8')
        return hashlib.sha256(payload).hexdigest()

    def _make_id(self, content_hash: str, length: int):
        return base64.b32encode(bytes.fromhex(content_hash)).decode('ascii').lower().rstrip('=')[:length]

    async def _fetchone(self, db, query: str, params: tuple):
        cursor = await db.execute(query, params)
        try:
            return await cursor.fetchone()
        finally:
            await cursor.close()

    async def create_code_share(
        self,
        content: str,
        *,
        lang: str = '',
        source: str = 'ai_ext',
        metadata: Optional[dict] = None,
    ):
        normalized_content = content.rstrip('\n') or ' '
        normalized_lang = lang.strip().lower()
        content_hash = self._make_content_hash(normalized_content, normalized_lang)
        metadata_json = json.dumps(metadata or {}, ensure_ascii=False, sort_keys=True)
        now = int(time.time())

        async with self._db_lock:
            db = await self._connect()
            try:
                for length in (8, 10, 12, 16, 20, 26, 32):
                    code_id = self._make_id(content_hash, length)
                    row = await self._fetchone(
                        db,
                        'SELECT content_hash FROM code_snippets WHERE id = ?',
                        (code_id,),
                    )
                    if row is None:
                        await db.execute(
                            '''
                            INSERT INTO code_snippets
                                (id, content_hash, lang, content, source, metadata_json, created_at)
                            VALUES (?, ?, ?, ?, ?, ?, ?)
                            ''',
                            (
                                code_id,
                                content_hash,
                                normalized_lang,
                                normalized_content,
                                source,
                                metadata_json,
                                now,
                            ),
                        )
                        await db.commit()
                        return CodeShare(code_id, f'{self.public_base_url}/code/{code_id}')
                    if row['content_hash'] == content_hash:
                        return CodeShare(code_id, f'{self.public_base_url}/code/{code_id}')
            finally:
                await db.close()

        raise RuntimeError('failed to allocate code share id')

    async def get_code_record(self, code_id: str):
        if not self.id_pattern.fullmatch(code_id):
            return None
        db = await self._connect()
        try:
            row = await self._fetchone(
                db,
                '''
                SELECT id, lang, content, source, metadata_json, created_at
                FROM code_snippets
                WHERE id = ?
                ''',
                (code_id,),
            )
        finally:
            await db.close()
        if row is None:
            return None
        return {
            'id': row['id'],
            'lang': row['lang'],
            'content': row['content'],
            'source': row['source'],
            'metadata': json.loads(row['metadata_json'] or '{}'),
            'created_at': row['created_at'],
        }

    def append_qr_to_png(self, png_bytes: bytes, url: str):
        with PILImage.open(BytesIO(png_bytes)) as source:
            image = source.convert('RGBA')

        qr = qrcode.QRCode(border=1, box_size=3)
        qr.add_data(url)
        qr.make(fit=True)
        qr_image = qr.make_image(fill_color='black', back_color='white').convert('RGBA')
        qr_size = qr_image.width

        pad = max(6, qr_size // 18)
        box_size = qr_size + pad * 2
        box = PILImage.new('RGBA', (box_size, box_size), (255, 255, 255, 235))
        box.alpha_composite(qr_image, (pad, pad))

        margin = max(14, qr_size // 8)
        footer_height = box_size + margin * 2
        footer_color = image.getpixel((min(image.width - 1, 8), max(0, image.height - 8)))
        canvas = PILImage.new('RGBA', (image.width, image.height + footer_height), footer_color)
        canvas.alpha_composite(image, (0, 0))

        x = max(margin, image.width - box_size - margin)
        y = image.height + margin
        canvas.alpha_composite(box, (x, y))

        out = BytesIO()
        canvas.save(out, format='PNG')
        return out.getvalue()

    async def get_code(self, request: Request):
        code_id = request.path_params.get('code_id') or request.query_params.get('id')
        if not code_id:
            return JSONResponse({'code': 1, 'errMsg': 'missing code id'}, status_code=400)

        record = await self.get_code_record(code_id)
        if record is None:
            return JSONResponse({'code': 1, 'errMsg': 'code not found'}, status_code=404)
        return JSONResponse({'code': 0, 'data': record})

    @autorun
    async def startup(self):
        await self._init_db()
        self.bot.asgi.add_route('/api/code', self.get_code, ['GET'])
        self.bot.asgi.add_route('/api/code/{code_id}', self.get_code, ['GET'])

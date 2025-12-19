
from dataclasses import dataclass
from decimal import Decimal
import aiohttp
from mirai import get_logger
from mirai.asgi import ASGI
from starlette.requests import Request
from starlette.responses import JSONResponse
from nap_cat_types import GetGroupMemberInfoResp
from plugin import Inject, InstrAttr, Plugin, autorun, delegate, route
from utilities import UserSpec

from typing import TYPE_CHECKING, Optional
if TYPE_CHECKING:
    from plugins.known_groups import KnownGroups
    from plugins.nap_cat import NapCat
    from plugins.check_in import CheckIn
    from plugins.voucher import Voucher

class UserMan():
    openid: Optional[str] = None
    ...

logger = get_logger()

@route('mini')
class Mini(Plugin):
    users: UserSpec[UserMan] = UserSpec[UserMan]()

    known_groups: Inject['KnownGroups']
    nap_cat: Inject['NapCat']
    check_in: Inject['CheckIn']
    voucher: Inject['Voucher']

    @delegate()
    async def get_matched_qqids(self, *, nickname: str):
        matched_qqids = []

        for group_id in self.known_groups:
            member_infos = await self.nap_cat.get_group_member_list(group_id)
            for info in member_infos:
                if info.nickname == nickname:
                    matched_qqids.append(info.user_id)

        return matched_qqids
    
    @delegate(InstrAttr.FORCE_BACKUP)
    async def update_user_record(self, *, qqid: int, openid: str):
        man = self.users.get_or_create_data(qqid)
        man.openid = openid

    def get_user_man_by_openid(self, openid: str):
        for qqid, man in self.users.users.items():
            if man.openid == openid:
                return qqid, man

    @delegate()
    async def user_info_endpoint(self, request: Request):
        # with self.engine.of() as c, c:
        data: dict[str, str] = await request.json()
        openid = data.get("openid")

        res = self.get_user_man_by_openid(openid)
        if res is None:
            return JSONResponse({
                "code": 1,
                "errMsg": '用户未绑定'
            })
        
        qqid, man = res
        for group_id in self.known_groups:
            member = await self.bot.get_group_member(group_id, qqid)
            if member is None:
                return JSONResponse({
                    "code": 1,
                    "errMsg": '群员不存在'
                })
            async with self.override(member):
                is_checked_in: bool = await self.check_in.is_checked_in_today()
                voucher_cnt: Decimal = await self.voucher.get_count()

        return JSONResponse({
            "code": 0,
            "is_checked_in": is_checked_in,
            "voucher_cnt": str(voucher_cnt),
        })


    async def bind_endpoint(self, request: Request):
        with self.engine.of() as c, c:
            data: dict[str, str] = await request.json()

            nickname = data.get("nickname")
            openid = data.get("openid")

            res = self.get_user_man_by_openid(openid)

            if res is not None:
                return JSONResponse({
                    "code": 0
                })
            
            logger.info(f'{nickname=}, {openid=}')

            matched_qqids: list[int] = await self.get_matched_qqids(nickname=nickname)

            logger.info(f'{matched_qqids=}')

            if len(matched_qqids) == 0:
                return JSONResponse({
                    "code": 1,
                    "errMsg": '未找到匹配的用户'
                })
        
            if len(matched_qqids) > 1:
                return JSONResponse({
                    "code": 1,
                    "errMsg": '用户冲突, 请联系管理员'
                })
            
            qqid = matched_qqids[0]
            
            await self.update_user_record(qqid=qqid, openid=openid)

            return JSONResponse({
                "code": 0
            })
    

    async def login_endpoint(self, request: Request):
        data: dict[str, str] = await request.json()

        code = data.get("code")
        # 866UZjMprcGQYAHy
        async with aiohttp.ClientSession() as session:
            async with session.get('https://api.q.qq.com/sns/jscode2session', params={
                'appid': '1112171843',
                'secret': '866UZjMprcGQYAHy',
                'js_code': code,
                'grant_type': 'authorization_code'
            }) as response:
                j = await response.json()
                return JSONResponse({
                    "openid": j["openid"],
                })
        ...

    async def test_endpoint(self, request: Request):
        # 获取 JSON 数据
        data: dict[str, str] = await request.json()
        
        # 或者手动解析
        # body = await request.body()
        # if body:
        #     data = json.loads(body)
        
        # 获取特定字段
        name = data.get("name", "未知")
        
        return JSONResponse({
            "status": "ok",
            "name": name
        })

    @autorun
    async def startup(self):
        asgi = ASGI()

        asgi.add_route('/mini-test', self.test_endpoint, ['POST'])
        asgi.add_route('/login', self.login_endpoint, ['POST'])
        asgi.add_route('/bind', self.bind_endpoint, ['POST'])
        asgi.add_route('/user_info', self.user_info_endpoint, ['POST'])

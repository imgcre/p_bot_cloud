
from dataclasses import dataclass
from decimal import Decimal
import inspect
from types import MethodType
import aiohttp
from mirai import get_logger
from mirai.asgi import ASGI
from starlette.requests import Request
from starlette.responses import JSONResponse
from nap_cat_types import GetGroupMemberInfoResp
from plugin import Inject, InstrAttr, Plugin, autorun, delegate, route
from utilities import UserSpec

from typing import TYPE_CHECKING, Awaitable, Callable, Optional, Union, get_args, get_origin
if TYPE_CHECKING:
    from plugins.known_groups import KnownGroups
    from plugins.nap_cat import NapCat
    from plugins.check_in import CheckIn
    from plugins.voucher import Voucher

class UserMan():
    openid: Optional[str] = None
    ...

logger = get_logger()

def is_optional(t):
    origin = get_origin(t)
    args = get_args(t)
    return origin is Union and len(args) == 2 and args[1] is type(None)

async def endpoint_args_resolver(m: MethodType, args: tuple[Request]):
    request, = args
    s = inspect.signature(m)
    params = [p for p in s.parameters.values() if p.kind not in (p.KEYWORD_ONLY, p.VAR_KEYWORD)]
    aas = []
    data: dict[str, str] = await request.json()

    for p in params:
        anno = p.annotation
        
        if anno is Request:
            aas.append(request)
            continue

        value = data.get(p.name)
        if value is None and not is_optional(anno):
            raise RuntimeError(f'参数"{p.name}"不存在')

        if anno in (str, int, float, bool):
            aas.append(anno(value))
            continue

        raise RuntimeError(f'不支持参数"{p.name}"的类型')

async def endpoint_wrapper(func: Callable[[], Awaitable]):
    try:
        return JSONResponse({
            "code": 0,
            **await func(),
        })
    except Exception as e:
        return JSONResponse({
            "code": 1,
            "errMsg": str(e)
        })

def endpoint(func: Callable):
    wrapper = delegate(custom_resolver=endpoint_args_resolver, custom_wrapper=endpoint_wrapper)(func)
    wrapper._endpoint_ = True
    return wrapper

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

    @endpoint
    async def user_info(self, request: Request):
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


    @endpoint
    async def bind(self, request: Request):
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
    
    @endpoint
    async def login(self, request: Request):
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


    @endpoint
    async def test(self, name: str):
        if name == 'Lisa':
            raise RuntimeError('不认识这个人')
        
        return {
            "name": name
        }

    @autorun
    async def startup(self):
        asgi = ASGI()

        for _, method in inspect.getmembers(self, predicate=inspect.ismethod):
            if hasattr(method, '_endpoint_'):
                asgi.add_route(f'/{method.__name__}', method, ['POST'])

        # asgi.add_route('/mini-test', self.test_endpoint, ['POST'])
        # asgi.add_route('/login', self.login_endpoint, ['POST'])
        # asgi.add_route('/bind', self.bind_endpoint, ['POST'])
        # asgi.add_route('/user_info', self.user_info_endpoint, ['POST'])

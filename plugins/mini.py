
import aiohttp
from mirai.asgi import ASGI
from starlette.requests import Request
from starlette.responses import JSONResponse
from plugin import Plugin, autorun, route

@route('mini')
class Mini(Plugin):

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

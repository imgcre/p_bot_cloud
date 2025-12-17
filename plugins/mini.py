
from mirai.asgi import ASGI
from starlette.requests import Request
from starlette.responses import JSONResponse
from plugin import Plugin, autorun, route

@route('mini')
class Mini(Plugin):

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

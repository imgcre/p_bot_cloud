对库的修改：

.venv\lib\site-packages\mirai\models\message.py
添加了 MarketFace 和 ShortVideo

.venv\Lib\site-packages\mirai\models\entities.py
添加了 GroupMemberActive, 修改了 MemberInfoModel

.venv\Lib\site-packages\mirai\bot.pyi
839 添加了 member_info 的重载

.venv\lib\site-packages\mirai\models\api.py
628 修改了 Recall

pyppeteer的版本有冲突，得单独用pip来安装

pip install httpx[socks]


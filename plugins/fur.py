import asyncio
from dataclasses import dataclass, field
from decimal import Decimal
import math
import time
from typing import Dict, Final, List, Optional, Set
import typing

from mirai import At, GroupMessage, MessageEvent

import mirai.models.message
from mirai.models.message import Quote
from event_types import AchvRemovedEvent
from plugin import Context, Plugin, autorun, delegate, enable_backup, instr, top_instr, any_instr, InstrAttr, PathArg, route, Inject, nudge_instr, unmute_instr
import random
from bilibili_api import topic, dynamic
import os
import random
from PIL import Image, ExifTags, TiffImagePlugin
from utilities import VOUCHER_NAME, VOUCHER_UNIT, AchvEnum, AchvExtra, AchvInfo, AchvOpts, AchvRarity, AchvRarityVal, GroupLocalStorage, GroupLocalStorageAsEvent, GroupMemberOp, GroupSpec, Source, VoucherRecordExtraClearMute, get_delta_time_str, get_logger, handler, throttle_config
import uuid
import aiohttp
import base64
import imghdr
import json
import itertools
import re
from enum import Enum, auto
from mirai.models.entities import Group, GroupMember
import pathlib

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from plugins.renderer import Renderer
    from plugins.achv import Achv
    from plugins.bili import Bili
    from plugins.known_groups import KnownGroups
    from plugins.admin import Admin
    from plugins.voucher import Voucher
    from plugins.throttle import Throttle

logger = get_logger()

# 小孩子不可以看
class FurAchv(AchvEnum):
    LING_YI = 0, '灵翼事件', '通过非指定方式抽到由灵翼老师拍摄的返图', AchvOpts(rarity=AchvRarity.UNCOMMON, custom_obtain_msg='触发了灵翼事件', display='🦄')
    NSFW = 1, '小孩子不可以看', '使用指令【#来只纳延】抽选到了纳延的色情图片', AchvOpts(rarity=AchvRarity.RARE, custom_obtain_msg='找到了好康的', display='🧶')
    BLACK = 2, '有效返图', '抽到了星效的画面几乎纯黑色的返图', AchvOpts(rarity=AchvRarity.RARE, custom_obtain_msg='这张图里有一只穿云月, 你发现了吗？')
    SUN = 3, '神说，要有光', '使用指令【#来只灯泡】抽到了会导致更长时间禁言的图片(此时图片中的内容一般是太阳)', AchvOpts(rarity=AchvRarity.LEGEND, custom_obtain_msg='感觉自己很烧', display='☀️')
    ESCAPE = 4, '逃过一劫', '使用指令【#来只灯泡】抽到了LED灯的图片, 此时将不会对抽到此图片的群成员进行禁言操作', AchvOpts(rarity=AchvRarity.RARE, custom_obtain_msg='表示：就这？', display='🍀')
    BOOM = 5, '繁荣', '？', AchvOpts(rarity=AchvRarity.COMMOM, custom_obtain_msg='踩到了炸弹💣', display='💣')
    BRIGHTLY_LIT = 6, '灯火通明', '累积被禁言1000次', AchvOpts(rarity=AchvRarity.EPIC, custom_obtain_msg='来到了照明商店', target_obtained_cnt=1000, unit='只灯泡', display='💡')
    HALF_FULL = 7, '半步轮回境', '单次禁言时长超过30分钟', AchvOpts(rarity=AchvRarity.RARE, custom_obtain_msg='夺造化，转涅盘，握生死，掌轮回。', display='🎭')
    SUPERSATURATED_SOLUTION = 8, '过饱和溶液', '单次禁言时长超过60分钟', AchvOpts(rarity=AchvRarity.EPIC, custom_obtain_msg='即将析出晶体', display='⚗️')
    FORBIDDEN_QUINTET = 9, '禁忌五重奏', '在仍在禁言的状态中继续被禁言五次', AchvOpts(rarity=AchvRarity.UNCOMMON, custom_obtain_msg='奏响了禁忌的五重奏', display='🎼')
    BAT = 10, '小蝙蝠', '使用指令【#来只暗辰】成功抽到了100次暗辰的图片', AchvOpts(rarity=AchvRarity.EPIC, locked=True, custom_obtain_msg='打开了传送门', target_obtained_cnt=100, display='🦇')
    FLAWLESS = 11, '十全美', '使用指令【#来只犯错】成功抽到了全部10种类型的犯错方式', AchvOpts(rarity=AchvRarity.RARE, custom_obtain_msg='完满了', target_obtained_cnt=10, display='🐱')

class MatchLevel(Enum):
    PERFECT = auto()
    FLUZZY = auto()

@dataclass
class MuteLogic():
    level: int = 1
    last_mute_tsc: int = 0

    def get_mute_duration(self):
        level_dec = time.time() // (10 * 60) - self.last_mute_tsc // (10 * 60)
        self.last_mute_tsc = time.time()
        self.level = max(self.level - level_dec + 1, 1)
        return math.ceil(60 * min(60, pow(1.2, self.level)))
        ...
    ...

@dataclass
class MuteMan():
    last_mute_ts: int = 0
    last_mute_duration: int = 0
    depth: int = 0

    def is_muting(self):
        return time.time() < self.last_mute_ts + self.last_mute_duration

    def get_remains_duration(self):
        return max(0, self.last_mute_ts + self.last_mute_duration - time.time())

    def clear(self):
        self.last_mute_ts = 0
        self.last_mute_duration = 0
        self.depth = 0

    def update_mute(self, duration: int):
        if self.get_remains_duration() > 0:
            self.depth += 1
        else:
            self.depth = 1
        self.last_mute_ts = time.time()
        self.last_mute_duration = duration
        return self.depth
    
@dataclass
class FurPicMsgRecord():
    msg_id: int
    source_id: Optional[int] = None
    created_ts: int = field(default_factory=time.time)

@dataclass
class FurPicMsgMan():
    records: list[FurPicMsgRecord] = field(default_factory=list)

    ...

class FurException(Exception): ...

class FurNotFoundException(FurException): ...

class AllFetchedException(FurException): ...

class PartialFetchedException(FurException): ...

@route('毛毛')
@enable_backup
class Fur(Plugin):
    gs_mute_logic: GroupSpec[MuteLogic] = GroupSpec[MuteLogic]()
    gs_fur_pic_msg_man: GroupSpec[FurPicMsgMan] = GroupSpec[FurPicMsgMan]()
    gls_mute_man: GroupLocalStorage[MuteMan] = GroupLocalStorage[MuteMan]()

    bili: Inject['Bili']
    known_groups: Inject['KnownGroups']
    renderer: Inject['Renderer']
    achv: Inject['Achv']
    admin: Inject['Admin']
    voucher: Inject['Voucher']
    throttle: Inject['Throttle']

    FETCH_AUTHOR_HISTORY_SIZE: Final = 10
    FETCH_IMG_PATH_HISTORY_SIZE: Final = 50

    fetch_author_history: Dict[str, List[str]] = {} # 目录的名字
    fetch_img_path_history: List[str] = []

    def __init__(self) -> None:
        random.seed()
        self.last_run_time = time.time()

    @autorun
    async def auto_recall_fur_pic(self, ctx: Context):
        await asyncio.sleep(5)
        while True:
            await asyncio.sleep(1)
            with ctx:
                for g_id in self.gs_fur_pic_msg_man.groups.keys():
                    group = await self.bot.get_group(g_id)
                    if group is None: continue
                    async with self.override(group):
                        await self.recall_outdated_fur_pic()

    @delegate()
    async def recall_outdated_fur_pic(self, group: Group, man: FurPicMsgMan):
        n = []
        for r in man.records:
            if time.time() - r.created_ts > 60 * 5:
                self.backup_man.set_dirty()
                try:
                    source_id = r.source_id
                    if source_id is None:
                        source_id = group.id
                    logger.debug(f'[recall {source_id=}, {r.msg_id=}]')
                    await self.bot.recall(r.msg_id, source_id)
                except: ...
                continue
            n.append(r)
        man.records = n

    @any_instr(InstrAttr.NO_ALERT_CALLER)
    async def auto_cockroach(self, event: GroupMessage):
        now = time.time()
        if event.sender.id == 2899441232 and (now - self.last_run_time > 60 * 60) and random.random() < 0.1:
            self.last_run_time = now
            return await self.get_pic('蟑螂', reset_cd=False)

    @top_instr('万物展厅', InstrAttr.NO_ALERT_CALLER)
    async def wwpass_gallery(self):
        api_url = 'https://www.ww-pass.com/api-v2/portal/list_character?limit=100'
        async with aiohttp.ClientSession(trust_env=True) as session:
            async with session.get(api_url) as response:
                j = await response.json()

        # {
        #     "_id": "662f788e053ebbc5759936b1",
        #     "cover_img": {
        #         "width": 1080,
        #         "height": 1440,
        #         "url": "https://web.oss.ww-pass.cn/gallery/char/picture/gd.jpg"
        #     },
        #     "designer": "蛙",
        #     "name": "光电效应",
        #     "source": "自设",
        #     "species": "✈️"
        # },
        item = random.choice(j['data']['list'])
        return [
            mirai.models.message.Image(url=f"{item['cover_img']['url']}@!cover_character"),
            f'\n   ---{item["name"]}'
        ]
        ...

    @top_instr('排单队列', InstrAttr.NO_ALERT_CALLER)
    async def wwpass_queue(self):
        api_url = 'https://web.oss.ww-pass.cn/api-status/order-list.json'

        async with aiohttp.ClientSession(trust_env=True) as session:
            async with session.get(api_url) as response:
                j = await response.json()
        ss = []

        for months in j['data']['list']:
            if 'list_custom' not in months: continue
            ss.append(f'--={months["year"]}年{months["month"]}月=--')
            for co_er in months["list_custom"]:
                product = {
                    'S': '🔵',
                    'O': '🔴',
                    'D': '🟣',
                }[co_er["product"]]
                state = {
                    '已完成': '👌',
                    '进行中': '⏳',
                    '未开始': '🔒',
                }[co_er["state"]]
                ss.append(f'{co_er["title"]} {product} {state}')
        return '\n'.join(ss)

    @top_instr('来测试', InstrAttr.NO_ALERT_CALLER)
    async def get_test(self):
        path = r'D:\projects\python\p_bot\plugins\fur\纳延\HT-364784069\Cache_1027207359e17904..jpg'
        with open(path, "rb") as image_file:
            b64_input = base64.b64encode(image_file.read()).decode('utf-8')
        what = imghdr.what(path)
        b64_url = f'data:image/{what};base64,{b64_input}'

        b64_img = await self.renderer.render('pic_details', data={
            'img_url': b64_url,

        })
        return [
            mirai.models.message.Image(base64=b64_img)
        ]

    @top_instr('毛五', InstrAttr.NO_ALERT_CALLER)
    async def ff(self):
        async with self.bili as credential:
            # res = await topic.search_topic('毛毛星期五')
            t = topic.Topic(topic_id=30607, credential=credential)
            cards = await t.get_cards(sort_by=topic.TopicCardsSortBy.RECOMMEND)
            random.shuffle(cards)
            for card in cards:
                if isinstance(card, dynamic.Dynamic):
                    dyn_info = await card.get_info()
                    if(dyn_info['item']['type'] != 'DYNAMIC_TYPE_DRAW'): continue
                    author_name = dyn_info['item']['modules']['module_author']['name']
                    major = dyn_info['item']['modules']['module_dynamic']['major']
                    if major['type'] == 'MAJOR_TYPE_OPUS':
                        pic_url = random.choice(major['opus']['pics'])['url']
                    elif major['type'] == 'MAJOR_TYPE_DRAW':
                        pic_url = random.choice(major['draw']['items'])['src']
                    else:
                        logger.debug(dyn_info)
                        raise Exception('找不到毛毛图片')
                    logger.debug(pic_url)
                    return [
                        mirai.models.message.Image(url=pic_url),
                        f'\n   ---来自: {author_name}'
                    ]

    @top_instr('禁言我')
    async def give_me_a_bulb(self):
        return await self.deliver_light_bulb()
        ...

    @top_instr('((来|吃).*?(只|点|份|条|头|个|碗|吨|块|把|双|群|匹|位|名|根|颗|朵|片|张|本|支|段|架|套|滴|幅|座|盘|所|斤|串|台|壶|瓶|杯|团|毫?升|量|艘|枚|阵|发)|看看)(?P<expr>.*?)', InstrAttr.NO_ALERT_CALLER, InstrAttr.FORCE_BACKUP)
    async def fur(self, expr: PathArg[str], author: Optional[At]):
        return await self.get_pic(expr, author)
            
    @delegate()
    async def deliver_light_bulb(self, **kwargs):
        return await self.get_pic('💡', reset_cd=False, **kwargs)
        ...

    @any_instr(InstrAttr.NO_ALERT_CALLER)
    async def xuelang_at(self, event: MessageEvent):
        xue_cnt = 0

        used_achv: Enum = await self.achv.get_used()
        if used_achv is not None:
            if used_achv is FurAchv.ESCAPE:
                return
            
            info: AchvInfo = used_achv.value
            rarity_val: AchvRarityVal = info.opts.rarity.value
            if rarity_val.level >= AchvRarity.LEGEND.value.level and not info.opts.is_punish:
                return

        async def is_boom_id(id: int):
            # if id == 254081521:
            #     return True
            member = await self.member_from(member_id=id)
            async with self.override(member):
                if await self.achv.is_used(FurAchv.BOOM):
                    return True
            return False

        for c in event.message_chain:
            if isinstance(c, At) and await is_boom_id(c.target):
                xue_cnt += 1
            if isinstance(c, Quote) and await is_boom_id(c.sender_id):
                xue_cnt += 1

        logger.debug(f'{xue_cnt=}')

        if xue_cnt > 0:
            await self.achv.submit(FurAchv.BOOM)
            return await self.deliver_light_bulb(factor=xue_cnt)

    @unmute_instr(InstrAttr.FORCE_BACKUP)
    async def clear_mute_state(self, man: MuteMan):
        await self.voucher.adjust(
            cnt=Decimal('-1'), 
            force=True, 
            extra=VoucherRecordExtraClearMute(exchange_duration=man.get_remains_duration())
        )
        man.clear()
        return [f'消耗了一{VOUCHER_UNIT}{VOUCHER_NAME}, 解除了禁言状态']

    @delegate(InstrAttr.FORCE_BACKUP)
    @throttle_config(name='返图', max_cooldown_duration=1*60*60)
    async def get_pic(self, expr: str, author: Optional[At], group: Group, mute_logic: MuteLogic, glse_gls_mute_man_: gls_mute_man.event_t(), member_op: GroupMemberOp, source: Source, fur_pic_msg_man: FurPicMsgMan, *, mute_targets: set[int]=None, factor: int=1, reset_cd: bool=True):
        await self.admin.check_proxy(disable_required=True)
        author = None
        glse_gls_mute_man = typing.cast(GroupLocalStorageAsEvent[MuteMan], glse_gls_mute_man_)
        
        with open(self.path.data.of_file('nickname_mappings.json'), encoding='utf-8') as f:
            j = json.load(f)

        def render_template(s, *, base_fac=None):
            while True:
                of_sp = re.split(r'\$\{(.*?)\}', s)
                if len(of_sp) == 1:
                    break
                li = []
                for idx, replacer in enumerate(of_sp):
                    if idx % 2 != 0:
                        if base_fac is not None and replacer == 'base':
                            replacer = base_fac()
                            ...
                        if replacer in j['templates']:
                            replacer = j['templates'][replacer]
                    li.append(replacer)
                s = ''.join(li)
            return s
        
        def get_role_weight(role_name, *, def_weight=1):
            obj = j['roles'][role_name]
            
            if isinstance(obj, dict):
                return obj['weight'] if 'weight' in obj else def_weight
            if isinstance(obj, list):
                for item in obj:
                    if isinstance(item, dict) and 'weight' in item:
                        return item['weight']
            return def_weight

        def _match(arr, name, *, curr_depth = 0, role: str=None):
            depth: float = None

            def update_depth(new_val: float):
                nonlocal depth
                if new_val is None:
                    return
                if depth is None:
                    depth = new_val
                if new_val < depth:
                    depth = new_val

            
            mixins = []

            def wrap_to_list(v):
                if isinstance(v, list):
                    return v
                return [v]
            
            def regexes_from_def_arr(def_arr):
                result = []
                if isinstance(def_arr, list):
                    result = list(itertools.chain.from_iterable([
                        [obj['regex'] for obj in def_arr if isinstance(obj, dict) and 'regex' in obj],
                        *[subarr for subarr in def_arr if isinstance(subarr, list)]
                    ]))
                else:
                    if 'regex' in def_arr:
                        result = wrap_to_list(def_arr['regex'])
                return result
            
            def keywords_from_def_arr(def_arr):
                result = []
                if isinstance(def_arr, list):
                    result = [v for v in def_arr if isinstance(v, str)]
                if isinstance(def_arr, dict):
                    if 'keyword' in def_arr:
                        result = wrap_to_list(def_arr['keyword'])
                return result
                ...

            keywords = keywords_from_def_arr(arr)
            regexes = regexes_from_def_arr(arr)

            if isinstance(arr, list):
                mixins = list(itertools.chain.from_iterable([wrap_to_list(obj['mixin']) for obj in arr if isinstance(obj, dict) and 'mixin' in obj]))
                
            if isinstance(arr, dict):
                if 'mixin' in arr:
                    mixins = wrap_to_list(arr['mixin'])

            # logger.debug(f'{mixins=}')

            if name in [render_template(v) for v in keywords]:
                update_depth(curr_depth)
            for mixins in mixins:
                if mixins in j['mixins']:
                    if name == mixins:
                        update_depth(curr_depth + 1)
                    update_depth(_match(j['mixins'][mixins], name, curr_depth=curr_depth + 1, role=role))

            def base_fac():
                base_name = role.split('.')[0]
                base_regexes = regexes_from_def_arr(j['roles'][base_name])
                base_keywords = keywords_from_def_arr(j['roles'][base_name])
                return f'({"|".join([*base_regexes, *base_keywords])})'

            for regexes in regexes:
                if re.fullmatch(render_template(regexes, base_fac=base_fac), name):
                    update_depth(curr_depth + 0.5)
            return depth
            
        role_weights = {k: get_role_weight(k) for k in j['roles'].keys()}

        def map_nickname_buf(name):
            hits: Dict[float, Set[tuple[str, float]]] = {}
            role_keys: list[str] = list(j['roles'].keys())
            for k, v in j['roles'].items():
                match_result = _match(v, name, role=k)
                # TODO: startswith的规则可能需要修改（获取自身和所有子级），应该改成判断子集的条件
                if k.startswith(f'{name}.') or k == name:
                    match_result = -1
                if match_result is not None:
                    if match_result not in hits:
                        hits[match_result] = set()
                    hits[match_result].add((k, role_weights[k]))
                    hits[match_result].update([(role_key, role_weights[role_key]) for role_key in role_keys if role_key.startswith(f'{k}.')])
            if len(hits) == 0:
                return name
            
            logger.debug(f'{hits=}')

            for item in sorted(hits.items()):
                return list(item[1])

        if author is not None:
            logger.debug(f'{author.display=}')

        if expr == '' and author is not None:
            at_map_dict = {
                1416248764: '纳延的腰子.target',
                1275645917: '纳延的猫条.target',
                3612795868: '纳延的小纳延.target',
                2627874128: '纳延的尾巴.target',
                3781281475: '纳延的肉垫.target',
            }

            if author.target in at_map_dict:
                expr = f'{at_map_dict[author.target]}'
                author = None

        if expr == '' and author is None:
            return


        logger.debug(f'{expr=}')
        raw_furs = expr.split('和')
        raw_furs = set(raw_furs)
        raw_furs = list(raw_furs)
        raw_furs.sort()

        fur_remains: dict[str, list[tuple[str, float]]] = {}

        def map_nickname_and_update_excludes(fur: str):
            pnps = fur_remains[fur]
            if len(pnps) == 0: raise AllFetchedException(f'已经看完{fur}的所有图片啦')
            mapped_name = random.choices([pnp[0] for pnp in pnps if pnp[0]], [pnp[1] for pnp in pnps if pnp[0]])[0]
            fur_remains[fur] = [i for i in fur_remains[fur] if i[0] != mapped_name]
            return mapped_name
        
        for fur in raw_furs:
            fur_remains[fur] = map_nickname_buf(fur)

        async def generate_image():
            furs = [map_nickname_and_update_excludes(fur) for fur in raw_furs]

            furs = list(set(furs))
            furs.sort()

            who = '&'.join(furs)
            who_nick = '&'.join([fur.split('.')[0] for fur in furs])

            if any(['纳延' in fur for fur in furs]):
                raise RuntimeError(f'建仓中。。。')

            if any(['毛线球' in fur for fur in furs]) and not await self.achv.is_used(FurAchv.NSFW):
                raise RuntimeError(f'请先佩戴称号"{FurAchv.NSFW.value.opts.display}"')

            async def post_process():
                if author is None and author_name == '灵翼':
                    await self.achv.submit(FurAchv.LING_YI)

                logger.debug(f'{who=}')
                if '.nsfw' in who:
                    await self.achv.submit(FurAchv.NSFW)

                if '.black' in who:
                    await self.achv.submit(FurAchv.BLACK)

                if '暗辰' in who:
                    await self.achv.submit(FurAchv.BAT)

                if '犯错' in who:
                    async def by(extra: 'AchvExtra'):
                        if extra.user_data is None:
                            extra.user_data = set()
                        extra.user_data.add(pathlib.PurePath(refer_image_file_path).stem)
                        extra.obtained_cnt = len(extra.user_data)
                    await self.achv.submit(FurAchv.FLAWLESS, by=by)
                    ...

                async def do_mute(time_s: int):
                    original_time_s = time_s

                    if mute_targets is not None: 
                        mans = [(self.gls_mute_man.get_or_create_data(group.id, mute_target), mute_target) for mute_target in mute_targets]
                    else:
                        mans = [(glse_gls_mute_man.get_or_create_data(), glse_gls_mute_man.member_id)]

                    remains_durations = []

                    for man, member_id in mans:
                        member = await self.member_from(member_id=member_id)
                        async with self.override(member):
                            try:
                                time_s = original_time_s

                                time_s *= factor
                                from plugins.admin import AdminAchv
                                if await self.achv.has(AdminAchv.ORIGINAL_SIN):
                                    time_s *= 10

                                if mute_targets is not None:
                                    time_s //= len(mute_targets)
                                    time_s = max(60, time_s)
                                
                                remains_duration = man.get_remains_duration()
                                remains_durations.append(remains_duration)
                                total = remains_duration * 4 + time_s
                                depth = man.update_mute(total)
                                logger.debug(f'mute -> {member_id}')
                                total = min(total, 30 * 24 * 60 * 60)

                                for mid in await self.admin.get_associated(member_id=member_id):
                                    await self.bot.mute(group.id, mid, total)

                                await self.achv.submit(FurAchv.BRIGHTLY_LIT)
                                if original_time_s >= 59 * 60 and await self.achv.has(FurAchv.HALF_FULL):
                                    await self.achv.submit(FurAchv.SUPERSATURATED_SOLUTION)
                                if original_time_s > 60 * 30:
                                    await self.achv.submit(FurAchv.HALF_FULL)
                                if depth == 5:
                                    await self.achv.submit(FurAchv.FORBIDDEN_QUINTET)
                            except: ...
                    return all([rd > 0 for rd in remains_durations])
                    
                skip_img = False

                if who == '灯泡':
                    skip_img = await do_mute(mute_logic.get_mute_duration())
                    if not skip_img:
                        commi_path = self.path.data['灯泡委托'][str(member_op.member.id)]
                        if os.path.exists(commi_path):
                            skip_img = random.choice([os.path.join(commi_path, p) for p in  os.listdir(commi_path)])
                        ...

                if who == '灯泡.escape':
                    await self.achv.submit(FurAchv.ESCAPE)
                
                if who == '灯泡.sun':
                    skip_img = await do_mute(100 * 60)
                    await self.achv.submit(FurAchv.SUN)

                if '灯泡' not in who and reset_cd:
                    await self.throttle.reset()

                return skip_img

            fur_path = self.path.data[who]

            if '灯泡' not in who and reset_cd:
                if not await self.throttle.do_associated():
                    return

            if not os.path.exists(fur_path):
                raise FurNotFoundException(f'没有找到{who_nick}的返图')
            
            author_folder_names = [
                author_folder_name 
                for author_folder_name 
                in os.listdir(fur_path)
                if os.path.isdir(os.path.join(fur_path, author_folder_name))
            ]

            tries_author_folder_name = set()

            skip_author_history_cond = len(author_folder_names) < self.FETCH_AUTHOR_HISTORY_SIZE or '.repeatable' in who

            img_file_cnt = 0
            for path, _, files in os.walk(fur_path):
                    img_file_cnt += len(files)

            skip_img_path_history_cond = img_file_cnt < self.FETCH_IMG_PATH_HISTORY_SIZE or '.repeatable' in who

            if skip_img_path_history_cond:
                lo_fetch_img_path_history = []
            else:
                lo_fetch_img_path_history = self.fetch_img_path_history

            if not skip_author_history_cond:
                for i in range(10):
                    if author is not None:
                        matched_folder_names = [name for name in author_folder_names if str(author.target) in name]
                        if len(matched_folder_names) == 0:
                            raise RuntimeError('未找到该作者的返图')
                        target_author_folder_name = random.choice(matched_folder_names)
                    else:
                        if who not in self.fetch_author_history:
                            self.fetch_author_history[who] = []
                        
                        if not skip_author_history_cond:
                            fetch_author_history_fur_sepc = self.fetch_author_history[who]
                        else:
                            fetch_author_history_fur_sepc = []
                        not_in_history_folder_names = [name for name in author_folder_names if name not in fetch_author_history_fur_sepc]
                        logger.debug(f'{not_in_history_folder_names=}')
                        if len(not_in_history_folder_names) > 0:
                            target_author_folder_name = random.choice(not_in_history_folder_names)
                            if not skip_author_history_cond:
                                fetch_author_history_fur_sepc.append(target_author_folder_name)
                            if len(fetch_author_history_fur_sepc) > self.FETCH_AUTHOR_HISTORY_SIZE:
                                fetch_author_history_fur_sepc.pop(0)
                        else:
                            target_author_folder_name = fetch_author_history_fur_sepc.pop(0)
                            if not skip_author_history_cond:
                                fetch_author_history_fur_sepc.append(target_author_folder_name)
                            logger.debug(f'refetch from history {target_author_folder_name=}')
                            ...

                    author_name, *autohr_id = target_author_folder_name.split('-')
                    autohr_id = int(autohr_id[0]) if autohr_id else None

                    # members = (await self.bot.member_list(event.group.id)).data
                    # member_ids = [member.id for member in members]

                    author_folder_path = os.path.join(fur_path, target_author_folder_name)
                    logger.debug(f'{author_folder_path=}')
                    try:
                        refer_image_file_path = random.choice([
                            path 
                            for name in os.listdir(author_folder_path) 
                            if (path := os.path.join(author_folder_path, name)) not in lo_fetch_img_path_history
                        ])
                    except IndexError:
                        tries_author_folder_name.add(target_author_folder_name)
                        if len(tries_author_folder_name) >= len(author_folder_names):
                            # raise RuntimeError(f'已经看完{who_nick}的所有图片啦')
                            raise PartialFetchedException(f'已经看完{who_nick}的所有图片啦')
                        if author is not None:
                            raise RuntimeError('该作者的返图全都看完啦')
                        continue
                    break
                else:
                    raise RuntimeError('==这里有bug但是不知道具体是什么==')
            else:
                # TODO: author_name

                if author is not None:
                    matched_folder_names = [name for name in author_folder_names if str(author.target) in name]
                    if len(matched_folder_names) == 0:
                        raise RuntimeError('未找到该作者的返图')
                    root_dir = random.choice(matched_folder_names)
                else:
                    root_dir = fur_path
                refer_image_file_paths = []
                for path, subdirs, files in os.walk(root_dir):
                    for name in files:
                        if name.startswith('.'):
                            continue
                        refer_image_file_paths.append(os.path.join(path, name))
                refer_image_file_path = random.choice([rifp for rifp in refer_image_file_paths if rifp not in lo_fetch_img_path_history])
                pure_path = pathlib.PurePath(refer_image_file_path)
                author_name, *autohr_id = pure_path.parent.name.split('-')
                autohr_id = int(autohr_id[0]) if autohr_id else None

            if not skip_img_path_history_cond:
                self.fetch_img_path_history.append(refer_image_file_path)
            if len(self.fetch_img_path_history) > self.FETCH_IMG_PATH_HISTORY_SIZE:
                self.fetch_img_path_history.pop(0)

            ext_name = os.path.splitext(refer_image_file_path)[-1][1:]

            if ext_name in ('txt',):
                with open(refer_image_file_path, "rt", encoding='utf-8') as txt_file:
                    skip_img = await post_process()
                    if skip_img: return
                    return [txt_file.read()]
                
            target_image_file_path = refer_image_file_path

            refer_img = Image.open(refer_image_file_path)
            width, height = refer_img.size

            # 2160
            if width > 1080 or height > 1080:
                refer_img.thumbnail((1080,1080), Image.ANTIALIAS)
                target_image_file_name = str(uuid.uuid4())
                target_image_file_path = self.path.data.cache.of_file(target_image_file_name)
                # 95
                refer_img.convert('RGB').save(target_image_file_path, 'JPEG', quality=85)

            logger.debug(f'{refer_image_file_path=}')

            with open(target_image_file_path, "rb") as image_file:
                b64_input = base64.b64encode(image_file.read()).decode('utf-8')

            what = imghdr.what(target_image_file_path)
            
            if what == 'gif':
                b64_img = b64_input
            else:
                exif = {}
                if refer_img._getexif() is not None:
                    exif = { ExifTags.TAGS[k]: v for k, v in refer_img._getexif().items() if k in ExifTags.TAGS }

                logger.debug(exif)

                b64_url = f'data:image/{what};base64,{b64_input}'

                key_renames = {
                    'Make': 'make',
                    'Model': 'model',
                    'DateTimeOriginal': 'date_time',
                    'FocalLength': 'focal_length',
                    'FNumber': 'f_number',
                    'ExposureTime': 'exposure_time',
                    'ISOSpeedRatings': 'iso_speed_ratings'
                }

                logger.debug({key_renames[key]: exif[key] for key in exif if key in key_renames})

                b64_img = await self.renderer.render('pic_details', data={
                    'img_url': b64_url,
                    'author': author_name,
                    'exif': {key_renames[key]: float(exif[key]) if isinstance(exif[key], TiffImagePlugin.IFDRational) else exif[key] for key in exif if key in key_renames}
                })

            skip_img = await post_process()
            if skip_img: 
                if isinstance(skip_img, str):
                    return [
                        mirai.models.message.Image(path=skip_img)
                    ]
                return
            return [
                mirai.models.message.Image(base64=b64_img)
            ]
        
        async def send_image():
            c = await generate_image()
            if c is not None:
                resp = await source.op.send(c)
                fur_pic_msg_man.records.append(
                    FurPicMsgRecord(msg_id=resp.message_id, source_id=source.get_target_id())
                )
        
        # [".*?酒.*?${cat}?"]
        
        if len(raw_furs) > 1:
            await send_image()
            return
        
        if(len(raw_furs) == 1):
            while True:
                try:
                    await send_image()
                    return
                except PartialFetchedException:
                    logger.warning('partial fetched...')
                except FurNotFoundException as e:
                    reason = f'返图未命中'
                    await self.admin.inc_violation_cnt(reason=reason, hint=reason)
                    return
        ...

    

            


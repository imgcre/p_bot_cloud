import asyncio
import base64
from collections import OrderedDict
from dataclasses import dataclass
from decimal import Decimal
from io import BytesIO
import random
import re
from pathlib import Path
from typing import Final,  Optional
from event_types import EffectiveSpeechEvent
from mirai import At, GroupMessage, Image, MessageEvent, Plain
from plugin import Plugin, any_instr, delegate, InstrAttr, route, enable_backup, Inject
from plugins.code_highlight import get_code_lexer
from mirai.models.entities import GroupMember

from typing import TYPE_CHECKING

from utilities import AchvRarity, AchvOpts, GroupLocalStorage, Source, SourceOp, VoucherRecordExtraAi, AchvEnum, throttle_config
if TYPE_CHECKING:
    from plugins.achv import Achv
    from plugins.events import Events
    from plugins.gpt import Gpt
    from plugins.admin import Admin
    from plugins.throttle import Throttle
    from plugins.voucher import Voucher
    from plugins.web_share import WebShare

class AiExtAchv(AchvEnum):
    AI_COOLDOWN = 0, 'AI冷却中', '主动和bot对话功能在冷却状态下时自动获得', AchvOpts(display_pinned=True, locked=True, hidden=True, display='🆒', display_weight=-1)
    EDGE = 1, '边缘', '在AI的CD只有一分钟时发起对话获得', AchvOpts(rarity=AchvRarity.UNCOMMON, display='⌛', custom_obtain_msg='差点就……')

@dataclass
class CustomMan():
    affection: int = 50

@route('AI拓展')
@enable_backup
class AiExt(Plugin):
    gls_custom: GroupLocalStorage[CustomMan] = GroupLocalStorage[CustomMan]()

    achv: Inject['Achv']
    events: Inject['Events']
    gpt: Inject['Gpt']
    admin: Inject['Admin']
    throttle: Inject['Throttle']
    voucher: Inject['Voucher']
    web_share: Inject['WebShare']

    # 单位是秒
    SPEEDUP_LOOKUP: Final = {
        AchvRarity.COMMOM: 30 * 60,
        AchvRarity.UNCOMMON: 10 * 60,
        AchvRarity.RARE: 5 * 60,
        AchvRarity.EPIC: 2 * 60,
    }

    SPEEDUP_EFFECTIVE_SPEECH: Final = 10 * 60

    MAX_BREAKDOWN: Final = 5
    AI_MSG_BREAK: Final = '<<PBOT_MSG_BREAK>>'
    MAX_AI_MESSAGE_CHARS: Final = 500

    MIN_BREAKDOWN_PROB_WORDS: Final = 5
    MAX_BREAKDOWN_PROB_WORDS: Final = 30
    RICH_IMAGE_CACHE_SIZE: Final = 64

    def __init__(self):
        self.ai_resp_msg_ids: list[int] = []
        self.rich_image_cache = OrderedDict()

    @delegate()
    async def chat(self, event: GroupMessage, op: SourceOp, *, msg: list):
        res = await self.gpt.response_with_ai(msg=msg)
        if res is not None:
            await self.bot.send(event, res)

    @throttle_config(name='AI', achv_speedup=True, effective_speedup=True, enable_min_duration=True)
    async def check_avaliable(self, *, recall: bool=False):
        cooldown_reamins = await self.throttle.get_cooldown_reamins()
        use_min_duration = await self.throttle.is_use_min_duration()

        await self.voucher.adjust(
            cnt=Decimal('-0.2'), 
            extra=VoucherRecordExtraAi()
        )

        if recall and not not use_min_duration and cooldown_reamins > 0 and cooldown_reamins < 60:
            await self.achv.submit(AiExtAchv.EDGE)
        return await self.throttle.do_associated(recall=recall, cooldown_reamins=cooldown_reamins)

    def flatten(self, r: list, f: list = None):
        if f is None:
            f = []
        for e in r:
            if isinstance(e, list):
                self.flatten(e, f)
            else:
                f.append(e)
        return f

    def breakdown_r(self, root: list):
        root = [s for s in root if not isinstance(s, str) or len(s) > 2]

        if len(root) >= self.MAX_BREAKDOWN:
            return root
        
        str_len = [len(s) if isinstance(s, str) else 0 for s in root]
        if len(str_len) == 0:
            return root
        
        index_max = max(range(len(str_len)), key=str_len.__getitem__)
        
        len_max = str_len[index_max]
        breakdown_prob = (len_max - self.MIN_BREAKDOWN_PROB_WORDS) / (self.MAX_BREAKDOWN_PROB_WORDS - self.MIN_BREAKDOWN_PROB_WORDS)
        will_breakdown = random.random() < breakdown_prob

        if not will_breakdown: return root

        txt: str = root[index_max]

        for rexpr in [r'\n+', r'(?<=!|！|。)', r'(?<=~)', r',|，']:
            splited_by = re.split(rexpr, txt)
            striped = [t.strip() for t in splited_by]
            filtered = [t for t in striped if len(t) > 0]
            if len(filtered) > 1 and len(root) - 1 + len(filtered) <= self.MAX_BREAKDOWN:
                root[index_max] = filtered
                root = self.flatten(root)
                return self.breakdown_r(root)
            
        return root

    def _get_cached_rich_image(self, key):
        b64_img = self.rich_image_cache.get(key)
        if b64_img is None:
            return None
        self.rich_image_cache.move_to_end(key)
        return Image(base64=b64_img)

    def _put_cached_rich_image(self, key, b64_img):
        self.rich_image_cache[key] = b64_img
        self.rich_image_cache.move_to_end(key)
        while len(self.rich_image_cache) > self.RICH_IMAGE_CACHE_SIZE:
            self.rich_image_cache.popitem(last=False)
        return Image(base64=b64_img)

    def _get_code_font_name(self):
        font_names = [
            'Sarasa Mono SC',
            'Noto Sans Mono CJK SC',
            'Noto Sans Mono CJK',
        ]
        for font_name in font_names:
            try:
                from pygments.formatters.img import FontManager
                FontManager(font_name, 28)
                return font_name
            except Exception:
                ...

        font_paths = [
            '/usr/local/share/fonts/sarasa/SarasaMonoSC-Regular.ttf',
            '/usr/local/share/fonts/sarasa/SarasaMonoSC-Bold.ttf',
            '/usr/share/fonts/truetype/noto/NotoSansSC-Regular.otf',
            '/usr/share/fonts/truetype/wqy/wqy-microhei.ttc',
            r'C:\Windows\Fonts\NotoSansSC-VF.ttf',
            r'C:\Windows\Fonts\msyh.ttc',
            r'C:\Windows\Fonts\simhei.ttf',
            '/usr/share/fonts/truetype/droid/DroidSansFallbackFull.ttf',
        ]
        for font_path in font_paths:
            if Path(font_path).exists():
                return font_path
        return 'Droid Sans Fallback'

    async def _render_code_image(self, code: str, lang: str = ''):
        font_name = self._get_code_font_name()
        key = ('code', 'cjk-font-v3-qr', font_name, lang, code)
        cached = self._get_cached_rich_image(key)
        if cached is not None:
            return cached

        from pygments import highlight
        from pygments.formatters import ImageFormatter
        lexer = get_code_lexer(code, lang)

        formatter = ImageFormatter(
            style='one-dark',
            line_numbers=False,
            font_name=font_name,
            font_size=28,
            line_pad=6,
            image_pad=24,
        )
        png_bytes = highlight(code.rstrip('\n') or ' ', lexer, formatter)
        share = await self.web_share.create_code_share(code, lang=lang, source='ai_ext')
        png_bytes = self.web_share.append_qr_to_png(png_bytes, share.url)
        b64_img = base64.b64encode(png_bytes).decode('ascii')
        return self._put_cached_rich_image(key, b64_img)

    def _get_math_font_properties(self):
        from matplotlib import font_manager, rcParams

        font_paths = [
            '/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc',
            '/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.otf',
            '/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc',
            '/usr/share/fonts/truetype/noto/NotoSansSC-Regular.otf',
            '/usr/share/fonts/truetype/wqy/wqy-microhei.ttc',
            r'C:\Windows\Fonts\NotoSansSC-VF.ttf',
            r'C:\Windows\Fonts\msyh.ttc',
            r'C:\Windows\Fonts\simhei.ttf',
        ]
        for font_path in font_paths:
            if Path(font_path).exists():
                try:
                    font_manager.fontManager.addfont(font_path)
                except Exception:
                    ...
                prop = font_manager.FontProperties(fname=font_path)
                rcParams['font.sans-serif'] = [prop.get_name(), 'DejaVu Sans']
                rcParams['axes.unicode_minus'] = False
                return prop

        font_names = [
            'Noto Sans CJK SC',
            'Noto Sans SC',
            'Source Han Sans SC',
            'WenQuanYi Micro Hei',
            'Microsoft YaHei',
            'SimHei',
        ]
        for font_name in font_names:
            try:
                font_path = font_manager.findfont(font_name, fallback_to_default=False)
                prop = font_manager.FontProperties(fname=font_path)
                rcParams['font.sans-serif'] = [prop.get_name(), 'DejaVu Sans']
                rcParams['axes.unicode_minus'] = False
                return prop
            except Exception:
                ...

        rcParams['font.sans-serif'] = ['DejaVu Sans']
        rcParams['axes.unicode_minus'] = False
        return font_manager.FontProperties(family='DejaVu Sans')

    def _get_math_body(self, formula: str):
        body = formula.strip()
        if body.startswith('$$') and body.endswith('$$'):
            body = body[2:-2].strip()
        elif body.startswith(r'\[') and body.endswith(r'\]'):
            body = body[2:-2].strip()
        elif body.startswith(r'\(') and body.endswith(r'\)'):
            body = body[2:-2].strip()
        elif body.startswith('$') and body.endswith('$'):
            body = body[1:-1].strip()
        return body

    def _plainify_simple_math(self, body: str):
        replacements = {
            r'^{\circ}': '°',
            r'^\circ': '°',
            r'\triangle': 'Δ',
            r'\Delta': 'Δ',
            r'\angle': '∠',
            r'\measuredangle': '∠',
            r'\cdot': '⋅',
            r'\times': '×',
            r'\div': '÷',
            r'\pm': '±',
            r'\mp': '∓',
            r'\parallel': '||',
            r'\perp': '⊥',
            r'\circ': '°',
            r'\degree': '°',
            r'\deg': '°',
            r'\leq': '≤',
            r'\le': '≤',
            r'\geq': '≥',
            r'\ge': '≥',
            r'\neq': '≠',
            r'\ne': '≠',
            r'\approx': '≈',
            r'\sim': '∼',
            r'\cong': '≅',
            r'\equiv': '≡',
            r'\implies': '=>',
            r'\infty': '∞',
            r'\alpha': 'α',
            r'\beta': 'β',
            r'\gamma': 'γ',
            r'\delta': 'δ',
            r'\epsilon': 'ε',
            r'\theta': 'θ',
            r'\lambda': 'λ',
            r'\mu': 'μ',
            r'\pi': 'π',
            r'\rho': 'ρ',
            r'\sigma': 'σ',
            r'\phi': 'φ',
            r'\omega': 'ω',
            r'\Gamma': 'Γ',
            r'\Theta': 'Θ',
            r'\Lambda': 'Λ',
            r'\Pi': 'Π',
            r'\Sigma': 'Σ',
            r'\Phi': 'Φ',
            r'\Omega': 'Ω',
        }
        for source, target in replacements.items():
            body = body.replace(source, target)

        superscripts = {
            '0': '⁰',
            '1': '¹',
            '2': '²',
            '3': '³',
            '4': '⁴',
            '5': '⁵',
            '6': '⁶',
            '7': '⁷',
            '8': '⁸',
            '9': '⁹',
            '+': '⁺',
            '-': '⁻',
        }
        body = re.sub(r'\^\{([0-9+-])\}', lambda m: superscripts[m.group(1)], body)
        body = re.sub(r'\^([0-9+-])', lambda m: superscripts[m.group(1)], body)
        body = re.sub(r'\s+', ' ', body).strip()

        structural_pattern = re.compile(
            r'\\[a-zA-Z]+|[{}_^]|\\[()[\]]|\\begin|\\end|\\frac|\\sqrt|\\sum|\\int|\\prod|\\lim'
        )
        if structural_pattern.search(body):
            return None
        return body

    def _normalize_mathtext(self, body: str):
        replacements = {
            r'\implies': r'\Rightarrow',
            r'\ge': r'\geq',
            r'\le': r'\leq',
        }
        for source, target in replacements.items():
            body = body.replace(source, target)
        return body

    def _math_span_pattern(self):
        return re.compile(
            r'(\$\$.*?\$\$|\\\[.*?\\\]|\\\(.*?\\\)|(?<!\\)\$(?!\$).+?(?<!\\)\$)',
            re.DOTALL,
        )

    def _is_block_math_formula(self, formula: str):
        stripped = formula.strip()
        return (stripped.startswith('$$') and stripped.endswith('$$')) or (
            stripped.startswith(r'\[') and stripped.endswith(r'\]')
        )

    def _split_block_math_lines(self, body: str):
        body = self._normalize_mathtext(body)
        lines = re.split(r'\\\\|\n+', body)
        normalized = []
        for line in lines:
            line = line.strip()
            if not line:
                continue
            line = line.lstrip('&').replace('&', '')
            normalized.append(line)
        return normalized

    def _math_body_chunks(self, body: str):
        body = self._normalize_mathtext(body)
        chunks = []
        last = 0
        for match in re.finditer(r'\\text\{([^{}]*)\}', body):
            if match.start() > last:
                chunks.append(('math', body[last:match.start()]))
            if match.group(1):
                chunks.append(('text', match.group(1)))
            last = match.end()
        if last < len(body):
            chunks.append(('math', body[last:]))

        split_chunks = []
        cjk_pattern = re.compile(r'([\u3400-\u9fff\u3040-\u30ff\uac00-\ud7af]+)')
        for kind, content in chunks:
            if kind != 'math':
                split_chunks.append((kind, content))
                continue
            for idx, part in enumerate(cjk_pattern.split(content)):
                if not part:
                    continue
                split_chunks.append(('text' if idx % 2 else 'math', part))
        return [(kind, content) for kind, content in split_chunks if content]

    def _looks_like_math_process(self, text: str):
        stripped = text.strip()
        if not stripped or self.AI_MSG_BREAK in stripped:
            return False

        matches = list(self._math_span_pattern().finditer(stripped))
        if not matches:
            return False

        lines = [line for line in stripped.splitlines() if line.strip()]
        formula_count = len(matches)
        relation_count = len(re.findall(
            r'=|≤|≥|=>|\\Rightarrow|\\leq?|\\geq?|因此|所以|于是|因为|故',
            stripped,
        ))
        step_marker_count = len(re.findall(
            r'(^|\n)\s*(?:\d+[.、)]|[-*]\s|步骤|证明|求证|解[:：]|证[:：]|由|所以|因此|于是|故)',
            stripped,
        ))
        has_process_keyword = any(keyword in stripped for keyword in [
            '证明', '求证', '推导', '求解', '解法', '化简', '代入', '联立',
        ])

        for match in matches:
            formula = match.group(0)
            body = self._get_math_body(formula)
            if self._is_block_math_formula(formula) and ('\n' in body or r'\\' in body):
                return len(lines) >= 2 or relation_count >= 2

        if formula_count >= 3 and (len(lines) >= 2 or relation_count >= 3):
            return True
        if formula_count >= 2 and len(lines) >= 3 and (relation_count >= 2 or step_marker_count >= 2):
            return True
        return formula_count >= 2 and has_process_keyword and relation_count >= 2

    def _append_math_process_plain(self, lines: list, current_line: list, text: str):
        parts = text.split('\n')
        for idx, part in enumerate(parts):
            if idx > 0:
                lines.append(current_line)
                current_line = []
            if part:
                current_line.append(('text', part))
        return current_line

    def _math_process_lines(self, text: str):
        lines = []
        current_line = []
        last = 0
        for match in self._math_span_pattern().finditer(text):
            current_line = self._append_math_process_plain(lines, current_line, text[last:match.start()])
            formula = match.group(0)
            body = self._get_math_body(formula)
            if self._is_block_math_formula(formula):
                if current_line:
                    lines.append(current_line)
                    current_line = []
                for math_line in self._split_block_math_lines(body):
                    lines.append(self._math_body_chunks(math_line))
            else:
                current_line.extend(self._math_body_chunks(body))
            last = match.end()

        current_line = self._append_math_process_plain(lines, current_line, text[last:])
        if current_line:
            lines.append(current_line)
        return lines

    def _measure_math_lines(self, fig, canvas, lines, font_prop, text_font_size, math_font_size):
        line_metrics = []
        for line in lines:
            line_width = 0
            line_height = 0
            chunk_metrics = []
            for kind, content in line:
                is_math = kind == 'math'
                text = f'${content}$' if is_math else content
                kwargs = {
                    'fontsize': math_font_size if is_math else text_font_size,
                    'color': '#abb2bf',
                    'ha': 'left',
                    'va': 'top',
                    'parse_math': is_math,
                }
                if not is_math:
                    kwargs['fontproperties'] = font_prop
                artist = fig.text(0, 0, text, **kwargs)
                canvas.draw()
                bbox = artist.get_window_extent(canvas.get_renderer())
                artist.remove()
                width = bbox.width
                height = bbox.height
                chunk_metrics.append((width, height))
                line_width += width
                line_height = max(line_height, height)
            if not line:
                line_height = text_font_size * fig.dpi / 72
            line_metrics.append((line_width, line_height, chunk_metrics))
        return line_metrics

    def _render_math_lines_image(self, lines: list, key):
        font_prop = self._get_math_font_properties()
        key = (*key, font_prop.get_name())
        cached = self._get_cached_rich_image(key)
        if cached is not None:
            return cached

        from matplotlib.backends.backend_agg import FigureCanvasAgg
        from matplotlib.figure import Figure

        dpi = 180
        text_font_size = 15
        math_font_size = 15
        measure_fig = Figure(figsize=(0.01, 0.01), dpi=dpi)
        measure_canvas = FigureCanvasAgg(measure_fig)
        line_metrics = self._measure_math_lines(
            measure_fig,
            measure_canvas,
            lines,
            font_prop,
            text_font_size,
            math_font_size,
        )

        margin_x = 24
        margin_y = 22
        line_gap = 9
        width_px = max(1, int(max((metric[0] for metric in line_metrics), default=1) + margin_x * 2))
        height_px = int(
            sum(metric[1] for metric in line_metrics)
            + max(0, len(line_metrics) - 1) * line_gap
            + margin_y * 2
        )

        fig = Figure(figsize=(width_px / dpi, height_px / dpi), dpi=dpi)
        fig.patch.set_facecolor('#282c34')
        canvas = FigureCanvasAgg(fig)

        y = margin_y
        for line, (_, line_height, chunk_metrics) in zip(lines, line_metrics):
            x = margin_x
            for (kind, content), (chunk_width, _) in zip(line, chunk_metrics):
                is_math = kind == 'math'
                kwargs = {
                    'fontsize': math_font_size if is_math else text_font_size,
                    'color': '#abb2bf',
                    'ha': 'left',
                    'va': 'top',
                    'parse_math': is_math,
                }
                if not is_math:
                    kwargs['fontproperties'] = font_prop
                fig.text(
                    x / width_px,
                    1 - y / height_px,
                    f'${content}$' if is_math else content,
                    **kwargs,
                )
                x += chunk_width
            y += line_height + line_gap

        buffered = BytesIO()
        fig.savefig(buffered, format='png', transparent=False, facecolor=fig.get_facecolor())
        b64_img = base64.b64encode(buffered.getvalue()).decode('ascii')
        return self._put_cached_rich_image(key, b64_img)

    def _render_math_process_image(self, text: str):
        text = text.strip()
        lines = self._math_process_lines(text)
        if not lines:
            return text
        return self._render_math_lines_image(lines, ('math-process', 'v2-cjk-mathtext', text))

    def _render_math_image(self, formula: str):
        body = self._get_math_body(formula)

        if not body:
            return formula
        plain_body = self._plainify_simple_math(body)
        if plain_body is not None:
            return plain_body
        body = self._normalize_mathtext(body)
        chunks = self._math_body_chunks(body)
        if len(chunks) > 1 or any(kind == 'text' for kind, _ in chunks):
            return self._render_math_lines_image([chunks], ('math', 'v3-mixed-cjk-mathtext', body))

        key = ('math', 'v2-half-size', body)
        cached = self._get_cached_rich_image(key)
        if cached is not None:
            return cached

        from matplotlib.backends.backend_agg import FigureCanvasAgg
        from matplotlib.figure import Figure

        font_prop = self._get_math_font_properties()
        fig = Figure(figsize=(0.01, 0.01), dpi=180)
        fig.patch.set_facecolor('#282c34')
        canvas = FigureCanvasAgg(fig)
        text = fig.text(0, 0, f'${body}$', fontsize=15, color='#abb2bf', fontproperties=font_prop)
        canvas.draw()
        bbox = text.get_window_extent(canvas.get_renderer()).expanded(1.08, 1.35)

        buffered = BytesIO()
        fig.savefig(
            buffered,
            format='png',
            transparent=False,
            facecolor=fig.get_facecolor(),
            bbox_inches=bbox.transformed(fig.dpi_scale_trans.inverted()),
            pad_inches=0.015,
        )
        b64_img = base64.b64encode(buffered.getvalue()).decode('ascii')
        return self._put_cached_rich_image(key, b64_img)

    def _split_math_images(self, text: str):
        if self.AI_MSG_BREAK in text:
            parts = []
            for idx, chunk in enumerate(text.split(self.AI_MSG_BREAK)):
                if idx > 0:
                    parts.append(self.AI_MSG_BREAK)
                parts.extend(self._split_math_images(chunk))
            return parts

        if self._looks_like_math_process(text):
            try:
                return [self._render_math_process_image(text)]
            except Exception:
                ...

        math_pattern = self._math_span_pattern()
        parts = []
        last = 0
        for match in math_pattern.finditer(text):
            if match.start() > last:
                parts.append(text[last:match.start()])
            formula = match.group(0)
            try:
                parts.append(self._render_math_image(formula))
            except Exception:
                parts.append(formula)
            last = match.end()
        if last < len(text):
            parts.append(text[last:])
        return parts

    async def _render_rich_text_images(self, text: str):
        code_pattern = re.compile(r'```([^\n`]*)\n?(.*?)```', re.DOTALL)
        parts = []
        last = 0
        for match in code_pattern.finditer(text):
            if match.start() > last:
                parts.extend(self._split_math_images(text[last:match.start()]))
            lang = match.group(1).strip()
            code = match.group(2)
            try:
                parts.append(await self._render_code_image(code, lang))
            except Exception:
                parts.append(match.group(0))
            last = match.end()
        if last < len(text):
            parts.extend(self._split_math_images(text[last:]))
        return [p for p in parts if not isinstance(p, str) or len(p) > 0]

    async def _render_rich_chain_images(self, mc: list):
        rendered = []
        for e in mc:
            if isinstance(e, str):
                rendered.extend(await self._render_rich_text_images(e))
            else:
                rendered.append(e)
        return rendered

    async def render_rich_chain_images(self, mc: list):
        return await self._render_rich_chain_images(mc)

    def _chain_text_len(self, mc: list):
        return sum(len(e) for e in mc if isinstance(e, str))

    def _normalize_message_chain(self, mc: list):
        normalized = []
        for idx, e in enumerate(mc):
            if isinstance(e, str):
                if idx == 0 or all(not isinstance(item, str) or len(item.strip()) == 0 for item in mc[:idx]):
                    e = re.sub(r'^[\s，,。]+', '', e)
                if idx == len(mc) - 1 or all(not isinstance(item, str) or len(item.strip()) == 0 for item in mc[idx + 1:]):
                    e = re.sub(r'[\s]+$', '', e)
                if len(e) == 0:
                    continue
            normalized.append(e)
        return normalized

    def _split_chain_by_breaks(self, mc: list):
        messages = [[]]
        found_break = False
        for e in mc:
            if not isinstance(e, str):
                messages[-1].append(e)
                continue

            parts = e.split(self.AI_MSG_BREAK)
            for idx, part in enumerate(parts):
                if idx > 0:
                    found_break = True
                    messages.append([])
                if part:
                    messages[-1].append(part)

        messages = [self._normalize_message_chain(m) for m in messages]
        messages = [m for m in messages if len(m) > 0]
        return messages, found_break

    def _find_fallback_break(self, text: str):
        limit = self.MAX_AI_MESSAGE_CHARS
        if len(text) <= limit:
            return len(text)

        candidates = ['\n', '。', '！', '？', '!', '?', '，', ',', '、', ' ']
        for sep in candidates:
            idx = text.rfind(sep, 0, limit + 1)
            if idx > 0:
                return idx + len(sep)
        return limit

    def _split_text_by_length(self, text: str):
        parts = []
        rest = text
        while len(rest) > self.MAX_AI_MESSAGE_CHARS:
            idx = self._find_fallback_break(rest)
            part = rest[:idx].strip()
            if part:
                parts.append(part)
            rest = rest[idx:].strip()
        if rest:
            parts.append(rest)
        return parts

    def _split_chain_by_length(self, mc: list):
        messages = [[]]
        current_len = 0
        for e in mc:
            if isinstance(e, str):
                for part in self._split_text_by_length(e):
                    if current_len > 0 and current_len + len(part) > self.MAX_AI_MESSAGE_CHARS:
                        messages.append([])
                        current_len = 0
                    messages[-1].append(part)
                    current_len += len(part)
            else:
                messages[-1].append(e)

        messages = [self._normalize_message_chain(m) for m in messages]
        return [m for m in messages if len(m) > 0]

    async def send_chat_segments(self, op: SourceOp, *, mc: list):
        mc = await self._render_rich_chain_images(mc)
        messages, found_break = self._split_chain_by_breaks(mc)
        if not found_break:
            messages = self._split_chain_by_length(mc)

        for i, message in enumerate(messages):
            if i != 0:
                text_len = self._chain_text_len(message)
                await asyncio.sleep(min(0.2 + text_len / 7, 3) if text_len > 0 else 1 + random.random())
            resp = await op.send(message)
            self.ai_resp_msg_ids.append(resp.message_id)

    @delegate(InstrAttr.BACKGROUND)
    async def as_chat_seq(self, op: SourceOp, *, mc: list):
        # 咱也一样... 不过没关系，咱可以攒着下次用嘛~ 下次就能用闪电五连鞭连抽五次啦！
        await self.send_chat_segments(op, mc=mc)
    
    def is_chat_seq_msg(self, msg_id: int):
        return msg_id in self.ai_resp_msg_ids

    async def mark_invoked(self):
        await self.throttle.reset(fn=self.check_avaliable)
        await self.achv.submit(AiExtAchv.AI_COOLDOWN, silent=True)

    @any_instr()
    async def update_cd_state(self):
        try:
            cooldown_reamins = await self.throttle.get_cooldown_reamins(fn=self.check_avaliable)
            if cooldown_reamins <= 0:
                await self.achv.remove(AiExtAchv.AI_COOLDOWN, force=True)
        except: ...

    @delegate()
    async def get_affection(self, man: CustomMan):
        return man.affection

    @delegate()
    async def increase_affection(self, man: CustomMan):
        await self.set_affection(val=man.affection + 1, text='提升')
    
    @delegate()
    async def decrease_affection(self, man: CustomMan):
        await self.set_affection(val=man.affection - 1, text='降低')

    @delegate()
    async def set_affection(self, source: Optional[Source], event: MessageEvent, man: CustomMan, *, val: int, text: str):
        for c in event.message_chain:
            if isinstance(c, Plain):
                if '好感' in c.text:
                    return
        try:
            if source is not None:
                man.affection = val
                self.backup_man.set_dirty()
                await source.op.send(['对', At(target=source.member.id), f' 的好感度{text}了!'])
        except: ...

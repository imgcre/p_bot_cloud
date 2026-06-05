import asyncio
import atexit
import io
import gc
import contextlib
import mimetypes
import os
from mirai import Image
from plugin import InstrAttr, Plugin, autorun, delegate, enable_backup, route
from pyppeteer import launch
import urllib.parse
import uuid
import json
import time
import base64
import statistics
from PIL import Image as PILImage

from utilities import SourceOp, get_logger

logger = get_logger()

# ./.vscode/settings.json ["terminal.integrated.env.windows"]
# $env:PYPPETEER_CHROMIUM_REVISION=1226537
# https://vikyd.github.io/download-chromium-history-version/#/

@route('渲染')
# @enable_backup
class Renderer(Plugin):
    api_base: str = 'http://localhost:4399/' # D:\projects\js\p-bot-fe
    render_scale: float = 2
    max_animation_duration: float = 6
    max_animation_frames: int = 90
    max_animation_dimension: int = 960
    browser_restart_render_count: int = 80
    browser_restart_rss_mb: int = 512

    def __init__(self):
        self.render_lock = asyncio.Lock()
        self.browser = None
        self.browser_render_count = 0
        self._atexit_registered = False

    @autorun
    async def startup(self):
        ...
        self.render_lock = asyncio.Lock()
        await self._ensure_browser()
        if not self._atexit_registered:
            atexit.register(self._close_browser_at_exit)
            self._atexit_registered = True

    def _browser_args(self):
        return [
            '--headless',
            '--disable-web-security',
            # '--enable-gpu',
            '--no-sandbox',
            '--use-gl=angle',
            '--use-angle=gl',
            '--enable-unsafe-webgpu',
            '--disable-dev-shm-usage',
            '--disable-setuid-sandbox',
            '--disable-features=IsolateOrigins',
            '--disable-site-isolation-trials',
            '--hide-scrollbars',
            #'--single-process',
            #'--in-process-gpu',
            '--disable-gpu',
            '--disable-dev-shm-usage',
            #'--disable-extensions',
            #'--enable-features=NetworkServiceInProcess',
            #'--no-first-run',
            #'--disable-sync',
            #'--disable-background-networking',
            # '--autoplay-policy=no-user-gesture-required',
        ]

    async def _launch_browser(self):
        self.browser = await launch(
            headless=False,
            executablePath=r'/usr/bin/chromium',
            args=self._browser_args()
        )
        self.browser_render_count = 0

    def _browser_alive(self):
        if self.browser is None:
            return False
        process = getattr(self.browser, 'process', None)
        if process is None:
            return True
        poll = getattr(process, 'poll', None)
        return poll is None or poll() is None

    async def _ensure_browser(self):
        if not self._browser_alive():
            await self._launch_browser()

    async def _close_browser(self):
        browser = self.browser
        self.browser = None
        if browser is None:
            return
        with contextlib.suppress(Exception):
            await browser.close()

    def _close_browser_at_exit(self):
        browser = getattr(self, 'browser', None)
        if browser is None:
            return
        self.browser = None
        try:
            loop = asyncio.new_event_loop()
            try:
                loop.run_until_complete(browser.close())
            finally:
                loop.close()
            return
        except Exception:
            process = getattr(browser, 'process', None)
            if process is not None and getattr(process, 'poll', lambda: None)() is None:
                with contextlib.suppress(Exception):
                    process.terminate()

    def _browser_root_pid(self):
        if self.browser is None:
            return None
        process = getattr(self.browser, 'process', None)
        if process is None:
            return None
        return getattr(process, 'pid', None)

    def _browser_tree_rss_kb(self):
        root_pid = self._browser_root_pid()
        if root_pid is None or not os.path.isdir('/proc'):
            return 0

        children = {}
        rss_by_pid = {}
        for name in os.listdir('/proc'):
            if not name.isdigit():
                continue
            pid = int(name)
            status_path = os.path.join('/proc', name, 'status')
            ppid = None
            rss = 0
            try:
                with open(status_path, encoding='utf-8') as status_file:
                    for line in status_file:
                        if line.startswith('PPid:'):
                            ppid = int(line.split()[1])
                        elif line.startswith('VmRSS:'):
                            rss = int(line.split()[1])
            except OSError:
                continue
            rss_by_pid[pid] = rss
            if ppid is not None:
                children.setdefault(ppid, []).append(pid)

        total_rss = 0
        stack = [root_pid]
        seen = set()
        while stack:
            pid = stack.pop()
            if pid in seen:
                continue
            seen.add(pid)
            total_rss += rss_by_pid.get(pid, 0)
            stack.extend(children.get(pid, []))
        return total_rss

    async def _restart_browser_if_needed(self):
        self.browser_render_count += 1
        rss_kb = self._browser_tree_rss_kb()
        over_count = self.browser_render_count >= self.browser_restart_render_count
        over_memory = (
            rss_kb > 0 and rss_kb >= self.browser_restart_rss_mb * 1024
        )
        if not over_count and not over_memory:
            return
        logger.info(
            f'restart chromium: {self.browser_render_count=}, '
            f'browser_rss_mb={rss_kb / 1024:.1f}'
        )
        await self._close_browser()
        gc.collect()
        await self._launch_browser()

    def local_file_url(self, file_path: str, api_base=None):
        if api_base is None:
            api_base = self.api_base
        token = uuid.uuid4().hex
        filename = urllib.parse.quote(os.path.basename(file_path))
        return urllib.parse.urljoin(api_base, f'__pbot_local_file__/{token}/{filename}')

    async def _install_local_file_interceptor(self, page, local_files: dict[str, str]):
        if not local_files:
            return
        await page.setRequestInterception(True)

        async def handle_request(req):
            local_path = local_files.get(req.url)
            if local_path is None:
                await req.continue_()
                return
            content_type = mimetypes.guess_type(local_path)[0] or 'application/octet-stream'
            with open(local_path, 'rb') as local_file:
                body = local_file.read()
            await req.respond({
                'status': 200,
                'contentType': content_type,
                'body': body,
            })

        page.on('request', lambda req: asyncio.ensure_future(handle_request(req)))

    async def _wait_render_ready(self, page):
        await page.evaluate('''() => {
            const waitFrames = () => new Promise(resolve => {
                requestAnimationFrame(() => requestAnimationFrame(resolve));
            });
            if (document.fonts && document.fonts.ready) {
                return document.fonts.ready.then(waitFrames, waitFrames);
            }
            return waitFrames();
        }''')

    def _limit_image_dimensions(self, image: PILImage.Image):
        max_dimension = max(1, int(self.max_animation_dimension))
        if max(image.size) <= max_dimension:
            return image
        image = image.copy()
        resample = getattr(getattr(PILImage, 'Resampling', PILImage), 'LANCZOS')
        image.thumbnail((max_dimension, max_dimension), resample)
        return image

    def _decode_frame(self, b64_data: str):
        frame_bytes = base64.b64decode(b64_data)
        try:
            with io.BytesIO(frame_bytes) as image_bio:
                image = PILImage.open(image_bio)
                image.load()
            return self._limit_image_dimensions(image.convert('RGBA'))
        finally:
            del frame_bytes

    @delegate(InstrAttr.BACKGROUND)
    async def render_as_task(self, op: SourceOp, *, url: str, data=None, target_selector='#target', done_selector='#done',
            api_base=None, fullpage=False, duration: float=None, keep_last=False,
            playback_rate=1):
        b64_img = await self.render(
            url, data=data, target_selector=target_selector, done_selector=done_selector, api_base=api_base,
            fullpage=fullpage, duration=duration, keep_last=keep_last, playback_rate=playback_rate
        )
        await op.send([
            Image(base64=b64_img)
        ])

    async def render(
            self, url, *, data=None, target_selector='#target', done_selector='#done',
            api_base=None, fullpage=False, duration: float=None, keep_last=False,
            playback_rate=1, local_files: dict[str, str]=None
        ):
        ...
        # # https://developer.mozilla.org/en-US/docs/Web/API/Animation/playbackRate
        async with self.render_lock:
            await self._ensure_browser()
            start = time.time()
            page = None
            if api_base is None:
                api_base = self.api_base
            try:
                page = await self.browser.newPage()
                await self._install_local_file_interceptor(page, local_files or {})
                await page.evaluateOnNewDocument(f'() => window.renderData={json.dumps(data)}')

                # await page.enable_debugger()

                if duration is not None:
                    duration = max(0.1, min(float(duration), self.max_animation_duration))
                    await page.pause_animation()

                await page.goto(urllib.parse.urljoin(api_base, url))

                # await page.pause_script()

                # render_scale = self.render_scale
                render_scale = 2

                if duration is not None:
                    render_scale = 1

                async def waitSelectors():
                    if not fullpage:
                        # await page.pause_script()
                        logger.info('waitSelectors')
                        # await page.resume_script()
                        await page.waitForSelector(done_selector)
                        await page.waitForSelector(target_selector)
                    await self._wait_render_ready(page)
                    ...

                if not fullpage:
                    await page.addStyleTag({'content': f':root {{font-size: {render_scale}px}}'})
                    # await page.waitForSelector(done_selector)
                    # await page.waitForSelector(target_selector)
                    target = await page.querySelector(target_selector)
                else:
                    target = page

                if duration is not None:

                    e = asyncio.Event()

                    async def wait_animation():
                        await asyncio.sleep(duration)
                        e.set()
                        ...

                    # await page.pause_script()

                    frames = await target.screencast({
                        'omitBackground': True,
                        'event': e,
                        'waitReady': waitSelectors(),
                        'onStart': lambda: asyncio.create_task(wait_animation()),
                        'format': 'jpeg',
                        'playbackRate': playback_rate
                        # 'quality': 50
                    })

                    selected_frames = []
                    frame_durations = []
                    min_frame_delta = 1 / (30 * playback_rate)
                    prev_ts = None
                    raw_frame_count = len(frames)
                    max_frames = max(1, int(self.max_animation_frames))
                    for frame in frames:
                        ts = frame['timestamp']
                        if prev_ts is not None and ts - prev_ts < min_frame_delta:
                            frame['data'] = None
                            continue

                        frame_duration = 1 / 30 if prev_ts is None else max(
                            1 / 30,
                            (ts - prev_ts) * playback_rate
                        )
                        selected_frames.append(self._decode_frame(frame['data']))
                        frame_durations.append(frame_duration)
                        frame['data'] = None
                        prev_ts = ts

                        if len(selected_frames) >= max_frames:
                            break

                    del frames

                    if len(selected_frames) == 0:
                        raise RuntimeError('no frames captured')

                    average_duration = statistics.mean(frame_durations)
                    average_fps = 1 / average_duration

                    if keep_last:
                        frame_durations[-1] = 5


                    logger.debug(
                        f'{raw_frame_count=} => selected_frame_count={len(selected_frames)}, '
                        f'{average_fps=:.2f}'
                    )


                    buffered = io.BytesIO()

                    # first_img.save(buffered, format="GIF", save_all=True, append_images=remains_img, duration=durations, loop=0)

                    first_frame, *remaining_frames = selected_frames
                    first_frame.save(
                        buffered,
                        format='GIF',
                        save_all=True,
                        append_images=remaining_frames,
                        duration=[max(20, int(item * 1000)) for item in frame_durations],
                        loop=1,
                        disposal=2,
                    )

                    img_str = base64.b64encode(buffered.getvalue())

                    del first_frame, remaining_frames, selected_frames, frame_durations, buffered
                    gc.collect()

                    # return frames[0]['data']
                    return img_str
                else:
                    await waitSelectors()

                    return await target.screenshot({
                        'omitBackground': True,
                        'encoding': 'base64'
                    })
            finally:
                if page is not None:
                    with contextlib.suppress(Exception):
                        await page.close()
                await self._restart_browser_if_needed()
                end = time.time()
                logger.debug(f'elapsed {end-start:.2f}s')

"""astrbot_plugin_rss_plus —— RSS 订阅增强版。

在原 astrbot_plugin_rss 基础上做了如下改造：

1. 翻译功能内置化：直接复用 AstrBot 已配置的大语言模型（Provider），
   不再需要写死的第三方 API Key，也不再依赖 openai SDK；
   同时把原来的同步阻塞调用改为异步调用，并加上缓存 / 超时 / 失败兜底。
2. 新增 WebUI 插件配置页（pages/settings）：所有配置项都可以在插件页面里
   可视化修改，并附带运行状态、订阅管理、翻译测试。
3. 推送文本样式可配置：推送文案的模板（「更新了喵❤」「内容：」
   「链接：」「---」等）以及换行规则都搬进配置文件的 message_style 段，
   改样式不用再动代码；同时换行更智能（句末标点、英文句子边界、话题标签、
   链接保护、按句子截断）。实现见 message_style.py。
4. 修复若干稳定性问题：
   - 定时任务 ID 改为 md5，避免 Python hash 随机化导致重启后任务重复 / 丢失；
   - 数据文件放到插件数据目录并原子写入；
   - 数据文件结构自检，避免手工改坏后插件起不来；
   - RSS 解析、图片下载增加容错与超时；
   - 正文里的界面小图标（「查看图片 / 评论配图 / 超话 / 表情」前面那些 1rem
     图标、作者头像、站徽 logo）不再被当成正文图片发送，见 data_handler.strip_html_pic；
   - 新增全局屏蔽词（v1.5.0）：标题/正文的完整原文命中即不推送，开启翻译时译文
     命中同样不推送；命中的条目静默跳过并写日志，手动 /rss get 不受限制、会在
     消息开头标注这一条命中的词。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import time
from typing import Any, List, Optional
from urllib.parse import parse_qsl, quote, urlencode, urljoin, urlparse, urlunparse

import aiohttp
import dateutil.parser
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from lxml import etree

from astrbot.api import AstrBotConfig
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star, register
import astrbot.api.message_components as Comp

try:
    from astrbot.api.event import MessageEventResult

    MessageEventResultType = MessageEventResult
except Exception:  # noqa: BLE001 - 极老版本兜底

    class MessageEventResultType:  # type: ignore[no-redef]
        """占位类型，仅用于 isinstance 判断。"""

        pass

from .data_handler import DataHandler
from .message_style import COVER_MARKER
from .message_style import DEMO_SAMPLE as MESSAGE_STYLE_DEMO
from .message_style import STYLE_KEYS as _MESSAGE_STYLE_KEYS
from .message_style import MessageStyle
from .pic_handler import RssImageHandler
from .rss import RSSItem
from .translator import TranslatorService

try:  # AstrBot >= 4.24 的插件 Pages 后端 API
    from astrbot.api.web import error_response, json_response, request

    WEB_API_AVAILABLE = True
except Exception:  # noqa: BLE001 - 低版本 AstrBot 仍然可以正常使用指令
    WEB_API_AVAILABLE = False

PLUGIN_NAME = "astrbot_plugin_rss_plus"
PLUGIN_VERSION = "v1.5.1"

# 微博 H5 视频接口：传视频 oid 就能拿到封面（不需要 cookie）。
# 只在 RSSHub 没给出 <video poster> 时兜底用。
VIDEO_COVER_API = "https://h5.video.weibo.com/api/component"
VIDEO_COVER_REFERER = "https://h5.video.weibo.com/"
VIDEO_COVER_TIMEOUT = 8

logger = logging.getLogger("astrbot")

# 允许在插件页面上修改的配置项（白名单，避免被写入奇怪的东西）
_TRANSLATE_KEYS = {
    "enable",
    "provider_id",
    "target_lang",
    "sources",
    "max_chars",
    "timeout",
    "temperature",
    "cache_size",
    "keep_original_on_fail",
    "translate_title",
    "skip_if_translated",
    "short_link_clean",
}
_PIC_KEYS = {"is_read_pic", "is_adjust_pic", "max_pic_item"}
_TOP_KEYS = {
    "show_title",
    "show_video_info",
    "title_max_length",
    "description_max_length",
    "max_items_per_poll",
    "compose",
    "t2i",
    "is_hide_url",
}
_BLOCK_KEYS = {"enable", "words"}
MAX_BLOCK_WORDS = 500  # 屏蔽词条数上限
MAX_BLOCK_WORD_LEN = 100  # 单个屏蔽词长度上限


@register(
    PLUGIN_NAME,
    "Soulter",
    "RSS订阅增强版（内置 AI 翻译 + 插件配置页）",
    PLUGIN_VERSION,
    "https://github.com/Soulter/astrbot_plugin_rss",
)
class RssPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig) -> None:
        super().__init__(context)

        self.context = context
        self.config = config
        self.data_handler = DataHandler()
        self.translator = TranslatorService(context, config.get("translate", {}))
        self.message_style = MessageStyle(config.get("message_style", {}))
        self.pic_handler = RssImageHandler()
        self.scheduler = AsyncIOScheduler()
        self._video_cover_cache: dict[str, str] = {}
        self._apply_config()
        self.scheduler.start()
        self._refresh_scheduler()
        self._register_page_apis()

    # ================================================================== 生命周期
    async def terminate(self):
        """插件被卸载 / 重载时调用。"""
        try:
            if self.scheduler.running:
                self.scheduler.shutdown(wait=False)
                logger.info("RSS定时任务调度器已关闭")
        except Exception as exc:  # noqa: BLE001
            logger.warning("关闭调度器失败: %s", exc)
        try:
            await self.pic_handler.close()
        except Exception:  # noqa: BLE001
            pass

    # 兼容旧版本的写法
    async def stop(self):
        await self.terminate()

    # ================================================================== 配置
    def _apply_config(self) -> None:
        """把（可能刚被插件页面修改过的）配置同步到运行期属性。"""
        cfg = self.config or {}
        self.title_max_length = int(cfg.get("title_max_length", 30) or 30)
        self.description_max_length = int(cfg.get("description_max_length", 500) or 500)
        self.max_items_per_poll = int(cfg.get("max_items_per_poll", 3) or 3)
        self.t2i = bool(cfg.get("t2i", False))
        self.is_hide_url = bool(cfg.get("is_hide_url", False))
        self.is_compose = bool(cfg.get("compose", False))
        self.show_title = bool(cfg.get("show_title", True))
        self.show_video_info = bool(cfg.get("show_video_info", True))

        pic_cfg = cfg.get("pic_config", {}) or {}
        self.is_read_pic = bool(pic_cfg.get("is_read_pic", False))
        self.is_adjust_pic = bool(pic_cfg.get("is_adjust_pic", False))
        # 注意：0 是合法值（该订阅/全局「不发正文图片」），不能用 `or 3` 兜底成 3
        try:
            self.max_pic_item = max(-1, int(pic_cfg.get("max_pic_item", 3)))
        except (TypeError, ValueError):
            self.max_pic_item = 3
        self.pic_handler.is_adjust_pic = self.is_adjust_pic

        translate_cfg = cfg.get("translate", {}) or {}
        self.translator.update_config(translate_cfg)
        self.short_link_clean = bool(translate_cfg.get("short_link_clean", True))

        # 屏蔽词：全局一份，命中（原文或译文）即不推送
        block_cfg = cfg.get("block_words", {}) or {}
        self.block_enable = self._as_bool(block_cfg.get("enable", True), True)
        self.block_words = self._normalize_block_words(block_cfg.get("words", []))
        self._block_words_lower = [word.lower() for word in self.block_words]

        self.message_style.update_config(cfg.get("message_style", {}) or {})

    def _save_config(self) -> None:
        save = getattr(self.config, "save_config", None)
        if callable(save):
            save()
        else:  # 理论上不会发生，兜底避免插件页面保存时报错
            logger.warning("当前 AstrBot 版本的 AstrBotConfig 不支持 save_config()")

    # ================================================================== 调度器
    def _generate_job_id(self, url: str, user: str) -> str:
        """稳定的任务 ID（不要用内置 hash，它受 PYTHONHASHSEED 影响）。"""
        digest = hashlib.md5(f"{url}|{user}".encode("utf-8")).hexdigest()[:16]
        return f"rss_{digest}"

    def parse_cron_expr(self, cron_expr: str) -> dict:
        """解析 5 段 cron 表达式为 apscheduler 需要的字典。"""
        fields = cron_expr.split()
        if len(fields) != 5:
            raise ValueError("cron 表达式需要 5 个字段：分 时 日 月 周")
        return {
            "minute": fields[0],
            "hour": fields[1],
            "day": fields[2],
            "month": fields[3],
            "day_of_week": fields[4],
        }

    def _refresh_scheduler(self) -> None:
        """按数据文件重新对齐定时任务（幂等）。"""
        try:
            logger.info("刷新RSS定时任务")
            expected: dict[str, tuple[str, str, str]] = {}
            for url, info in self.data_handler.iter_channels():
                for user, sub_info in info.get("subscribers", {}).items():
                    if not isinstance(sub_info, dict):
                        continue
                    cron_expr = sub_info.get("cron_expr", "* * * * *")
                    expected[self._generate_job_id(url, user)] = (url, user, cron_expr)

            for job in self.scheduler.get_jobs():
                if job.id not in expected:
                    logger.info("删除过期定时任务: %s", job.id)
                    self.scheduler.remove_job(job.id)

            for job_id, (url, user, cron_expr) in expected.items():
                existing = self.scheduler.get_job(job_id)
                if existing is not None and str(existing.trigger) == str(
                    CronTrigger.from_crontab(cron_expr)
                ):
                    continue
                try:
                    trigger = CronTrigger.from_crontab(cron_expr)
                except Exception as exc:  # noqa: BLE001
                    logger.error("非法的 cron 表达式 %s (%s): %s", cron_expr, url, exc)
                    continue
                if existing is not None:
                    self.scheduler.remove_job(job_id)
                logger.info("添加新定时任务: %s (%s - %s)", job_id, url, user)
                self.scheduler.add_job(
                    self.cron_task_callback,
                    trigger=trigger,
                    args=[url, user],
                    id=job_id,
                    replace_existing=True,
                    coalesce=True,
                    max_instances=1,
                    misfire_grace_time=120,
                )
        except Exception as exc:  # noqa: BLE001
            logger.error("刷新定时任务失败: %s", exc)

    # ================================================================== RSS 抓取
    async def parse_channel_info(self, url: str, max_retry: int = 3) -> Optional[bytes]:
        """获取 RSS 原始内容（带重试）。"""
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            )
        }
        timeout = aiohttp.ClientTimeout(total=30, connect=10)
        connector = aiohttp.TCPConnector(ssl=False)

        async with aiohttp.ClientSession(
            trust_env=True, connector=connector, timeout=timeout, headers=headers
        ) as session:
            for retry in range(max_retry):
                try:
                    async with session.get(url) as resp:
                        if resp.status != 200:
                            logger.error("rss: 无法正常打开站点 %s，状态码: %s", url, resp.status)
                            await asyncio.sleep(1)
                            continue
                        return await resp.read()
                except asyncio.TimeoutError:
                    logger.warning("rss: 请求站点 %s 超时，重试第 %s/%s 次", url, retry + 1, max_retry)
                    await asyncio.sleep(1)
                except aiohttp.ClientError as exc:
                    logger.warning("rss: 请求站点 %s 网络错误: %s，重试第 %s/%s 次", url, exc, retry + 1, max_retry)
                    await asyncio.sleep(1)
                except Exception as exc:  # noqa: BLE001
                    logger.error("rss: 请求站点 %s 发生未知错误: %s", url, exc)
                    return None
        logger.error("rss: 请求站点 %s 重试 %s 次后仍失败", url, max_retry)
        return None

    def smart_clean_url(self, url: str) -> str:
        """去掉常见的跟踪参数。"""
        parsed = urlparse(url)
        query_params = parse_qsl(parsed.query)
        blacklist = {
            "fbclid",
            "utm_source",
            "utm_medium",
            "utm_campaign",
            "utm_term",
            "utm_content",
            "gclid",
            "msclkid",
        }
        cleaned_params = [(k, v) for k, v in query_params if k not in blacklist]
        new_query = urlencode(cleaned_params) if cleaned_params else ""
        return urlunparse(parsed._replace(query=new_query))

    async def poll_rss(
        self,
        url: str,
        num: int = -1,
        after_timestamp: int = 0,
        after_link: str = "",
        raw_text: Optional[bytes] = None,
    ) -> List[RSSItem]:
        """拉取并解析 RSS，返回新条目。

        raw_text 不为空时直接复用已下载的内容，避免同一次操作重复请求站点。
        """
        text = raw_text if raw_text is not None else await self.parse_channel_info(url)
        if text is None:
            logger.error("rss: 无法解析站点 %s 的RSS信息", url)
            return []

        try:
            root = etree.fromstring(text)
        except Exception as exc:  # noqa: BLE001
            logger.error("rss: 解析XML失败 %s: %s", url, exc)
            return []

        items = root.xpath("//item")
        if not items:
            logger.info("rss: 站点 %s 无内容", url)
            return []

        cnt = 0
        rss_items: List[RSSItem] = []

        for item in items:
            try:
                if num != -1 and cnt >= num:
                    break

                chan_title = (
                    self.data_handler.data[url]["info"]["title"]
                    if url in self.data_handler.data
                    and isinstance(self.data_handler.data[url], dict)
                    and self.data_handler.data[url].get("info")
                    else "未知频道"
                )

                title_elem = item.xpath("title")
                title = title_elem[0].text.strip() if title_elem and title_elem[0].text else "无标题"
                title = self.data_handler.clean_plain_text(title) or "无标题"
                full_title = title  # 未截断的标题，用于屏蔽词检查
                if len(title) > self.title_max_length:
                    title = title[: self.title_max_length] + "..."

                link_elem = item.xpath("link")
                link = link_elem[0].text.strip() if link_elem and link_elem[0].text else ""
                if link and not re.match(r"^https?://", link):
                    try:
                        link = urljoin(self.data_handler.get_root_url(url), link)
                    except Exception as exc:  # noqa: BLE001
                        logger.warning("补全链接失败 %s -> %s: %s", url, link, exc)

                desc_elem = item.xpath("description")
                description = desc_elem[0].text if desc_elem and desc_elem[0].text else ""

                pic_url_list: list[str] = []
                video_covers: list[str] = []
                video_url = ""
                if description:
                    # 视频帖：封面图与播放页链接必须先取（后面会把整个 video 块当噪音删掉）
                    info = self.data_handler.extract_video_info(description, link or url)
                    video_covers = list(info["covers"])
                    video_url = info["video_url"]
                    # RSSHub 没给 poster 时，拿视频 oid 去微博 H5 接口补一张封面
                    if self.show_video_info and not video_covers and info["oid"]:
                        cover = await self._fetch_video_cover_by_oid(info["oid"])
                        if cover:
                            video_covers.append(cover)
                    if video_covers or video_url:
                        logger.info(
                            "rss: 视频帖 %s 封面 %s 张、播放页 %s",
                            link, len(video_covers), video_url or "-",
                        )
                    # 正文与正文图片（不含视频封面）
                    description, pic_url_list = self.data_handler.parse_description(description, url)
                description = self.data_handler.clean_plain_text(description)

                # 屏蔽词在「截断之前」的完整文本上查，免得关键词落在被切掉的尾巴里
                block_word, block_where = self._block_scan(
                    (full_title, "标题"), (description, "正文")
                )

                if len(description) > self.description_max_length:
                    description = description[: self.description_max_length] + "..."

                pub_date = ""
                pub_date_timestamp = int(time.time())
                pubdate_elem = item.xpath("pubDate")
                if pubdate_elem and pubdate_elem[0].text:
                    pub_date = pubdate_elem[0].text.strip()
                    try:
                        pub_date_parsed = dateutil.parser.parse(pub_date)
                        pub_date_timestamp = int(pub_date_parsed.timestamp())
                    except Exception as exc:  # noqa: BLE001
                        logger.warning("解析pubDate失败 %s - %s: %s", url, pub_date, exc)

                is_new = False
                if pub_date_timestamp > after_timestamp:
                    is_new = True
                elif not pub_date and link and link != after_link:
                    is_new = True

                if is_new:
                    rss_items.append(
                        RSSItem(
                            chan_title,
                            title,
                            link,
                            description,
                            pub_date,
                            pub_date_timestamp,
                            pic_url_list,
                            url,
                            video_url,
                            video_covers,
                            block_word,
                            block_where,
                        )
                    )
                    cnt += 1
                else:
                    # RSS 通常按时间倒序，遇到旧内容直接结束
                    break
            except Exception as exc:  # noqa: BLE001
                logger.error("rss: 解析Rss条目失败 %s: %s", url, exc)
                continue

        return rss_items

    # ================================================================== 消息组装
    def _resolve_pic_settings(self, feed_url: str, umo: Optional[str]) -> tuple[bool, int]:
        """决定本次要读几张正文图片：订阅的独立设置优先，未设置时跟随全局配置。

        注意：这里只管**正文图片**。视频帖的封面图不参与这套设置，
        它只由「视频帖：推送封面图与视频链接」开关控制。

        Returns:
            (是否读取正文图片, 图片上限)；上限 -1 表示不限制。
        """
        override = None
        if feed_url and umo:
            try:
                override = self.data_handler.get_pic_limit(feed_url, umo)
            except Exception as exc:  # noqa: BLE001
                logger.warning("读取订阅独立图片配置失败 %s - %s: %s", feed_url, umo, exc)
        if override is None:
            return self.is_read_pic, self.max_pic_item
        if override == -1:
            return True, -1
        if override <= 0:
            return False, 0
        return True, override

    def _block_hit(self, *texts) -> str:
        """返回命中的屏蔽词；没命中返回空串。

        子串匹配，英文不分大小写（中文没有词边界，子串也是最符合直觉的做法）。
        """
        if not self.block_enable or not self._block_words_lower:
            return ""
        for text in texts:
            if not text:
                continue
            low = str(text).lower()
            for word in self._block_words_lower:
                if word in low:
                    return word
        return ""

    def _block_scan(self, *pairs) -> tuple[str, str]:
        """按顺序检查 ``(文本, 位置)``，返回第一个命中的 ``(屏蔽词, 位置)``。"""
        for text, where in pairs:
            word = self._block_hit(text)
            if word:
                return word, where
        return "", ""

    async def _build_chain(
        self, item: RSSItem, umo: Optional[str] = None, apply_block: bool = True
    ) -> tuple[list, dict]:
        """组装消息链（含内置翻译与可配置的推送样式），并报告屏蔽词命中情况。

        Args:
            apply_block: True（定时推送）时命中屏蔽词返回空消息链，调用方据此跳过；
                False（手动 ``/rss get``）时只报告、不拦截。

        Returns:
            ``(comps, block_info)``，``block_info`` 形如
            ``{"blocked": bool, "word": 命中的词, "where": 标题/正文/译文标题/译文正文}``。

        屏蔽词查两次：**原文**（解析时就查好了，见 ``poll_rss``，覆盖被长度截断掉
        的部分）与**译文**（原文干净但译文出现屏蔽词时同样不推送）。
        """
        info = {"blocked": False, "word": "", "where": ""}
        comps: list = []
        try:
            title = item.title or ""
            text = item.description or ""

            # 原文命中：直接返回，顺手省掉一次翻译调用。
            # item.block_word 是 poll_rss 拿「未截断」的完整文本查出来的（关键词
            # 落在被长度截掉的那段里也能拦住），这里再按当前配置查一遍标题/正文，
            # 保证即使条目不是 poll_rss 造出来的、或解析后配置又被改过，也不会漏。
            hit, where = self._block_scan((title, "标题"), (text, "正文"))
            if not hit and item.block_word:
                hit, where = item.block_word, item.block_where or "原文"
            if hit:
                info.update(blocked=True, word=hit, where=where)
                if apply_block:
                    logger.info(
                        "rss: %s 命中屏蔽词「%s」（%s），跳过推送", item.link, hit, where
                    )
                    return [], info

            title_translated = False
            text_translated = False
            if (
                self.show_title
                and title
                and self.translator.enabled
                and self.translator.translate_title
                and self.translator.should_translate(item.link, title)
            ):
                title = await self.translator.translate(title, umo=umo)
                title_translated = True

            if self.translator.enabled and self.translator.should_translate(item.link, text):
                translated = await self.translator.translate(text, umo=umo)
                if translated:
                    text = translated
                    text_translated = True
                    if self.short_link_clean:
                        text = self._replace_first_url(text, item.description, item.link)

            # 译文命中：和原文一样不推送
            hit, where = self._block_scan(
                (title if title_translated else "", "译文标题"),
                (text if text_translated else "", "译文正文"),
            )
            if hit:
                if not info["blocked"]:
                    info.update(blocked=True, word=hit, where=where)
                if apply_block:
                    logger.info(
                        "rss: %s 命中屏蔽词「%s」（%s），跳过推送", item.link, hit, where
                    )
                    return [], info

            # 视频封面：位置由模板里的 {video_cover} 决定
            cover_urls = list(item.video_covers) if self.show_video_info else []

            # 排版（模板 + 智能换行 + 截断）全部交给 message_style，
            # 样式可以在配置文件 / 插件页面里改，不需要动代码。
            rendered = self.message_style.render(
                chan_title=item.chan_title,
                title=title,
                content=text,
                link=item.link,
                video=item.video_url if self.show_video_info else "",
                video_cover=bool(cover_urls),
                pub_date=item.pubDate,
                feed_url=item.feed_url,
                show_title=self.show_title,
                hide_url=self.is_hide_url,
            )

            inline_cover = ""
            if cover_urls and COVER_MARKER in rendered:
                inline_cover = cover_urls.pop(0)  # 这张插进模板指定的位置

            # 按标记切开：文字 → 封面图 → 文字 …
            pieces = rendered.split(COVER_MARKER)
            for index, piece in enumerate(pieces):
                if index > 0:
                    await self._append_image(comps, inline_cover, item)
                if index < len(pieces) - 1 and piece.endswith("\n"):
                    piece = piece[:-1]  # 标记自己占的那一行换行不算
                if index > 0 and piece.startswith("\n"):
                    piece = piece[1:]
                if piece:
                    comps.append(Comp.Plain(piece))

            read_pic, pic_limit = self._resolve_pic_settings(item.feed_url, umo)

            # 图片列表，顺序：视频封面 → 正文图片
            #
            # 视频封面**只**归「视频帖：推送封面图与视频链接」开关管：
            #   · 不受「读取图片」开关影响（那个开关只管正文图片，默认还是关的）；
            #   · 不参与「每次最大图片数量」，所以上限设成 0 也照样发封面
            #     （上游的 0 只表示「正文图片不发」；连封面都不要就关掉视频帖开关）。
            images: list[str] = list(cover_urls)

            # 正文图片照旧：读不读、读几张都听图片设置的
            if read_pic and item.pic_urls:
                body_pics = list(item.pic_urls)
                if pic_limit != -1:
                    body_pics = body_pics[: max(0, pic_limit)]
                images.extend(body_pics)

            # 去重（保持顺序）
            deduped: list[str] = []
            for pic_url in images:
                if pic_url not in deduped:
                    deduped.append(pic_url)

            for pic_url in deduped:
                await self._append_image(comps, pic_url, item)
        except Exception as exc:  # noqa: BLE001
            logger.error("组装消息链失败: %s", exc)
            comps = [Comp.Plain(f"消息组装失败: {exc}")]
        return comps, info

    async def _get_chain_components(self, item: RSSItem, umo: Optional[str] = None) -> list:
        """组装消息链；命中屏蔽词时返回空列表（定时推送据此跳过）。"""
        comps, _ = await self._build_chain(item, umo=umo, apply_block=True)
        return comps

    async def _append_image(self, comps: list, pic_url: str, item: RSSItem) -> None:
        """下载一张图并追加到消息链（失败时给出可配置的提示文案）。"""
        if not pic_url:
            return
        try:
            base64str, reason = await self.pic_handler.fetch_image_base64(
                pic_url, base_url=item.link or item.feed_url
            )
            if base64str:
                comps.append(Comp.Image.fromBase64(base64str))
            else:
                logger.warning("rss: 图片读取失败 %s：%s", pic_url, reason)
                comps.append(Comp.Plain(f"{self.message_style.pic_error_text()}\n"))
        except Exception as exc:  # noqa: BLE001
            logger.warning("处理图片失败 %s: %s", pic_url, exc)
            comps.append(Comp.Plain(f"{self.message_style.pic_error_text(pic_url)}\n"))

    async def _fetch_video_cover_by_oid(self, oid: str) -> str:
        """用微博 H5 接口给视频补一张封面（RSSHub 没输出 poster 时的兜底）。

        接口：``POST https://h5.video.weibo.com/api/component?page=/show/<oid>``，
        返回 JSON 里的 ``cover_image`` 就是封面，**不需要 cookie**。
        结果（包括失败）会缓存，避免同一条视频被反复请求。
        """
        oid = (oid or "").strip()
        if not oid:
            return ""
        if oid in self._video_cover_cache:
            return self._video_cover_cache[oid]

        cover = ""
        try:
            page = quote(f"/show/{oid}", safe="")
            body = urlencode(
                {"data": json.dumps({"Component_Play_Playinfo": {"oid": oid}})}
            )
            timeout = aiohttp.ClientTimeout(total=VIDEO_COVER_TIMEOUT, connect=5)
            headers = {
                "User-Agent": (
                    "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) "
                    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.0 Mobile/15E148 Safari/604.1"
                ),
                "Referer": VIDEO_COVER_REFERER,
                "Content-Type": "application/x-www-form-urlencoded",
            }
            async with aiohttp.ClientSession(
                trust_env=True, timeout=timeout, headers=headers
            ) as session:
                async with session.post(
                    f"{VIDEO_COVER_API}?page={page}", data=body
                ) as resp:
                    if resp.status == 200:
                        payload = await resp.json(content_type=None)
                        play_info = ((payload or {}).get("data") or {}).get(
                            "Component_Play_Playinfo"
                        ) or {}
                        cover = str(play_info.get("cover_image") or "").strip()
                        if cover.startswith("//"):
                            cover = "https:" + cover
        except Exception as exc:  # noqa: BLE001 - 兜底失败不影响推送
            logger.info("rss: 视频封面接口查询失败 %s: %s", oid, exc)

        if len(self._video_cover_cache) >= 256:
            self._video_cover_cache.clear()
        self._video_cover_cache[oid] = cover
        if cover:
            logger.info("rss: 视频 %s 的封面由接口补出：%s", oid, cover)
        return cover

    def _replace_first_url(self, translated: str, original: str, link: str) -> str:
        """把译文里的第一个外链换成清理过跟踪参数的短链。"""
        try:
            urls = re.findall(
                r"https?://(?:www\.)?[-a-zA-Z0-9@:%._+~#=]{1,256}\.[a-zA-Z0-9()]{2,6}\b"
                r"(?:[-a-zA-Z0-9()@:%_+.~#?&/=]*[^.\s])?",
                original,
            )
            if not urls:
                return translated
            short_url = self.smart_clean_url(urls[0])
            return translated.replace(urls[0], f"\n{short_url}")
        except Exception:  # noqa: BLE001
            return translated

    async def _send_items(self, user: str, rss_items: List[RSSItem]) -> None:
        """按平台规则推送条目；命中屏蔽词的条目直接跳过（不推送、只在日志里交代）。"""
        if not rss_items:
            return
        parts = user.split(":", 2)
        platform_name = parts[0] if len(parts) == 3 else ""

        skipped: list[RSSItem] = []

        if platform_name == "aiocqhttp" and self.is_compose:
            nodes = []
            for item in rss_items:
                comps, _ = await self._build_chain(item, umo=user)
                if not comps:  # 命中屏蔽词
                    skipped.append(item)
                    continue
                nodes.append(Comp.Node(uin=0, name="Astrbot", content=comps))
            if nodes:
                await self.context.send_message(
                    user, MessageChain(chain=nodes, use_t2i_=self.t2i)
                )
        else:
            for item in rss_items:
                comps, _ = await self._build_chain(item, umo=user)
                if not comps:  # 命中屏蔽词
                    skipped.append(item)
                    continue
                await self.context.send_message(
                    user, MessageChain(chain=comps, use_t2i_=self.t2i)
                )

        if skipped:
            logger.info(
                "rss: %s 有 %d 条内容命中屏蔽词被跳过：%s",
                user,
                len(skipped),
                "、".join(f"{it.block_word}({it.block_where})" for it in skipped[:5]),
            )

    async def cron_task_callback(self, url: str, user: str) -> None:
        """定时任务回调。"""
        try:
            if url not in self.data_handler.data:
                logger.warning("RSS定时任务: URL %s 不存在", url)
                return
            if user not in self.data_handler.data[url].get("subscribers", {}):
                logger.warning("RSS定时任务: 用户 %s 未订阅 %s", user, url)
                return

            logger.info("RSS 定时任务触发: %s - %s", url, user)
            sub = self.data_handler.data[url]["subscribers"][user]
            last_update = sub.get("last_update", 0)
            latest_link = sub.get("latest_link", "")

            rss_items = await self.poll_rss(
                url,
                num=self.max_items_per_poll,
                after_timestamp=last_update,
                after_link=latest_link,
            )
            max_ts = last_update

            await self._send_items(user, rss_items)

            if rss_items:
                for item in rss_items:
                    max_ts = max(max_ts, item.pubDate_timestamp)
                sub["last_update"] = max_ts
                sub["latest_link"] = rss_items[0].link
                self.data_handler.save_data()
                logger.info("RSS 定时任务 %s 推送成功 - %s", url, user)
            else:
                logger.info("RSS 定时任务 %s 无消息更新 - %s", url, user)
        except Exception as exc:  # noqa: BLE001
            logger.error("RSS定时任务执行失败 %s - %s: %s", url, user, exc)

    # ================================================================== 订阅管理
    @staticmethod
    def _looks_like_feed(text: Optional[bytes]) -> bool:
        """判断抓到的内容是不是 RSS/Atom/RDF 源，而不是普通网页。"""
        if not text:
            return False
        try:
            root = etree.fromstring(text)
        except Exception:  # noqa: BLE001
            return False
        tag = str(root.tag).lower()
        if "}" in tag:  # 去掉命名空间
            tag = tag.split("}", 1)[1]
        if tag in ("rss", "feed", "rdf"):
            return True
        try:
            return bool(root.xpath("//item | //entry | //channel"))
        except Exception:  # noqa: BLE001
            return False

    @staticmethod
    def _user_format_ok(user: str) -> bool:
        """校验 unified_msg_origin 格式：平台:类型:会话ID。"""
        parts = (user or "").split(":", 2)
        return len(parts) == 3 and all(p.strip() for p in parts)

    async def _create_subscription(
        self,
        url: str,
        user: str,
        cron_expr: str,
        max_pic_item=None,
        force: bool = False,
    ) -> tuple[bool, str, Optional[dict]]:
        """创建/更新一条订阅（指令与插件页面共用）。

        Returns:
            (是否成功, 提示信息, 频道信息)
        """
        url = (url or "").strip()
        user = (user or "").strip()
        cron_expr = (cron_expr or "").strip()

        if not self._user_format_ok(user):
            return False, "推送会话格式不正确，应为 平台:类型:会话ID，例如 aiocqhttp:group:123456", None
        if not self._is_url_or_ip(url):
            return False, "请输入正确的订阅地址（http/https 链接或 IP）", None
        try:
            CronTrigger.from_crontab(cron_expr)
        except Exception as exc:  # noqa: BLE001
            return False, f"非法的 cron 表达式: {exc}", None

        # 只抓一次，频道信息和条目都从这份内容里解析
        text = await self.parse_channel_info(url)
        is_feed = self._looks_like_feed(text)
        latest_items = await self.poll_rss(url, raw_text=text) if is_feed else []
        if not is_feed:
            logger.warning("rss: %s 看起来不是 RSS/Atom 源，内容前缀 %r", url, (text or b"")[:120])

        last_update = int(time.time())
        latest_link = ""
        if latest_items:
            last_update = latest_items[0].pubDate_timestamp
            latest_link = latest_items[0].link

        existing = self.data_handler.data.get(url)
        if self.data_handler._is_channel(existing) and (existing.get("info") or {}).get("title"):
            info = existing["info"]
        else:
            title = description = ""
            if is_feed:
                try:
                    title, description = self.data_handler.parse_channel_text_info(text)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("解析频道信息失败 %s: %s", url, exc)
            if not is_feed or not title:
                if not force:
                    reason = (
                        "该地址不是有效的 RSS/Atom 源"
                        if text and not is_feed
                        else "无法获取频道信息（网络不通或缺少标题）"
                    )
                    return (
                        False,
                        f"添加失败：{reason}。确认无误可勾选「强制添加」。",
                        None,
                    )
            info = {"title": title or url, "description": description or "无描述"}

        entry = {
            "cron_expr": cron_expr,
            "last_update": last_update,
            "latest_link": latest_link,
        }
        if max_pic_item is not None:
            entry["max_pic_item"] = int(max_pic_item)

        if self.data_handler._is_channel(existing):
            existing.setdefault("subscribers", {})[user] = entry
            existing["info"] = info
        else:
            self.data_handler.data[url] = {"subscribers": {user: entry}, "info": info}

        self.data_handler.save_data()
        self._refresh_scheduler()
        logger.info("RSS 订阅已添加/更新: %s -> %s (%s)", url, user, cron_expr)
        return True, f"已添加订阅：{info['title']}", info

    async def _add_url(self, url: str, cron_expr: str, message: AstrMessageEvent):
        """指令用的包装：失败时返回一条消息结果。"""
        try:
            ok, msg, info = await self._create_subscription(
                url, message.unified_msg_origin, cron_expr
            )
            if not ok:
                return message.plain_result(msg)
            return info
        except Exception as exc:  # noqa: BLE001
            logger.error("添加URL订阅失败 %s: %s", url, exc)
            return message.plain_result(f"添加失败: {exc}")

    def _is_url_or_ip(self, text: str) -> bool:
        url_pattern = r"^(?:http|https)://.+$"
        ip_pattern = (
            r"^((25[0-5]|2[0-4][0-9]|[01]?[0-9][0-9]?)\.){3}"
            r"(25[0-5]|2[0-4][0-9]|[01]?[0-9][0-9]?)$"
        )
        return bool(re.match(url_pattern, text) or re.match(ip_pattern, text))

    # ================================================================== 指令
    @filter.command_group("rss", alias={"RSS"})
    def rss(self):
        """RSS订阅插件

        可以订阅和管理多个RSS源，支持cron表达式设置更新频率

        cron 表达式格式：
        * * * * *，分别表示分钟 小时 日 月 星期，* 表示任意值，支持范围和逗号分隔。例：
        1. 0 0 * * * 表示每天 0 点触发。
        2. 0/5 * * * * 表示每 5 分钟触发。
        3. 0 9-18 * * * 表示每天 9 点到 18 点触发。
        4. 0 0 1,15 * * 表示每月 1 号和 15 号 0 点触发。
        星期的取值范围是 0-6，0 表示星期天。
        """
        pass

    @rss.group("rsshub")
    def rsshub(self, event: AstrMessageEvent):
        """RSSHub相关操作

        可以添加、查看、删除RSSHub的端点
        """
        pass

    @filter.permission_type(filter.PermissionType.ADMIN)
    @rsshub.command("add")
    async def rsshub_add(self, event: AstrMessageEvent, url: str):
        """添加一个RSSHub端点

        Args:
            url: RSSHub服务器地址，例如：https://rsshub.app
        """
        try:
            if url.endswith("/"):
                url = url[:-1]
            if not self._is_url_or_ip(url):
                yield event.plain_result("请输入正确的URL或IP地址")
                return
            elif url in self.data_handler.get_endpoints():
                yield event.plain_result("该RSSHub端点已存在")
                return
            else:
                self.data_handler.data.setdefault("rsshub_endpoints", []).append(url)
                self.data_handler.save_data()
                yield event.plain_result("添加成功")
        except Exception as exc:  # noqa: BLE001
            logger.error("添加RSSHub端点失败: %s", exc)
            yield event.plain_result(f"添加失败: {exc}")

    @rsshub.command("list")
    async def rsshub_list(self, event: AstrMessageEvent):
        """列出所有已添加的RSSHub端点"""
        try:
            endpoints = self.data_handler.get_endpoints()
            if not endpoints:
                yield event.plain_result("暂无添加的RSSHub端点")
                return
            ret = "当前Bot添加的rsshub endpoint：\n"
            ret += "\n".join([f"{i}: {x}" for i, x in enumerate(endpoints)])
            yield event.plain_result(ret)
        except Exception as exc:  # noqa: BLE001
            logger.error("列出RSSHub端点失败: %s", exc)
            yield event.plain_result(f"获取失败: {exc}")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @rsshub.command("remove")
    async def rsshub_remove(self, event: AstrMessageEvent, idx: int):
        """删除一个RSSHub端点

        Args:
            idx: 要删除的端点索引，可通过list命令查看
        """
        try:
            endpoints = self.data_handler.get_endpoints()
            if idx < 0 or idx >= len(endpoints):
                yield event.plain_result("索引越界")
                return

            removed_url = endpoints[idx]
            for url, info in list(self.data_handler.iter_channels()):
                if url.startswith(removed_url):
                    self.data_handler.delete_channel(url)

            self.data_handler.data["rsshub_endpoints"].pop(idx)
            self.data_handler.save_data()
            self._refresh_scheduler()
            yield event.plain_result("删除成功")
        except Exception as exc:  # noqa: BLE001
            logger.error("删除RSSHub端点失败: %s", exc)
            yield event.plain_result(f"删除失败: {exc}")

    @rss.command("add")
    async def add_command(
        self,
        event: AstrMessageEvent,
        idx: int,
        route: str,
        minute: str,
        hour: str,
        day: str,
        month: str,
        day_of_week: str,
    ):
        """通过RSSHub路由添加订阅

        Args:
            idx: RSSHub端点索引，可通过/rss rsshub list查看
            route: RSSHub路由，需以/开头
            minute: Cron表达式分钟字段
            hour: Cron表达式小时字段
            day: Cron表达式日期字段
            month: Cron表达式月份字段
            day_of_week: Cron表达式星期字段
        """
        try:
            endpoints = self.data_handler.get_endpoints()
            if idx < 0 or idx >= len(endpoints):
                yield event.plain_result("索引越界, 请使用 /rss rsshub list 查看已经添加的 rsshub endpoint")
                return
            if not route.startswith("/"):
                yield event.plain_result("路由必须以 / 开头")
                return

            url = endpoints[idx] + route
            ret = await self._add_url(url, f"{minute} {hour} {day} {month} {day_of_week}", event)
            if isinstance(ret, MessageEventResultType):
                yield ret
                return

            self._refresh_scheduler()
            yield event.plain_result(f"添加成功。频道信息：\n标题: {ret['title']}\n描述: {ret['description']}")
        except Exception as exc:  # noqa: BLE001
            logger.error("添加RSS订阅失败: %s", exc)
            yield event.plain_result(f"添加失败: {exc}")

    @rss.command("add-url")
    async def add_url_command(
        self,
        event: AstrMessageEvent,
        url: str,
        minute: str,
        hour: str,
        day: str,
        month: str,
        day_of_week: str,
    ):
        """直接通过Feed URL添加订阅

        Args:
            url: RSS Feed的完整URL
            minute: Cron表达式分钟字段
            hour: Cron表达式小时字段
            day: Cron表达式日期字段
            month: Cron表达式月份字段
            day_of_week: Cron表达式星期字段
        """
        try:
            ret = await self._add_url(url, f"{minute} {hour} {day} {month} {day_of_week}", event)
            if isinstance(ret, MessageEventResultType):
                yield ret
                return
            self._refresh_scheduler()
            yield event.plain_result(f"添加成功。频道信息：\n标题: {ret['title']}\n描述: {ret['description']}")
        except Exception as exc:  # noqa: BLE001
            logger.error("添加URL订阅失败: %s", exc)
            yield event.plain_result(f"添加失败: {exc}")

    @rss.command("list")
    async def list_command(self, event: AstrMessageEvent):
        """列出当前所有订阅的RSS频道"""
        try:
            user = event.unified_msg_origin
            subs_urls = self.data_handler.get_subs_channel_url(user)
            if not subs_urls:
                yield event.plain_result("暂无订阅的RSS频道")
                return
            ret = "当前订阅的频道：\n"
            for cnt, url in enumerate(subs_urls):
                info = self.data_handler.data[url]["info"]
                pic_limit = self.data_handler.get_pic_limit(url, user)
                pic_text = "跟随全局" if pic_limit is None else ("不发送" if pic_limit == 0 else ("不限" if pic_limit == -1 else f"{pic_limit} 张"))
                ret += f"{cnt}. {info['title']} - {info['description']}（图片：{pic_text}）\n"
            yield event.plain_result(ret)
        except Exception as exc:  # noqa: BLE001
            logger.error("列出订阅失败: %s", exc)
            yield event.plain_result(f"获取失败: {exc}")

    @rss.command("remove")
    async def remove_command(self, event: AstrMessageEvent, idx: int):
        """删除一个RSS订阅

        Args:
            idx: 要删除的订阅索引，可通过/rss list查看
        """
        try:
            user = event.unified_msg_origin
            subs_urls = self.data_handler.get_subs_channel_url(user)
            if idx < 0 or idx >= len(subs_urls):
                yield event.plain_result("索引越界, 请使用 /rss list 查看已经添加的订阅")
                return

            url = subs_urls[idx]
            job_id = self._generate_job_id(url, user)
            if self.scheduler.get_job(job_id):
                self.scheduler.remove_job(job_id)
                logger.info("删除订阅定时任务: %s", job_id)

            self.data_handler.delete_subscriber(url, user)
            self._refresh_scheduler()
            yield event.plain_result("删除成功")
        except Exception as exc:  # noqa: BLE001
            logger.error("删除订阅失败: %s", exc)
            yield event.plain_result(f"删除失败: {exc}")

    @rss.command("get")
    async def get_command(self, event: AstrMessageEvent, idx: int):
        """获取指定订阅的最新内容

        这是**手动**指令，屏蔽词不拦截：即使这条内容在定时推送时会被屏蔽，
        这里也照常发出来，并在最前面标注这一条命中了哪个屏蔽词，方便调试。

        Args:
            idx: 要查看的订阅索引，可通过/rss list查看
        """
        try:
            user = event.unified_msg_origin
            subs_urls = self.data_handler.get_subs_channel_url(user)
            if idx < 0 or idx >= len(subs_urls):
                yield event.plain_result("索引越界, 请使用 /rss list 查看已经添加的订阅")
                return

            url = subs_urls[idx]
            rss_items = await self.poll_rss(url)
            if not rss_items:
                yield event.plain_result("没有新的订阅内容")
                return

            item = rss_items[0]
            parts = user.split(":", 2)
            if len(parts) != 3:
                yield event.plain_result("用户格式错误")
                return
            platform_name = parts[0]

            comps, block_info = await self._build_chain(item, umo=user, apply_block=False)

            notice = self._block_notice(block_info)
            if notice:
                comps = [Comp.Plain(notice)] + list(comps)

            if platform_name == "aiocqhttp" and self.is_compose:
                node = Comp.Node(uin=0, name="Astrbot", content=comps)
                yield event.chain_result([node]).use_t2i(self.t2i)
            else:
                yield event.chain_result(comps).use_t2i(self.t2i)
        except Exception as exc:  # noqa: BLE001
            logger.error("获取订阅内容失败: %s", exc)
            yield event.plain_result(f"获取失败: {exc}")

    def _block_notice(self, block_info: dict) -> str:
        """``/rss get`` 开头的屏蔽词提示（没有命中就返回空串）。

        只讲**这一次真正发出去的那条**：它命中了哪个词、平时为什么收不到。
        不去统计这一批抓取里还有多少条会被拦下——``/rss get`` 本来只输出一条，
        整批的条数对不上号，那种提示只会变成噪音。
        """
        if not self.block_enable or not self._block_words_lower:
            return ""
        if not block_info.get("blocked"):
            return ""
        return (
            f"⚠️ 本条命中屏蔽词「{block_info.get('word', '')}」"
            f"（{block_info.get('where', '原文')}），定时推送会跳过这 1 条；"
            "本次是手动 /rss get，照常发送。"
        )

    @rss.command("pic")
    async def pic_command(self, event: AstrMessageEvent, idx: int, limit: str):
        """设置某个订阅的图片数量上限（每个订阅可以不一样）

        Args:
            idx: 订阅索引，可通过 /rss list 查看
            limit: 图片数量：数字表示最多几张；0 表示该订阅不发送图片；-1 表示不限制；default 表示跟随全局配置
        """
        try:
            user = event.unified_msg_origin
            subs_urls = self.data_handler.get_subs_channel_url(user)
            if idx < 0 or idx >= len(subs_urls):
                yield event.plain_result("索引越界, 请使用 /rss list 查看已经添加的订阅")
                return

            url = subs_urls[idx]
            raw = str(limit).strip().lower()
            if raw in ("default", "-", "全局", "全局配置"):
                self.data_handler.set_pic_limit(url, user, None)
                yield event.plain_result("已恢复为跟随全局图片配置")
                return

            try:
                value = int(raw)
            except ValueError:
                yield event.plain_result("图片数量需要是整数：0 不发送，-1 不限制，default 跟随全局")
                return
            if value < -1 or value > 50:
                yield event.plain_result("图片数量取值范围：-1 ~ 50")
                return

            self.data_handler.set_pic_limit(url, user, value)
            desc = "不发送图片" if value == 0 else ("不限制图片数量" if value == -1 else f"最多 {value} 张图片")
            yield event.plain_result(f"已设置该订阅的图片上限：{desc}")
        except Exception as exc:  # noqa: BLE001
            logger.error("设置订阅图片上限失败: %s", exc)
            yield event.plain_result(f"设置失败: {exc}")

    @rss.command("pic-test", alias={"picdebug"})
    async def pic_test_command(self, event: AstrMessageEvent, url: str):
        """诊断一张图片为什么发不出去

        Args:
            url: 图片链接
        """
        try:
            info = await self.pic_handler.diagnose(url)
            lines = [
                "图片诊断结果：",
                f"地址: {info['url']}",
                f"结果: {'成功' if info['ok'] else '失败'} - {info['reason']}",
                f"HTTP: {info['status']}  类型: {info['content_type'] or '无'}  大小: {info['size']} 字节",
            ]
            if info.get("detected_type"):
                lines.append(f"实际格式: {info['detected_type']}")
            if info.get("image"):
                img = info["image"]
                lines.append(f"图像信息: {img['format']} {img['size']} {img['mode']}")
            if info.get("image_error"):
                lines.append(f"PIL 解析失败: {info['image_error']}")
            yield event.plain_result("\n".join(lines))
        except Exception as exc:  # noqa: BLE001
            yield event.plain_result(f"诊断失败: {exc}")

    @rss.command("translate", alias={"tr"})
    async def translate_command(self, event: AstrMessageEvent, text: str):
        """使用内置翻译测试一段文本

        Args:
            text: 需要翻译的文本
        """
        if not self.translator.enabled:
            yield event.plain_result("翻译功能已在插件配置中关闭")
            return
        try:
            result = await self.translator.translate(text, umo=event.unified_msg_origin)
            yield event.plain_result(f"译文：\n{result}")
        except Exception as exc:  # noqa: BLE001
            yield event.plain_result(f"翻译失败: {exc}")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @rss.command("clear-all")
    async def clear_all_tasks(self, event: AstrMessageEvent):
        """清空所有定时任务（危险操作）"""
        try:
            count = 0
            for job in self.scheduler.get_jobs():
                if job.id.startswith("rss_"):
                    self.scheduler.remove_job(job.id)
                    count += 1
            self.data_handler.data = {"rsshub_endpoints": []}
            self.data_handler.save_data()
            yield event.plain_result(f"已删除 {count} 个定时任务和所有订阅数据")
        except Exception as exc:  # noqa: BLE001
            yield event.plain_result(f"清理失败: {exc}")

    # ================================================================== 插件页面 API
    def _register_page_apis(self) -> None:
        """注册插件页面所需的 Web API。"""
        if not WEB_API_AVAILABLE:
            logger.warning(
                "当前 AstrBot 版本不支持插件 Pages（astrbot.api.web 不可用），"
                "插件配置页将无法使用，请升级 AstrBot 或改用插件配置面板。"
            )
            return
        register = getattr(self.context, "register_web_api", None)
        if not callable(register):
            logger.warning("当前 AstrBot 版本不支持 context.register_web_api，插件配置页不可用")
            return

        prefix = f"/{PLUGIN_NAME}"
        apis = [
            (f"{prefix}/page/config", self.page_get_config, ["GET"], "读取插件配置"),
            (f"{prefix}/page/config", self.page_save_config, ["POST"], "保存插件配置"),
            (f"{prefix}/page/status", self.page_status, ["GET"], "插件运行状态"),
            (f"{prefix}/page/subscriptions", self.page_subscriptions, ["GET"], "订阅列表"),
            (f"{prefix}/page/subscriptions/add", self.page_add_subscription, ["POST"], "新增订阅"),
            (f"{prefix}/page/subscriptions/delete", self.page_delete_subscription, ["POST"], "删除订阅"),
            (f"{prefix}/page/subscriptions/cron", self.page_update_cron, ["POST"], "修改订阅频率"),
            (f"{prefix}/page/subscriptions/update", self.page_update_subscription, ["POST"], "修改订阅的推送频率与图片上限"),
            (f"{prefix}/page/rsshub", self.page_rsshub_list, ["GET"], "RSSHub 端点列表"),
            (f"{prefix}/page/rsshub/add", self.page_rsshub_add, ["POST"], "添加 RSSHub 端点"),
            (f"{prefix}/page/rsshub/remove", self.page_rsshub_remove, ["POST"], "删除 RSSHub 端点"),
            (f"{prefix}/page/translate/test", self.page_translate_test, ["POST"], "翻译测试"),
            (f"{prefix}/page/style/preview", self.page_style_preview, ["POST"], "推送样式预览"),
            (f"{prefix}/page/pic/test", self.page_pic_test, ["POST"], "图片链接诊断"),
            (f"{prefix}/page/actions/reload", self.page_reload, ["POST"], "重载配置与定时任务"),
        ]
        for route, handler, methods, desc in apis:
            try:
                register(route, handler, methods, desc)
            except Exception as exc:  # noqa: BLE001
                logger.error("注册插件页面 API 失败 %s: %s", route, exc)

    # ---------------------------------------------------------------- 配置读写
    def _public_config(self) -> dict:
        cfg = self.config or {}
        return {
            "title_max_length": int(cfg.get("title_max_length", 30) or 30),
            "description_max_length": int(cfg.get("description_max_length", 500) or 500),
            "max_items_per_poll": int(cfg.get("max_items_per_poll", 3) or 3),
            "compose": bool(cfg.get("compose", False)),
            "show_title": bool(cfg.get("show_title", True)),
            "show_video_info": bool(cfg.get("show_video_info", True)),
            "t2i": bool(cfg.get("t2i", False)),
            "is_hide_url": bool(cfg.get("is_hide_url", False)),
            "translate": {
                "enable": self.translator.enabled,
                "provider_id": self.translator.provider_id,
                "target_lang": self.translator.target_lang,
                "sources": list(self.translator.sources),
                "max_chars": self.translator.max_chars,
                "timeout": self.translator.timeout,
                "temperature": self.translator.temperature,
                "cache_size": self.translator.cache_size,
                "keep_original_on_fail": self.translator.keep_original_on_fail,
                "translate_title": self.translator.translate_title,
                "skip_if_translated": self.translator.skip_if_translated,
                "short_link_clean": getattr(self, "short_link_clean", True),
            },
            "pic_config": {
                "is_read_pic": self.is_read_pic,
                "is_adjust_pic": self.is_adjust_pic,
                "max_pic_item": self.max_pic_item,
            },
            "block_words": {
                "enable": getattr(self, "block_enable", True),
                "words": list(getattr(self, "block_words", [])),
            },
            "message_style": self.message_style.to_dict(),
        }

    @staticmethod
    def _as_bool(value: Any, default: bool) -> bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return bool(value)
        if isinstance(value, str):
            return value.strip().lower() in ("1", "true", "yes", "on", "是")
        return default

    @staticmethod
    def _as_int(value: Any, default: int, minimum: int, maximum: int) -> int:
        try:
            return max(minimum, min(maximum, int(value)))
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _as_float(value: Any, default: float, minimum: float, maximum: float) -> float:
        try:
            return max(minimum, min(maximum, float(value)))
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _normalize_block_words(raw: Any) -> list[str]:
        """把屏蔽词收敛成「去空白、按大小写去重」的列表。

        兼容三种写法：list（插件页面 / 配置文件）、以及用换行或逗号分隔的字符串。
        匹配时统一转小写，所以这里保留用户原始大小写只用于回显。
        """
        if isinstance(raw, str):
            chunks: list[str] = re.split(r"[\n\r,，;；]+", raw)
        elif isinstance(raw, (list, tuple, set)):
            chunks = []
            for entry in raw:
                chunks.extend(re.split(r"[\n\r,，;；]+", str(entry)))
        else:
            return []

        words: list[str] = []
        seen: set[str] = set()
        for chunk in chunks:
            word = str(chunk).strip()[:MAX_BLOCK_WORD_LEN]
            if not word:
                continue
            low = word.lower()
            if low in seen:
                continue
            seen.add(low)
            words.append(word)
            if len(words) >= MAX_BLOCK_WORDS:
                break
        return words

    def _sanitize_config(self, payload: dict) -> dict:
        """把页面传来的配置收敛成合法值（只接受白名单键）。"""
        current = self._public_config()
        result = dict(current)

        for key in _TOP_KEYS:
            if key not in payload:
                continue
            if key in ("compose", "t2i", "is_hide_url", "show_title", "show_video_info"):
                result[key] = self._as_bool(payload[key], current[key])
            elif key == "title_max_length":
                result[key] = self._as_int(payload[key], current[key], 5, 500)
            elif key == "description_max_length":
                result[key] = self._as_int(payload[key], current[key], 20, 5000)
            elif key == "max_items_per_poll":
                result[key] = self._as_int(payload[key], current[key], -1, 100)

        pic_payload = payload.get("pic_config")
        if isinstance(pic_payload, dict):
            pic = dict(current["pic_config"])
            for key in _PIC_KEYS:
                if key not in pic_payload:
                    continue
                if key in ("is_read_pic", "is_adjust_pic"):
                    pic[key] = self._as_bool(pic_payload[key], pic[key])
                else:
                    pic[key] = self._as_int(pic_payload[key], pic[key], -1, 50)
            result["pic_config"] = pic

        block_payload = payload.get("block_words")
        if isinstance(block_payload, dict):
            block = dict(current["block_words"])
            for key in _BLOCK_KEYS:
                if key not in block_payload:
                    continue
                if key == "enable":
                    block[key] = self._as_bool(block_payload[key], block[key])
                else:
                    block[key] = self._normalize_block_words(block_payload[key])
            result["block_words"] = block

        tr_payload = payload.get("translate")
        if isinstance(tr_payload, dict):
            tr = dict(current["translate"])
            for key in _TRANSLATE_KEYS:
                if key not in tr_payload:
                    continue
                value = tr_payload[key]
                if key in ("enable", "keep_original_on_fail", "translate_title", "skip_if_translated", "short_link_clean"):
                    tr[key] = self._as_bool(value, tr[key])
                elif key == "provider_id":
                    tr[key] = str(value or "").strip()[:200]
                elif key == "target_lang":
                    tr[key] = (str(value or "中文").strip() or "中文")[:32]
                elif key == "sources":
                    if isinstance(value, str):
                        items = [s.strip() for s in value.replace("，", ",").split(",")]
                    elif isinstance(value, list):
                        items = [str(s).strip() for s in value]
                    else:
                        items = tr[key]
                    tr[key] = [s for s in items if s][:50]
                elif key == "max_chars":
                    tr[key] = self._as_int(value, tr[key], 50, 20000)
                elif key == "timeout":
                    tr[key] = self._as_int(value, tr[key], 5, 600)
                elif key == "cache_size":
                    tr[key] = self._as_int(value, tr[key], 0, 10000)
                elif key == "temperature":
                    tr[key] = self._as_float(value, tr[key], 0.0, 2.0)
            result["translate"] = tr

        # 推送样式：直接交给 MessageStyle 收敛（它同时也是运行期用的那份实现）
        style_payload = payload.get("message_style")
        if isinstance(style_payload, dict):
            merged = dict(current["message_style"])
            for key in _MESSAGE_STYLE_KEYS:
                if key in style_payload:
                    merged[key] = style_payload[key]
            result["message_style"] = MessageStyle(merged).to_dict()

        return result

    def _write_config(self, new_config: dict) -> None:
        """写回 AstrBotConfig 并落盘。"""
        for key, value in new_config.items():
            self.config[key] = value
        self._save_config()
        self._apply_config()

    # ---------------------------------------------------------------- handlers
    def _available_providers(self) -> list:
        """列出可用于翻译的模型，供插件页面的下拉框使用。"""
        providers = []
        try:
            for provider in self.context.get_all_providers():
                try:
                    meta = provider.meta()
                except Exception:  # noqa: BLE001
                    continue
                providers.append({"id": meta.id, "model": meta.model, "type": meta.type})
        except Exception as exc:  # noqa: BLE001
            logger.warning("获取模型列表失败: %s", exc)
        return providers

    async def page_get_config(self):
        return json_response(
            {
                "ok": True,
                "plugin": PLUGIN_NAME,
                "version": PLUGIN_VERSION,
                "config": self._public_config(),
                "providers": self._available_providers(),
            }
        )

    async def page_save_config(self):
        payload = await request.json(default={})
        if not isinstance(payload, dict):
            return error_response("请求体必须是 JSON 对象", status_code=400)
        try:
            new_config = self._sanitize_config(payload)
            self._write_config(new_config)
            self._refresh_scheduler()
        except Exception as exc:  # noqa: BLE001
            logger.error("保存插件配置失败: %s", exc)
            return error_response(f"保存失败: {exc}", status_code=500)
        return json_response({"ok": True, "message": "配置已保存", "config": self._public_config()})

    async def page_status(self):
        jobs = []
        for job in self.scheduler.get_jobs():
            jobs.append(
                {
                    "id": job.id,
                    "next_run_time": job.next_run_time.strftime("%Y-%m-%d %H:%M:%S")
                    if getattr(job, "next_run_time", None)
                    else None,
                }
            )
        channels = list(self.data_handler.iter_channels())
        subscriber_total = sum(len(info.get("subscribers", {})) for _, info in channels)
        return json_response(
            {
                "ok": True,
                "plugin": PLUGIN_NAME,
                "version": PLUGIN_VERSION,
                "scheduler_running": bool(self.scheduler.running),
                "channel_count": len(channels),
                "subscriber_count": subscriber_total,
                "endpoint_count": len(self.data_handler.get_endpoints()),
                "data_file": str(self.data_handler.config_path),
                "jobs": sorted(jobs, key=lambda x: x["next_run_time"] or ""),
                "translator": {
                    "enabled": self.translator.enabled,
                    "target_lang": self.translator.target_lang,
                    "sources": list(self.translator.sources),
                    "cache_entries": len(self.translator._cache),  # noqa: SLF001
                },
                "provider": await self.translator.describe_provider(),
            }
        )

    async def page_subscriptions(self):
        return json_response({"ok": True, "items": self.data_handler.overview()})

    async def page_add_subscription(self):
        """在插件页面里新增一条订阅。

        请求体：
          url           直链地址；或配合 endpoint_index + route 走 RSSHub
          endpoint_index RSSHub 端点下标（可选）
          route          RSSHub 路由，需以 / 开头（可选）
          user          推送会话 umo，必填
          cron_expr     推送频率，必填
          max_pic_item  该订阅的图片上限（可选，留空表示跟随全局）
          force         抓不到频道信息时是否仍然添加
        """
        payload = await request.json(default={})
        if not isinstance(payload, dict):
            return error_response("请求体必须是 JSON 对象", status_code=400)

        url = str(payload.get("url") or "").strip()
        route = str(payload.get("route") or "").strip()
        endpoint_index = payload.get("endpoint_index")

        # 支持「选 RSSHub 端点 + 填路由」的写法
        if endpoint_index is not None and str(endpoint_index).strip() != "":
            endpoints = self.data_handler.get_endpoints()
            try:
                idx = int(endpoint_index)
            except (TypeError, ValueError):
                return error_response("RSSHub 端点下标必须是整数", status_code=400)
            if idx < 0 or idx >= len(endpoints):
                return error_response("RSSHub 端点下标越界，请先在 RSSHub 页签添加端点", status_code=400)
            if not route:
                return error_response("请填写 RSSHub 路由，例如 /weibo/user/123456", status_code=400)
            if not route.startswith("/"):
                return error_response("RSSHub 路由必须以 / 开头", status_code=400)
            url = endpoints[idx].rstrip("/") + route
        elif route and not url:
            return error_response("请先选择 RSSHub 端点", status_code=400)

        user = str(payload.get("user") or "").strip()
        cron_expr = str(payload.get("cron_expr") or "").strip()
        force = bool(payload.get("force"))

        max_pic_item = None
        if "max_pic_item" in payload:
            raw = payload.get("max_pic_item")
            text = "" if raw is None else str(raw).strip()
            if text and text.lower() not in ("default", "-", "全局"):
                try:
                    max_pic_item = int(text)
                except (TypeError, ValueError):
                    return error_response("图片数量需要是整数：0 不发送，-1 不限制", status_code=400)
                if max_pic_item < -1 or max_pic_item > 50:
                    return error_response("图片数量取值范围：-1 ~ 50", status_code=400)

        try:
            ok, message, info = await self._create_subscription(
                url, user, cron_expr, max_pic_item=max_pic_item, force=force
            )
        except Exception as exc:  # noqa: BLE001
            logger.error("插件页面新增订阅失败 %s: %s", url, exc)
            return error_response(f"添加失败: {exc}", status_code=500)
        if not ok:
            return error_response(message, status_code=400)
        return json_response(
            {
                "ok": True,
                "message": message,
                "url": url,
                "info": info,
                "items": self.data_handler.overview(),
            }
        )

    async def page_delete_subscription(self):
        payload = await request.json(default={})
        url = str(payload.get("url") or "").strip()
        user = str(payload.get("user") or "").strip()
        if not url:
            return error_response("缺少 url 参数", status_code=400)
        try:
            if user:
                removed = self.data_handler.delete_subscriber(url, user)
            else:
                removed = self.data_handler.delete_channel(url)
            if not removed:
                return error_response("未找到该订阅", status_code=404)
            self._refresh_scheduler()
        except Exception as exc:  # noqa: BLE001
            return error_response(f"删除失败: {exc}", status_code=500)
        return json_response({"ok": True, "message": "已删除"})

    async def page_update_cron(self):
        payload = await request.json(default={})
        url = str(payload.get("url") or "").strip()
        user = str(payload.get("user") or "").strip()
        cron_expr = str(payload.get("cron_expr") or "").strip()
        if not url or not user or not cron_expr:
            return error_response("url / user / cron_expr 均为必填", status_code=400)
        try:
            CronTrigger.from_crontab(cron_expr)
        except Exception as exc:  # noqa: BLE001
            return error_response(f"非法的 cron 表达式: {exc}", status_code=400)
        try:
            if not self.data_handler.set_cron(url, user, cron_expr):
                return error_response("未找到该订阅", status_code=404)
            self._refresh_scheduler()
        except Exception as exc:  # noqa: BLE001
            return error_response(f"保存失败: {exc}", status_code=500)
        return json_response({"ok": True, "message": "已更新推送频率"})

    async def page_update_subscription(self):
        """更新单个订阅：推送频率与「该订阅的图片数量上限」。

        max_pic_item 语义：
          缺省(不传)          -> 保持原值
          null / "" / default -> 清除独立设置，跟随全局配置
          0                   -> 该订阅不发送图片
          -1                  -> 该订阅图片数量不限制
          n > 0               -> 该订阅最多 n 张
        """
        payload = await request.json(default={})
        if not isinstance(payload, dict):
            return error_response("请求体必须是 JSON 对象", status_code=400)
        url = str(payload.get("url") or "").strip()
        user = str(payload.get("user") or "").strip()
        if not url or not user:
            return error_response("url / user 均为必填", status_code=400)

        if not self.data_handler.has_subscription(url, user):
            return error_response("未找到该订阅", status_code=404)

        if "cron_expr" in payload:
            cron_expr = str(payload.get("cron_expr") or "").strip()
            try:
                CronTrigger.from_crontab(cron_expr)
            except Exception as exc:  # noqa: BLE001
                return error_response(f"非法的 cron 表达式: {exc}", status_code=400)
            if not self.data_handler.set_cron(url, user, cron_expr):
                return error_response("未找到该订阅", status_code=404)

        if "max_pic_item" in payload:
            raw = payload.get("max_pic_item")
            text = "" if raw is None else str(raw).strip()
            if text == "" or text.lower() in ("default", "-", "全局", "全局配置"):
                self.data_handler.set_pic_limit(url, user, None)
            else:
                try:
                    value = int(text)
                except (TypeError, ValueError):
                    return error_response("图片数量需要是整数：0 不发送，-1 不限制", status_code=400)
                if value < -1 or value > 50:
                    return error_response("图片数量取值范围：-1 ~ 50", status_code=400)
                self.data_handler.set_pic_limit(url, user, value)

        self._refresh_scheduler()
        return json_response(
            {"ok": True, "message": "已更新", "items": self.data_handler.overview()}
        )

    async def page_rsshub_list(self):
        return json_response({"ok": True, "endpoints": self.data_handler.get_endpoints()})

    async def page_rsshub_add(self):
        payload = await request.json(default={})
        url = str(payload.get("url") or "").strip().rstrip("/")
        if not url:
            return error_response("缺少 url 参数", status_code=400)
        if not self._is_url_or_ip(url):
            return error_response("请输入正确的 URL 或 IP 地址", status_code=400)
        if url in self.data_handler.get_endpoints():
            return error_response("该 RSSHub 端点已存在", status_code=409)
        self.data_handler.data.setdefault("rsshub_endpoints", []).append(url)
        self.data_handler.save_data()
        return json_response({"ok": True, "message": "添加成功", "endpoints": self.data_handler.get_endpoints()})

    async def page_rsshub_remove(self):
        payload = await request.json(default={})
        endpoints = self.data_handler.get_endpoints()
        try:
            idx = int(payload.get("index"))
        except (TypeError, ValueError):
            return error_response("缺少 index 参数", status_code=400)
        if idx < 0 or idx >= len(endpoints):
            return error_response("索引越界", status_code=400)
        removed_url = endpoints[idx]
        for url, _info in list(self.data_handler.iter_channels()):
            if url.startswith(removed_url):
                self.data_handler.delete_channel(url)
        self.data_handler.data["rsshub_endpoints"].pop(idx)
        self.data_handler.save_data()
        self._refresh_scheduler()
        return json_response({"ok": True, "message": "删除成功", "endpoints": self.data_handler.get_endpoints()})

    async def page_translate_test(self):
        payload = await request.json(default={})
        text = str(payload.get("text") or "").strip()
        if not text:
            return error_response("请输入要翻译的文本", status_code=400)
        if not self.translator.enabled:
            return error_response("翻译功能当前已关闭", status_code=400)
        started = time.time()
        result = await self.translator.translate(text)
        provider = await self.translator.describe_provider()
        changed = result.strip() != text.strip()
        return json_response(
            {
                "ok": True,
                "source": text,
                "result": result,
                "translated": changed,
                "elapsed_ms": int((time.time() - started) * 1000),
                "provider": provider,
            }
        )

    async def page_style_preview(self):
        """用页面当前（可能还没保存的）推送样式渲染一条示例消息。"""
        payload = await request.json(default={})
        if not isinstance(payload, dict):
            payload = {}

        merged = self.message_style.to_dict()
        style_payload = payload.get("message_style")
        if isinstance(style_payload, dict):
            for key in _MESSAGE_STYLE_KEYS:
                if key in style_payload:
                    merged[key] = style_payload[key]
        style = MessageStyle(merged)

        supplied = payload.get("sample")
        supplied = supplied if isinstance(supplied, dict) else {}
        values = dict(MESSAGE_STYLE_DEMO)
        for key in ("chan_title", "title", "content", "link", "video", "pub_date", "feed_url"):
            value = supplied.get(key)
            if isinstance(value, str) and value.strip():
                values[key] = value

        show_title = self._as_bool(payload.get("show_title"), self.show_title)
        hide_url = self._as_bool(payload.get("is_hide_url"), self.is_hide_url)
        # 预览里把封面位置显示成一个可见的占位说明
        text = style.render(show_title=show_title, hide_url=hide_url, video_cover=True, **values)
        cover_placeholder = COVER_MARKER in text
        text = text.replace(COVER_MARKER, "🖼（视频封面图会插在这里）")
        return json_response(
            {
                "ok": True,
                "text": text,
                "cover_placeholder": cover_placeholder,
                "style": style.to_dict(),
                "used_template": "template_hide_url" if hide_url else "template",
                "show_title": show_title,
                "hide_url": hide_url,
                "line_count": text.count("\n") + 1,
                "char_count": len(text),
            }
        )

    async def page_pic_test(self):
        """诊断一条图片链接为什么读取失败。"""
        payload = await request.json(default={})
        url = str(payload.get("url") or "").strip()
        if not url:
            return error_response("请输入图片链接", status_code=400)
        try:
            info = await self.pic_handler.diagnose(url)
        except Exception as exc:  # noqa: BLE001
            return error_response(f"诊断失败: {exc}", status_code=500)
        return json_response({"ok": True, "result": info})

    async def page_reload(self):
        try:
            self.data_handler.load_data()
            self._apply_config()
            self._refresh_scheduler()
        except Exception as exc:  # noqa: BLE001
            return error_response(f"重载失败: {exc}", status_code=500)
        return json_response(
            {
                "ok": True,
                "message": "已重载数据与定时任务",
                "job_count": len(self.scheduler.get_jobs()),
            }
        )

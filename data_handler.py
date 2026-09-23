"""RSS 订阅数据持久化。

改进点：
- 数据文件放到 AstrBot 的插件数据目录（``data/plugin_data/<plugin>/``），
  并对旧版本写在 ``data/astrbot_plugin_rss_data.json`` 的数据做自动迁移；
- 保存改为「先写临时文件再原子替换」，避免进程被杀导致数据文件损坏；
- 读取时做结构自检，避免手工改坏 json 后插件整体起不来；
- 新增若干供 WebUI 插件页调用的查询/修改方法。
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
from html import unescape
from pathlib import Path
from typing import Any, Optional
from urllib.parse import parse_qsl, unquote, urlencode, urljoin, urlparse, urlunparse

from bs4 import BeautifulSoup
from lxml import etree

logger = logging.getLogger("astrbot")

PLUGIN_NAME = "astrbot_plugin_rss_plus"
DATA_FILENAME = "rss_data.json"
LEGACY_DATA_FILE = Path("data") / "astrbot_plugin_rss_data.json"
RESERVED_KEYS = {"rsshub_endpoints", "settings"}


def _resolve_data_dir() -> Path:
    """定位插件数据目录，兼容不同版本的 AstrBot。"""
    try:  # AstrBot >= 4.x
        from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path

        return Path(get_astrbot_plugin_data_path()) / PLUGIN_NAME
    except Exception:  # noqa: BLE001 - 旧版本或单元测试环境
        pass
    try:  # 旧版本 SDK
        from astrbot.api.star import StarTools  # type: ignore

        return Path(StarTools.get_data_dir(PLUGIN_NAME))
    except Exception:  # noqa: BLE001
        return Path("data") / PLUGIN_NAME


class DataHandler:
    def __init__(self, config_path: Optional[str] = None, default_config: Optional[dict] = None):
        if config_path:
            self.config_path = Path(config_path)
        else:
            self.config_path = _resolve_data_dir() / DATA_FILENAME
        self.default_config = default_config or {"rsshub_endpoints": []}
        self.data: dict = {}
        self._migrate_legacy_file()
        self.load_data()

    # ------------------------------------------------------------------ 读写
    def _migrate_legacy_file(self) -> None:
        """把旧版本写在项目根目录的数据迁移到插件数据目录。"""
        try:
            if self.config_path.exists() or not LEGACY_DATA_FILE.exists():
                return
            self.config_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(LEGACY_DATA_FILE, self.config_path)
            logger.info("rss: 已将旧数据 %s 迁移到 %s", LEGACY_DATA_FILE, self.config_path)
        except Exception as exc:  # noqa: BLE001
            logger.warning("rss: 迁移旧数据失败: %s", exc)

    def load_data(self) -> dict:
        """从数据文件中加载数据，文件不存在或损坏时回退到默认结构。"""
        try:
            if not self.config_path.exists():
                self.config_path.parent.mkdir(parents=True, exist_ok=True)
                self.data = json.loads(json.dumps(self.default_config))
                self.save_data()
                return self.data
            with open(self.config_path, "r", encoding="utf-8") as f:
                raw = json.load(f)
            self.data = raw if isinstance(raw, dict) else json.loads(json.dumps(self.default_config))
        except Exception as exc:  # noqa: BLE001
            logger.error("rss: 读取数据文件失败，使用空数据: %s", exc)
            self.data = json.loads(json.dumps(self.default_config))
        self._ensure_shape()
        return self.data

    def _ensure_shape(self) -> None:
        if not isinstance(self.data.get("rsshub_endpoints"), list):
            self.data["rsshub_endpoints"] = []

    def save_data(self) -> None:
        """原子写入，避免半截文件。"""
        try:
            self.config_path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = self.config_path.with_suffix(".tmp")
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(self.data, f, indent=2, ensure_ascii=False)
            os.replace(tmp_path, self.config_path)
        except Exception as exc:  # noqa: BLE001
            logger.error("rss: 保存数据失败: %s", exc)

    # ------------------------------------------------------------------ 查询
    @staticmethod
    def _is_channel(info: Any) -> bool:
        return isinstance(info, dict) and isinstance(info.get("subscribers"), dict)

    def iter_channels(self):
        """遍历所有订阅频道：(url, info)。"""
        for url, info in list(self.data.items()):
            if url in RESERVED_KEYS or not self._is_channel(info):
                continue
            yield url, info

    def get_subs_channel_url(self, user_id: str) -> list[str]:
        """获取某个会话订阅的频道 url 列表。"""
        subs_url = []
        for url, info in self.iter_channels():
            if user_id in info["subscribers"]:
                subs_url.append(url)
        return subs_url

    def get_endpoints(self) -> list[str]:
        value = self.data.get("rsshub_endpoints")
        return value if isinstance(value, list) else []

    def overview(self) -> list[dict]:
        """给插件页面用的订阅总览（含每个订阅会话的独立设置）。"""
        result = []
        for url, info in self.iter_channels():
            subscribers = info.get("subscribers", {})
            details = []
            for user, sub in subscribers.items():
                if not isinstance(sub, dict):
                    continue
                details.append(
                    {
                        "user": user,
                        "cron_expr": str(sub.get("cron_expr", "* * * * *")),
                        "max_pic_item": sub.get("max_pic_item"),
                    }
                )
            details.sort(key=lambda x: x["user"])
            result.append(
                {
                    "url": url,
                    "title": (info.get("info") or {}).get("title", "未知频道"),
                    "description": (info.get("info") or {}).get("description", ""),
                    "subscriber_count": len(subscribers),
                    "subscribers": list(subscribers.keys()),
                    "subscriber_details": details,
                    "cron_expr": details[0]["cron_expr"] if details else "* * * * *",
                }
            )
        return sorted(result, key=lambda x: x["title"])

    # ------------------------------------------------------------------ 修改
    def delete_channel(self, url: str) -> bool:
        if url in self.data and self._is_channel(self.data[url]):
            self.data.pop(url, None)
            self.save_data()
            return True
        return False

    def set_cron(self, url: str, user: str, cron_expr: str) -> bool:
        info = self.data.get(url)
        if not self._is_channel(info) or user not in info["subscribers"]:
            return False
        info["subscribers"][user]["cron_expr"] = cron_expr
        self.save_data()
        return True

    def has_subscription(self, url: str, user: str) -> bool:
        """该 (订阅源, 会话) 是否存在。"""
        info = self.data.get(url)
        if not self._is_channel(info):
            return False
        return isinstance(info["subscribers"].get(user), dict)

    def get_pic_limit(self, url: str, user: str):
        """读取某个订阅会话的独立图片上限。

        Returns:
            None  表示未单独设置（跟随全局配置）；
            0     表示该订阅不发送图片；
            -1    表示不限制；
            n > 0 表示最多 n 张。
        """
        info = self.data.get(url)
        if not self._is_channel(info):
            return None
        sub = info["subscribers"].get(user)
        if not isinstance(sub, dict):
            return None
        value = sub.get("max_pic_item", None)
        if value is None:
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    def set_pic_limit(self, url: str, user: str, value) -> bool:
        """设置某个订阅会话的独立图片上限；value 为 None 表示恢复跟随全局。"""
        info = self.data.get(url)
        if not self._is_channel(info):
            return False
        sub = info["subscribers"].get(user)
        if not isinstance(sub, dict):
            return False
        if value is None:
            sub.pop("max_pic_item", None)
        else:
            sub["max_pic_item"] = int(value)
        self.save_data()
        return True

    def delete_subscriber(self, url: str, user: str) -> bool:
        info = self.data.get(url)
        if not self._is_channel(info) or user not in info["subscribers"]:
            return False
        info["subscribers"].pop(user, None)
        if not info["subscribers"]:
            self.data.pop(url, None)
        self.save_data()
        return True

    # ------------------------------------------------------------------ HTML
    def parse_channel_text_info(self, text) -> tuple[str, str]:
        """解析 RSS 频道信息。

        找不到时返回空字符串（而不是占位文案），由调用方决定是否兜底，
        否则「随便一个能解析的地址」都会被当成有效订阅。
        """
        root = etree.fromstring(text)
        titles = root.xpath("/rss/channel/title | //channel/title | //feed/title | //title")
        descs = root.xpath(
            "/rss/channel/description | //channel/description | //subtitle | //description | //summary"
        )
        title = (titles[0].text or "").strip() if titles and titles[0].text else ""
        description = (descs[0].text or "").strip() if descs and descs[0].text else ""
        return title, description

    # 媒体标签：RSSHub 微博路由会渲染出 video/iframe 等，直接整块丢弃
    MEDIA_TAG_RE = re.compile(
        r"<\s*(video|audio|iframe|embed|object|source|track)\b[\s\S]*?<\s*/\s*\1\s*>"
        r"|<\s*(video|audio|iframe|embed|object|source|track)\b[^>]*?/?>",
        re.I,
    )
    ANCHOR_RE = re.compile(r"<\s*a\b[\s\S]*?<\s*/\s*a\s*>", re.I)
    VIDEO_HREF_RE = re.compile(r"weibo\.com/tv|//video\.|/tv/show|/video/|video\.weibo", re.I)
    # 视频封面（poster）与视频播放页链接：必须在删掉 video 块之前提取
    VIDEO_TAG_RE = re.compile(r"<\s*video\b[^>]*>", re.I)
    VIDEO_BLOCK_RE = re.compile(r"<\s*video\b[\s\S]*?(?:<\s*/\s*video\s*>|$)", re.I)
    POSTER_ATTR_RE = re.compile(r"""poster\s*=\s*["']([^"']+)["']""", re.I)
    HREF_ATTR_RE = re.compile(r"""href\s*=\s*["']([^"']+)["']""", re.I)
    # 正文里任何微博视频播放页链接（用于没有 <video> 块 / 块里没有链接时兜底）
    VIDEO_PAGE_URL_RE = re.compile(
        r"https?://[^\s<>\"']*?(?:video\.weibo\.com|h5\.video\.weibo\.com|weibo\.com/tv/show|m\.weibo\.cn/tv)[^\s<>\"']*",
        re.I,
    )
    # 视频 oid，形如 1034:5345988664295458
    VIDEO_OID_RE = re.compile(r"(\d{2,6}:\d{6,})")

    # 视频播放页链接里的跟踪参数（只保留 fid 之类的必要参数）
    VIDEO_TRACKING_PARAMS = frozenset(
        {
            "luicode", "launchid", "extparam", "from", "wm", "featurecode",
            "uicode", "mod", "act", "v_p", "c", "suda", "request_id",
        }
    )

    # 正文里的视频噪音文案（微博视频占位说明等）
    VIDEO_NOISE_PATTERNS = (
        # 注意：链接可能先被 strip_video_noise 去掉，所以「微博视频」四个字要允许缺失
        re.compile(r"视频无法显示[，,、]?\s*请前往\s*(?:微博视频)?\s*观看[。.!！]?"),
        re.compile(r"视频无法显示[。.!！]?"),
        re.compile(r"请前往\s*(?:微博视频)?\s*观看[。.!！]?"),
        re.compile(r"[\u4e00-\u9fa5A-Za-z0-9_\-]{0,24}的微博视频"),  # 例如「知任CHS的微博视频」
        re.compile(r"微博视频"),
        re.compile(r"网页链接"),
        re.compile(r"[▶►]\s*"),
    )

    # 明显是播放图标 / 视频占位图，不作为图片发送
    PIC_SKIP_PATTERNS = (
        re.compile(r"timeline_card_small_video", re.I),
        re.compile(r"video_default", re.I),
        re.compile(r"video_placeholder", re.I),
        re.compile(r"/video[_/]", re.I),
        re.compile(r"play[_/]?icon", re.I),
        re.compile(r"/icon_[a-z0-9_]*video", re.I),
    )

    # 常见懒加载属性（按优先级）
    PIC_ATTRS = (
        "data-src",
        "data-original",
        "data-lazy-src",
        "data-echo",
        "data-actualsrc",
        "data-url",
        "src",
    )
    # 占位图 / 无意义地址，直接跳过
    PIC_SKIP = (
        "data:image/gif;base64,r0lgod",  # 1x1 透明占位（base64 前缀）
        "data:image/svg+xml",
        "about:blank",
        "loading.gif",
        "spacer.gif",
        "blank.gif",
        "placeholder",
    )

    def strip_html_pic(self, html, base_url: str = "") -> list[str]:
        """解析 HTML 内容，提取图片地址。

        会依次尝试 data-src / data-original 等懒加载属性，兼容 srcset，
        跳过 1x1 占位图，并把相对地址 / 协议相对地址补全成可请求的 URL。
        """
        from html import unescape
        from urllib.parse import urljoin

        soup = BeautifulSoup(html or "", "html.parser")
        ordered_content: list[str] = []
        for img in soup.find_all("img"):
            value = None
            for attr in self.PIC_ATTRS:
                try:
                    candidate = img.get(attr)
                except Exception:  # noqa: BLE001
                    candidate = None
                if candidate:
                    value = candidate
                    break
            if not value:
                for attr in ("srcset", "data-srcset"):
                    try:
                        srcset = img.get(attr)
                    except Exception:  # noqa: BLE001
                        srcset = None
                    if srcset:
                        value = srcset.split(",")[0].strip().split(" ")[0]
                        break
            if not value:
                continue

            url = unescape(str(value).strip().strip('"').strip("'"))
            if not url:
                continue
            lowered = url.lower()
            if any(hint in lowered for hint in self.PIC_SKIP):
                continue
            if self._is_video_asset(url):
                continue
            if url.startswith("//"):
                url = "https:" + url
            elif base_url and not re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*:", url):
                url = urljoin(base_url, url)
            if url not in ordered_content:
                ordered_content.append(url)
        return ordered_content

    def strip_video_noise(self, html: str) -> str:
        """丢掉视频相关的 HTML 片段（video 标签、指向微博视频的链接等）。"""
        if not html:
            return ""

        def _drop_media(match: "re.Match") -> str:
            return " "

        text = self.MEDIA_TAG_RE.sub(_drop_media, html)

        def _drop_anchor(match: "re.Match") -> str:
            block = match.group(0)
            inner = re.sub(r"<[^>]+>", "", block).strip()
            href_match = re.search(r'href\s*=\s*["\']([^"\']*)["\']', block, re.I)
            href = href_match.group(1) if href_match else ""
            if re.search(r"微博视频|视频无法显示", inner):
                return " "
            if re.search(r"视频", inner) and self.VIDEO_HREF_RE.search(href):
                return " "
            if self.VIDEO_HREF_RE.search(href):
                return " "
            return block

        return self.ANCHOR_RE.sub(_drop_anchor, text)

    def clean_plain_text(self, text: str) -> str:
        """清掉微博视频之类的固定噪音文案，并整理多余空白。"""
        if not text:
            return ""
        for pattern in self.VIDEO_NOISE_PATTERNS:
            text = pattern.sub("", text)
        text = re.sub(r"[ \t\u3000]{2,}", " ", text)
        text = re.sub(r"\n{2,}", "\n", text)
        text = re.sub(r"^[，,、。.；;：:\s]+", "", text)
        return text.strip()

    def parse_description(self, html, base_url: str = "") -> tuple[str, list[str]]:
        """一次解析出正文纯文本与图片地址，并过滤视频噪音。

        注意：这里只处理**正文图片**。视频帖的封面图与播放页链接请另外调用
        :meth:`extract_video_info`——封面不算正文图片，它有自己的开关和优先级。
        """
        cleaned = self.strip_video_noise(html or "")
        text = self.clean_plain_text(self.strip_html(cleaned))
        pics = [p for p in self.strip_html_pic(cleaned, base_url) if not self._is_video_asset(p)]
        return text, pics

    def extract_video_info(self, html: str, base_url: str = "") -> dict:
        """从（还没被清理的）HTML 里取出视频封面图、视频播放页链接与视频 oid。

        RSSHub 的微博视频帖长这样::

            <video controls="controls" poster="https://tvax2.sinaimg.cn/orj480/xxx.jpg">
                <source src="https://f.video.weibocdn.com/xxx.mp4">
                <p>视频无法显示，请前往<a href="https://video.weibo.com/show?fid=...">微博视频</a>观看。</p>
            </video>

        封面就是 ``poster``（微博 API 的 page_info.page_pic / livephoto 大图），
        视频播放页链接在里面的 ``<a href>`` 上。两者都必须在 strip_video_noise
        把整个 video 块删掉之前取出来。

        Returns:
            ``{"covers": [...], "video_url": str, "oid": str}``；
            ``oid`` 是视频号（如 ``1034:5345988664295458``），RSSHub 没给 poster 时
            可以拿它去微博 H5 接口补一张封面。
        """
        result = {"covers": [], "video_url": "", "oid": ""}
        if not html:
            return result

        covers: list[str] = []
        for tag in self.VIDEO_TAG_RE.findall(html):
            match = self.POSTER_ATTR_RE.search(tag)
            if not match:
                continue
            url = self._absolutize_url(match.group(1), base_url)
            if url and url not in covers and not self._is_video_asset(url):
                covers.append(url)

        video_url = ""
        for block in self.VIDEO_BLOCK_RE.findall(html):
            href = self.HREF_ATTR_RE.search(block)
            if href and self.VIDEO_HREF_RE.search(href.group(1)):
                video_url = self.clean_video_url(unescape(href.group(1)).strip())
                if video_url:
                    break

        # 没有 <video> 块（或块里没有链接）时，正文里可能还留着微博视频链接
        if not video_url:
            for candidate in self.VIDEO_PAGE_URL_RE.findall(html):
                cleaned = self.clean_video_url(unescape(candidate).strip())
                if cleaned and self.VIDEO_HREF_RE.search(cleaned):
                    video_url = cleaned
                    break

        result["covers"] = covers
        result["video_url"] = video_url
        result["oid"] = self.video_oid_from_url(video_url)
        return result

    @classmethod
    def video_oid_from_url(cls, url: str) -> str:
        """从视频播放页链接里抠出 oid（``1034:5345988664295458``）。"""
        if not url:
            return ""
        match = cls.VIDEO_OID_RE.search(unquote(url))
        return match.group(1) if match else ""

    @staticmethod
    def clean_video_url(url: str) -> str:
        """去掉视频播放页链接里的跟踪参数。

        ``https://video.weibo.com/show?fid=1034%3A123&luicode=20000174&launchid=...``
        → ``https://video.weibo.com/show?fid=1034%3A123``
        """
        if not url:
            return ""
        try:
            parsed = urlparse(url)
            params = parse_qsl(parsed.query, keep_blank_values=True)
            fids = [v for k, v in params if k == "fid"]
            if fids and parsed.path.rstrip("/").endswith("/show"):
                return urlunparse(parsed._replace(query=urlencode([("fid", fids[0])])))
            kept = [(k, v) for k, v in params if k.lower() not in DataHandler.VIDEO_TRACKING_PARAMS]
            return urlunparse(parsed._replace(query=urlencode(kept) if kept else ""))
        except Exception:  # noqa: BLE001
            return url

    def _absolutize_url(self, url: str, base_url: str = "") -> str:
        """把图片地址补全成可请求的绝对地址。"""
        url = unescape(str(url or "")).strip().strip('"').strip("'")
        if not url:
            return ""
        if url.startswith("//"):
            return "https:" + url
        if base_url and not re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*:", url):
            try:
                return urljoin(base_url, url)
            except Exception:  # noqa: BLE001
                return url
        return url

    def _is_video_asset(self, url: str) -> bool:
        """判断图片地址是不是播放图标之类的视频素材。"""
        if not url:
            return True
        return any(p.search(url) for p in self.PIC_SKIP_PATTERNS)

    def strip_html(self, html) -> str:
        """去除HTML标签。"""
        soup = BeautifulSoup(html or "", "html.parser")
        text = soup.get_text()
        return re.sub(r"\n+", "\n", text).strip()

    def get_root_url(self, url) -> str:
        """获取URL的根域名。"""
        from urllib.parse import urlparse

        parsed_url = urlparse(url)
        return f"{parsed_url.scheme}://{parsed_url.netloc}"

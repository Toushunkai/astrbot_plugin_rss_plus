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

    # ------------------------------------------------------------------ 界面图标
    #
    # RSSHub 的微博路由会把「查看图片 / 评论配图 / 超话 / 表情 / 视频 / 文章」
    # 前面的小图标统一包成 ``<span class="url-icon"><img …></span>``；这些是
    # 界面素材，不是正文配图。一旦被当成图片抓取，推送里就会冒出一堆 1rem 的
    # 小图标（如 ``timeline_card_small_photo_default.png``、
    # ``timeline_card_small_super_default.png``）。
    #
    # 过滤分三层：结构（图标容器）→ 地址特征（图标目录 / 关键词）→ 声明尺寸，
    # 同时把「查看图片 / 评论配图」链接里真正的图片地址从 href 上取回来。
    # ------------------------------------------------------------------

    # 图标容器：出现在这些 class 里的 <img> 一律不是正文配图
    ICON_WRAPPER_CLASSES = frozenset(
        {"url-icon", "icon", "icons", "emoji", "emoticon", "face", "avatar", "badge"}
    )

    # 明确的 UI 素材目录：这些目录下不论文件名是什么，都不是正文图
    ICON_DIR_RE = re.compile(
        r"(?:sinaimg\.cn/(?:upload|photo|m/emoticon)/|weibo\.com/aj/static/|sinajs\.cn/)",
        re.I,
    )
    # 新浪 / 微博的「正文配图」目录：命中即认定为正文图，避免被关键词误杀
    CONTENT_IMAGE_RE = re.compile(
        r"sinaimg\.cn/(?:large|mw\d+|orj\d+|woriginal|bmiddle|small|square|thumb\d+|crop[^/]*)/",
        re.I,
    )
    # 图标地址里的关键词（只在「不像正文图」时才生效）
    ICON_KEYWORDS = (
        "timeline_card", "weibo_card", "card_icon", "favicon", "avatar",
        "emoticon", "/emote/", "emote_", "/emoji", "/icon/", "icon_", "icon-",
        "-icon", "_icon", "sprite", "spacer", "placeholder", "loading.gif",
        "blank.gif", "pixel.gif", "transparent.gif", "1x1", "qrcode", "qr_code",
        "/logo", "logo.", "badge",
    )
    # 图片扩展名（判断链接指向的是图片而不是网页）
    IMAGE_EXT_RE = re.compile(r"\.(?:jpe?g|png|gif|webp|bmp|avif)(?:[?#]|$)", re.I)
    # 微博把外链包成 https://weibo.cn/sinaurl?u=<编码后的真实地址>
    SINAURL_RE = re.compile(r"weibo\.cn/sinaurl\?(?:[^#\s]*?&)?u=([^&#\s]+)", re.I)
    # 常见图床域名：用于识别「图片在 href 上」的链接
    IMAGE_HOST_HINT_RE = re.compile(
        r"(?:sinaimg\.cn|weibocdn\.com|qpic\.cn|zhimg\.com|hdslb\.com|doubanio\.com|alicdn\.com|byteimg\.com)/",
        re.I,
    )
    # 「查看图片 / 评论配图 / 查看动图」这类链接文字
    IMAGE_ANCHOR_TEXT_RE = re.compile(
        r"^[\s【\[(（]*?(查看图片|查看原图|查看动图|评论配图|原图|图片)[\s】\])）]*?$"
    )
    # <img> 自己的 class 里有 icon / avatar / emoji 这样的词 → 明确是图标
    ICON_CLASS_RE = re.compile(
        r"(?:^|[-_])(?:icon|icons|avatar|emoji|emoticon|face|badge|logo)(?:[-_]|$)", re.I
    )
    # 图标容器整块（用于把表情图标换回 alt 文案）
    URL_ICON_BLOCK_RE = re.compile(
        r"<\s*span\b[^>]*class\s*=\s*[\"']?[^\"'>]*\burl-icon\b[^\"'>]*[\"']?[^>]*>"
        r"[\s\S]*?<\s*/\s*span\s*>",
        re.I,
    )
    ALT_ATTR_RE = re.compile(r"""\balt\s*=\s*["']([^"']*)["']""", re.I)

    # ------------------------------------------------------------------ 图标判定
    @classmethod
    def _has_icon_keyword(cls, url: str) -> bool:
        low = (url or "").lower()
        return any(key in low for key in cls.ICON_KEYWORDS)

    @classmethod
    def _looks_like_content_image(cls, url: str) -> bool:
        """地址看起来是「正文配图」而不是界面图标。"""
        low = (url or "").lower()
        if not low:
            return False
        if cls.ICON_DIR_RE.search(low):
            return False  # 明确的 UI 素材目录
        if cls.CONTENT_IMAGE_RE.search(low):
            return True  # 微博正文图目录
        if cls._has_icon_keyword(low):
            return False
        return bool(cls.IMAGE_EXT_RE.search(low))

    @classmethod
    def _declared_size_is_icon(cls, img) -> bool:
        """<img> 自己声明了很小的尺寸（1rem / 20px 之类）→ 图标。"""
        style = str(img.get("style") or "").lower()
        for match in re.finditer(r"(?:width|height)\s*:\s*([\d.]+)\s*(rem|em|px|pt)?", style):
            try:
                value = float(match.group(1))
            except ValueError:
                continue
            unit = (match.group(2) or "px").lower()
            if value <= (2.5 if unit in ("rem", "em") else 48):
                return True
        for attr in ("width", "height"):
            raw = str(img.get(attr) or "").strip()
            if raw.isdigit() and int(raw) <= 48:
                return True
        return False

    @staticmethod
    def _has_tiny_box(img) -> bool:
        """width / height 两个属性都写了而且都很小 → 头像 / 缩略图 / 图标。

        微博作者头像是 ``<img width="48" height="48" src="…/crop.0.0.180.180.180/…">``，
        地址本身长得像正文图，只能靠声明的尺寸认出来。
        两个属性都 ≥ 2 才算，避免把「1×1 占位 + data-src 真图」的懒加载写法误杀。
        """
        try:
            width = str(img.get("width") or "").strip()
            height = str(img.get("height") or "").strip()
        except Exception:  # noqa: BLE001
            return False
        if not (width.isdigit() and height.isdigit()):
            return False
        w, h = int(width), int(height)
        return 2 <= w <= 48 and 2 <= h <= 48

    @staticmethod
    def _img_src(img) -> str:
        """按优先级取出 <img> 的地址（含懒加载属性与 srcset）。"""
        for attr in DataHandler.PIC_ATTRS:
            try:
                candidate = img.get(attr)
            except Exception:  # noqa: BLE001
                candidate = None
            if candidate:
                return str(candidate).strip()
        for attr in ("srcset", "data-srcset"):
            try:
                srcset = img.get(attr)
            except Exception:  # noqa: BLE001
                srcset = None
            if srcset:
                return str(srcset).split(",")[0].strip().split(" ")[0]
        return ""

    def _image_url_from_anchor(self, anchor, base_url: str = "") -> str:
        """从「真实图片在链接上」的 <a> 里取出图片地址。

        RSSHub 微博路由会输出::

            <a href="https://…/large/xxx.jpg" data-rsshub-image="href">
                <span class="url-icon"><img src="…/timeline_card_small_photo_default.png"></span>
                <span class="surl-text">评论配图</span>
            </a>

        此时小图标必须丢掉，真正的图片在 ``href`` 上（RSSHub 用
        ``data-rsshub-image="href"`` 标记，或链接被包成
        ``https://weibo.cn/sinaurl?u=<编码后的图片地址>``）。
        超话那种指向网页的链接则什么都不返回。
        """
        try:
            href = str(anchor.get("href") or "").strip()
        except Exception:  # noqa: BLE001
            href = ""
        if not href:
            return ""
        marked = str(anchor.get("data-rsshub-image") or "").strip().lower() == "href"

        # 微博外链：https://weibo.cn/sinaurl?u=<编码后的真实地址>
        sina = self.SINAURL_RE.search(href)
        if sina:
            href = unquote(sina.group(1))

        href = self._absolutize_url(unescape(href), base_url)
        if not href.lower().startswith(("http://", "https://")):
            return ""
        if self.VIDEO_HREF_RE.search(href) or self._is_video_asset(href):
            return ""  # 视频播放页链接不是图片

        looks_image = bool(self.IMAGE_EXT_RE.search(href))
        if marked or looks_image:
            return href
        # 少数情况地址被短链包裹：只有链接文字明说「查看图片」才认
        if self.IMAGE_HOST_HINT_RE.search(href):
            try:
                text = anchor.get_text(" ", strip=True)
            except Exception:  # noqa: BLE001
                text = ""
            if self.IMAGE_ANCHOR_TEXT_RE.match(text or ""):
                return href
        return ""

    @staticmethod
    def _node_classes(node) -> list[str]:
        """取节点的 class 列表（真实 bs4 给的是 list，桩可能是字符串）。"""
        try:
            raw = node.get("class")
        except Exception:  # noqa: BLE001
            return []
        if not raw:
            return []
        if isinstance(raw, str):
            raw = raw.split()
        try:
            return [str(item).lower() for item in raw]
        except TypeError:
            return [str(raw).lower()]

    def _is_ui_icon_img(self, img, base_url: str = "") -> bool:
        """判断 <img> 是不是界面图标（而不是正文配图）。"""
        value = self._img_src(img)
        url = self._absolutize_url(value, base_url) if value else ""
        if url.lower().startswith("data:"):
            return False  # 内联图片交给 pic_handler / PIC_SKIP 处理
        is_content = self._looks_like_content_image(url)

        for parent in list(img.parents)[:6]:
            name = getattr(parent, "name", "") or ""
            if name in ("html", "body", "[document]"):
                break
            classes = self._node_classes(parent)
            if any(cls in self.ICON_WRAPPER_CLASSES for cls in classes):
                return True  # 图标容器（url-icon 等）
            if not is_content and any(
                key in cls for cls in classes for key in ("icon", "avatar", "emoji", "face", "badge")
            ):
                return True

        # <img> 自己带 icon / avatar / emoji 之类的 class：这是元素级的明确声明，
        # 即便地址长得像正文图也当图标（注意用词边界，"iconic" 之类不算）
        own = self._node_classes(img)
        if own and any(
            cls in self.ICON_WRAPPER_CLASSES or self.ICON_CLASS_RE.search(cls) for cls in own
        ):
            return True

        # 作者头像之类：宽高属性都写得很小（微博头像是 48×48）
        if self._has_tiny_box(img):
            return True

        if is_content:
            return False
        if self._declared_size_is_icon(img):
            return True
        return self._has_icon_keyword(url)

    def strip_html_pic(self, html, base_url: str = "") -> list[str]:
        """解析 HTML 内容，提取**正文图片**地址。

        会过滤掉界面小图标（``url-icon`` 容器、微博卡片图标目录、1rem / 20px
        之类的小图标），并把「查看图片 / 评论配图」链接里真正的图片地址从
        ``href`` 上取回来；依次尝试 data-src / data-original 等懒加载属性，
        兼容 srcset，跳过 1x1 占位图，并把相对地址 / 协议相对地址补全成可请求
        的 URL。
        """
        soup = BeautifulSoup(html or "", "html.parser")
        ordered_content: list[str] = []
        seen: set[str] = set()

        def _append(url: str) -> None:
            if url and url not in seen:
                seen.add(url)
                ordered_content.append(url)

        # 按文档顺序遍历：<a> 一定排在自己内部的 <img> 之前，
        # 所以「图片在链接上」时，真实图片正好落在小图标原来的位置上。
        for node in soup.find_all(["a", "img"]):
            if node.name == "a":
                real = self._image_url_from_anchor(node, base_url)
                if real:
                    _append(real)
                continue

            # <img>：先看它是不是「图片链接」里充当门面的小图标
            anchor = next((p for p in node.parents if getattr(p, "name", "") == "a"), None)
            if anchor is not None and self._image_url_from_anchor(anchor, base_url):
                continue  # 该链接真正的图片已经按 href 收好了

            if self._is_ui_icon_img(node, base_url):
                continue
            value = self._img_src(node)
            if not value:
                continue
            url = self._absolutize_url(unescape(value.strip().strip('"').strip("'")), base_url)
            if not url:
                continue
            if any(hint in url.lower() for hint in self.PIC_SKIP):
                continue
            if self._is_video_asset(url):
                continue
            _append(url)
        return ordered_content

    def inline_icon_alt(self, html: str) -> str:
        """把图标容器换成它的 alt 文案（表情），没有 alt 的直接删掉。

        微博表情是 ``<span class="url-icon"><img alt="[笑cry]" …></span>``，
        在纯文本里本来就该显示成 ``[笑cry]``；而「查看图片 / 超话」那种没有
        alt 的小图标则直接消失，不再作为图片发出去。
        """
        if not html:
            return ""

        def _replace(match: "re.Match") -> str:
            alt = self.ALT_ATTR_RE.search(match.group(0))
            return alt.group(1) if alt else " "

        return self.URL_ICON_BLOCK_RE.sub(_replace, html)

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
        """一次解析出正文纯文本与图片地址，并过滤视频噪音与界面图标。

        注意：这里只处理**正文图片**。视频帖的封面图与播放页链接请另外调用
        :meth:`extract_video_info`——封面不算正文图片，它有自己的开关和优先级。
        正文图片里也不会出现「查看图片 / 超话 / 表情」前面的小图标（见
        :meth:`strip_html_pic`）；表情图标会还原成 ``[笑cry]`` 这样的文案。
        """
        cleaned = self.strip_video_noise(html or "")
        text = self.clean_plain_text(self.strip_html(self.inline_icon_alt(cleaned)))
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

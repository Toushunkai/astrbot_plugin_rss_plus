"""RSS 图片处理。

相比旧版的关键改进（都是为了不再出现「图片链接读取失败」）：

1. **不信任 Content-Type**：优先嗅探文件头魔数（JPEG/PNG/GIF/WEBP/BMP/TIFF）。
   很多图床/CDN 会返回 ``application/octet-stream`` 甚至 ``binary/octet-stream``，
   按 Content-Type 白名单过滤会误杀。
2. **防盗链**：第一次请求带上图片自身域名的 ``Referer`` 与 ``Accept: image/*``，
   失败后再退化为不带 Referer 重试一次。
3. **URL 规整**：处理 ``&amp;`` 转义、``//host/a.jpg`` 协议相对地址、
   相对路径、URL 中的空格与中文等非法字符。
4. **支持 data: URI**（部分 RSS 直接把图片内联在正文里）。
5. **超时 / 体积 / 类型**三重保护，并且每条失败原因都会写进日志，
   可用 ``/rss pic-test <url>`` 单独诊断。
"""

from __future__ import annotations

import asyncio
import base64
import html as html_lib
import logging
import random
import re
from io import BytesIO
from typing import Optional
from urllib.parse import quote, urljoin, urlparse

import aiohttp
from PIL import Image

logger = logging.getLogger("astrbot")

MAX_IMAGE_BYTES = 12 * 1024 * 1024  # 单张图片最大 12MB
DEFAULT_TIMEOUT = aiohttp.ClientTimeout(total=25, connect=10)

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

# 文件头魔数 -> 类型
_MAGIC: list[tuple[bytes, str]] = [
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
    (b"BM", "image/bmp"),
    (b"II*\x00", "image/tiff"),
    (b"MM\x00*", "image/tiff"),
]

_TEXTUAL_IMAGE_HINTS = (b"<svg", b"<?xml", b"<html", b"<!doctype html")

# 部分 CDN 要求 Referer 必须是「自家站点」，否则 403。
# 实测（2026-09）微博 sinaimg：不带 Referer -> 403；带 https://weibo.com/ 或自身域名 -> 200。
_REFERER_BY_HOST: tuple[tuple[str, str], ...] = (
    ("sinaimg.cn", "https://weibo.com/"),
    ("weibo.com", "https://weibo.com/"),
    ("weibocdn.com", "https://weibo.com/"),
    ("sinajs.cn", "https://weibo.com/"),
    ("pximg.net", "https://www.pixiv.net/"),
    ("pixiv.net", "https://www.pixiv.net/"),
    ("zhimg.com", "https://www.zhihu.com/"),
    ("hdslb.com", "https://www.bilibili.com/"),
    ("bilibili.com", "https://www.bilibili.com/"),
    ("doubanio.com", "https://www.douban.com/"),
    ("qpic.cn", "https://mp.weixin.qq.com/"),
    ("qlogo.cn", "https://mp.weixin.qq.com/"),
    ("alicdn.com", "https://www.taobao.com/"),
    ("xhscdn.com", "https://www.xiaohongshu.com/"),
    ("xiaohongshu.com", "https://www.xiaohongshu.com/"),
    ("twimg.com", "https://twitter.com/"),
    ("byteimg.com", "https://www.toutiao.com/"),
    ("toutiaoimg.com", "https://www.toutiao.com/"),
    ("zcool.cn", "https://www.zcool.com.cn/"),
    ("lofter.com", "https://www.lofter.com/"),
    ("baidu.com", "https://www.baidu.com/"),
    ("bdimg.com", "https://www.baidu.com/"),
    ("bdstatic.com", "https://www.baidu.com/"),
)

# HTTP 状态码：这些情况换 Referer 重试是有意义的
_RETRYABLE_STATUS = {None, 401, 403, 404, 405, 406, 412, 429, 451, 500, 502, 503, 504}


def site_referer(url: str) -> Optional[str]:
    """按图片域名给出该站点「自家」的 Referer。"""
    try:
        host = (urlparse(url).netloc or "").lower()
    except Exception:  # noqa: BLE001
        return None
    if not host:
        return None
    if host.startswith("www."):
        host = host[4:]
    for suffix, referer in _REFERER_BY_HOST:
        if host == suffix or host.endswith("." + suffix):
            return referer
    return None


def referer_candidates(url: str) -> list[tuple[str, Optional[str]]]:
    """按优先级给出要依次尝试的 Referer 列表。

    1. 图片自身域名（实测对 sinaimg 有效，且对绝大多数 CDN 安全）；
    2. 该站点的官方域名（应对只认自家 Referer 的图床）；
    3. 不带 Referer（应对完全不允许带 Referer 的对象存储）。
    """
    candidates: list[tuple[str, Optional[str]]] = []
    try:
        parts = urlparse(url)
        if parts.scheme and parts.netloc:
            candidates.append(("自带域名 Referer", f"{parts.scheme}://{parts.netloc}/"))
    except Exception:  # noqa: BLE001
        pass
    site = site_referer(url)
    if site and all(site != ref for _, ref in candidates):
        candidates.append(("站点 Referer", site))
    candidates.append(("无 Referer", None))
    return candidates
_DATA_URI_RE = re.compile(r"^data:(?P<mime>[^;,]+)?(?P<b64>;base64)?,(?P<data>.*)$", re.S | re.I)


def sniff_image_type(data: bytes) -> Optional[str]:
    """通过文件头判断图片类型，判断不出来返回 None。"""
    if not data or len(data) < 12:
        return None
    for magic, mime in _MAGIC:
        if data.startswith(magic):
            return mime
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[4:8] == b"ftyp":
        brand = data[8:12]
        if brand in (b"avif", b"avis"):
            return "image/avif"
        if brand in (b"heic", b"heix", b"hevc", b"mif1"):
            return "image/heic"
    return None


def looks_like_markup(data: bytes) -> bool:
    """内容像 HTML/XML（防盗链页面、404 页面）而不是图片。"""
    head = data[:400].lstrip().lower()
    return any(head.startswith(hint) for hint in _TEXTUAL_IMAGE_HINTS)


def image_completeness(data: bytes, kind: Optional[str] = None) -> tuple[bool, str]:
    """判断图片数据是否完整（防止把被截断的图片发出去）。

    JPEG 必须以 EOI(FFD9) 结束、PNG 必须含 IEND、GIF 必须以 ';' 结束。
    数据被截断时某些解码器会把缺失部分渲染成灰块，必须提前拦住。
    """
    if not data:
        return False, "数据为空"
    kind = kind or sniff_image_type(data) or ""
    tail = data[-512:]
    if kind == "image/jpeg":
        if b"\xff\xd9" not in tail:
            return False, "JPEG 数据不完整（缺少 EOI 结束标记，可能被截断）"
        return True, "ok"
    if kind == "image/png":
        if b"IEND" not in data[-32:]:
            return False, "PNG 数据不完整（缺少 IEND 块，可能被截断）"
        return True, "ok"
    if kind == "image/gif":
        if not data.rstrip(b"\x00\r\n ").endswith(b";"):
            return False, "GIF 数据不完整（缺少结束符，可能被截断）"
        return True, "ok"
    if kind in ("image/webp", "image/bmp", "image/tiff"):
        # RIFF 结构自带长度字段，简单校验一下
        if kind == "image/webp" and len(data) >= 8:
            import struct

            declared = struct.unpack("<I", data[4:8])[0] + 8
            if declared > len(data) + 8:
                return False, f"WEBP 数据不完整（声明 {declared} 字节，实际 {len(data)} 字节）"
        return True, "ok"
    return True, "ok"


def normalize_image_url(raw: str, base_url: str = "") -> str:
    """把 RSS 里的各种奇怪写法规整成可请求的 URL。"""
    url = html_lib.unescape((raw or "").strip().strip('"').strip("'"))
    if not url:
        return ""
    if url.startswith("//"):
        url = "https:" + url
    elif url.startswith("/") and base_url:
        url = urljoin(base_url, url)
    elif base_url and not re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*:", url):
        url = urljoin(base_url, url)
    if url.lower().startswith(("http://", "https://")):
        # 处理空格、中文等非法字符，但保留已有的 %xx 与查询串结构
        parts = urlparse(url)
        url = parts._replace(
            path=quote(parts.path, safe="/%:@!$&'()*+,;=~"),
            query=quote(parts.query, safe="=&%:@!$'()*+,;/?~"),
        ).geturl()
    return url


class FetchOutcome:
    """一次图片获取的结果（带失败原因，便于诊断）。"""

    __slots__ = ("data", "reason", "status", "content_type", "url")

    def __init__(self, data: Optional[bytes], reason: str, status: Optional[int] = None,
                 content_type: str = "", url: str = ""):
        self.data = data
        self.reason = reason
        self.status = status
        self.content_type = content_type
        self.url = url

    @property
    def ok(self) -> bool:
        return bool(self.data)

    def __bool__(self) -> bool:  # 兼容 `if outcome:`
        return self.ok


class RssImageHandler:
    """rss 处理图片的类。"""

    def __init__(self, is_adjust_pic: bool = False, timeout: aiohttp.ClientTimeout = DEFAULT_TIMEOUT):
        """
        Args:
            is_adjust_pic: 是否防和谐（随机改一个角像素），默认为 False。
            timeout: 下载图片的超时设置。
        """
        self.is_adjust_pic = is_adjust_pic
        self.timeout = timeout
        self._session: Optional[aiohttp.ClientSession] = None

    # ------------------------------------------------------------------ 会话
    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                trust_env=True,
                timeout=self.timeout,
                headers={"User-Agent": _UA},
            )
        return self._session

    async def _reset_session(self) -> None:
        """会话可能因为事件循环切换 / 连接池异常而失效，直接丢弃重建。"""
        try:
            if self._session and not self._session.closed:
                await self._session.close()
        except Exception:  # noqa: BLE001
            pass
        self._session = None

    async def close(self) -> None:
        await self._reset_session()

    # ------------------------------------------------------------------ 下载
    async def _request_once(self, url: str, referer: Optional[str]) -> FetchOutcome:
        headers = {"Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8"}
        if referer:
            headers["Referer"] = referer
        try:
            session = await self._get_session()
            async with session.get(url, headers=headers, allow_redirects=True) as resp:
                status = resp.status
                ctype = (resp.headers.get("Content-Type") or "").split(";")[0].strip().lower()
                if status != 200:
                    return FetchOutcome(None, f"HTTP {status}", status, ctype, url)

                length = resp.headers.get("Content-Length")
                if length and length.isdigit() and int(length) > MAX_IMAGE_BYTES:
                    return FetchOutcome(None, f"图片过大（{int(length) // 1024 // 1024}MB）", status, ctype, url)

                # 注意：不能写成 resp.content.read(n)！
                # aiohttp 的 StreamReader.read(n)（n > 0）只返回「当前缓冲区的一个分片」，
                # 对大图会直接截断数据，导致图片下半部分变成灰块。
                # 这里用 iter_chunked 一直读到 EOF，并顺便做体积上限保护。
                buf = bytearray()
                too_large = False
                async for chunk in resp.content.iter_chunked(64 * 1024):
                    buf += chunk
                    if len(buf) > MAX_IMAGE_BYTES:
                        too_large = True
                        break
                if too_large:
                    return FetchOutcome(None, "图片超过 12MB 上限", status, ctype, url)
                data = bytes(buf)
                if not data:
                    return FetchOutcome(None, "响应内容为空", status, ctype, url)

                # 如果响应带 Content-Length 且未被压缩，长度不一致说明传输被中断
                encoding = (resp.headers.get("Content-Encoding") or "identity").lower()
                if (
                    length
                    and length.isdigit()
                    and encoding in ("identity", "")
                    and len(data) != int(length)
                ):
                    return FetchOutcome(
                        None,
                        f"图片数据不完整（收到 {len(data)}/{length} 字节，传输被中断）",
                        status,
                        ctype,
                        url,
                    )
                return FetchOutcome(data, "ok", status, ctype, url)
        except asyncio.TimeoutError:
            return FetchOutcome(None, "请求超时", None, "", url)
        except aiohttp.ClientError as exc:
            return FetchOutcome(None, f"网络错误 {exc}", None, "", url)
        except Exception as exc:  # noqa: BLE001
            return FetchOutcome(None, f"请求异常 {exc}", None, "", url)

    @staticmethod
    def _accept_payload(outcome: FetchOutcome) -> FetchOutcome:
        """校验拿到的字节到底是不是图片。"""
        data = outcome.data or b""
        ctype = outcome.content_type or ""
        sniffed = sniff_image_type(data)

        if sniffed:
            complete, why = image_completeness(data, sniffed)
            if not complete:
                outcome.data = None
                outcome.reason = why
                return outcome
            return outcome
        if looks_like_markup(data):
            outcome.data = None
            outcome.reason = f"返回的是网页而不是图片（{ctype or '未知类型'}），可能是防盗链或 404 页面"
            return outcome
        if ctype == "image/svg+xml" or data[:200].lstrip().startswith(b"<svg"):
            outcome.data = None
            outcome.reason = "SVG 图片暂不支持发送"
            return outcome
        outcome.data = None
        outcome.reason = f"不是可识别的图片格式（Content-Type={ctype or '无'}）"
        return outcome

    async def fetch_image_outcome(self, image_url: str, base_url: str = "") -> FetchOutcome:
        """下载图片，返回带原因的结果对象。"""
        raw = (image_url or "").strip()

        # data: URI 直接解码，不需要联网
        match = _DATA_URI_RE.match(raw)
        if match:
            mime = (match.group("mime") or "image/png").lower()
            payload = match.group("data") or ""
            try:
                data = base64.b64decode(payload) if match.group("b64") else payload.encode("utf-8")
            except Exception as exc:  # noqa: BLE001
                return FetchOutcome(None, f"data URI 解码失败: {exc}", None, mime, raw[:120])
            outcome = FetchOutcome(data, "ok", None, mime, raw[:120])
            return self._accept_payload(outcome)

        url = normalize_image_url(raw, base_url)
        if not url:
            return FetchOutcome(None, "图片地址为空", None, "", "")
        if not url.lower().startswith(("http://", "https://")):
            return FetchOutcome(None, f"不支持的图片地址协议: {url[:60]}", None, "", url)

        # 依次尝试不同的 Referer 策略（微博等图床强制校验 Referer，不能省略）
        attempts: list[str] = []
        last: Optional[FetchOutcome] = None
        for label, referer in referer_candidates(url):
            outcome = await self._request_once(url, referer)
            if outcome.ok:
                accepted = self._accept_payload(outcome)
                if accepted.ok:
                    if attempts:
                        logger.info("rss: 图片改用「%s」后读取成功 %s", label, url)
                    return accepted
                outcome = accepted
            last = outcome
            attempts.append(f"{label}→{outcome.reason}")
            # 200 但内容不是图片（防盗链页面）时同样值得换 Referer 再试；
            # 只有明确的业务性错误（如 400）才提前结束。
            if (
                outcome.status is not None
                and outcome.status != 200
                and outcome.status not in _RETRYABLE_STATUS
            ):
                break
            if outcome.status is None:
                await self._reset_session()  # 连接层失败，重建会话再试

        if last is None:  # 理论上不会发生
            return FetchOutcome(None, "无可用请求方式", None, "", url)
        last.reason = "；".join(attempts)
        return last

    async def fetch_image(self, image_url: str, base_url: str = "") -> Optional[bytes]:
        """下载图片原始字节，失败返回 None（旧接口，保留兼容）。"""
        return (await self.fetch_image_outcome(image_url, base_url)).data

    # ------------------------------------------------------------------ Base64
    def _to_base64(self, raw: bytes) -> str:
        return base64.b64encode(raw).decode("utf-8")

    def _adjust_corner(self, raw: bytes, color=(255, 255, 255)) -> bytes:
        """随机改一个角的像素，返回 JPEG 字节；失败则退回原图。"""
        try:
            with Image.open(BytesIO(raw)) as img:
                img = img.convert("RGB")
                width, height = img.size
                if width < 2 or height < 2:
                    return raw
                pixels = img.load()
                corners = [(0, 0), (width - 1, 0), (0, height - 1), (width - 1, height - 1)]
                chosen = random.choice(corners)
                pixels[chosen[0], chosen[1]] = color
                output = BytesIO()
                img.save(output, format="JPEG", quality=90)
                return output.getvalue()
        except Exception as exc:  # noqa: BLE001
            logger.warning("rss: 防和谐处理失败，改为原图发送: %s", exc)
            return raw

    async def fetch_image_base64(self, image_url: str, base_url: str = "") -> tuple[Optional[str], str]:
        """下载并按需处理图片，返回 (base64, 失败原因)。"""
        outcome = await self.fetch_image_outcome(image_url, base_url)
        if not outcome.ok:
            return None, outcome.reason
        raw = outcome.data or b""
        if self.is_adjust_pic:
            raw = self._adjust_corner(raw)
        return self._to_base64(raw), "ok"

    async def modify_corner_pixel_to_base64(self, image_url, color=(255, 255, 255), base_url: str = "") -> Optional[str]:
        """兼容旧调用：返回 Base64 字符串，失败返回 None。"""
        base64str, reason = await self.fetch_image_base64(image_url, base_url)
        if not base64str:
            logger.warning("rss: 图片读取失败 %s: %s", image_url, reason)
        return base64str

    # ------------------------------------------------------------------ 诊断
    async def diagnose(self, image_url: str, base_url: str = "") -> dict:
        """给 /rss pic-test 用的详细诊断信息。"""
        outcome = await self.fetch_image_outcome(image_url, base_url)
        info = {
            "url": outcome.url or image_url,
            "ok": outcome.ok,
            "reason": outcome.reason,
            "status": outcome.status,
            "content_type": outcome.content_type,
            "size": len(outcome.data) if outcome.data else 0,
            "detected_type": sniff_image_type(outcome.data or b"") if outcome.ok else None,
            "complete": image_completeness(outcome.data or b"")[0] if outcome.ok else False,
        }
        if outcome.ok:
            try:
                with Image.open(BytesIO(outcome.data or b"")) as img:
                    info["image"] = {"format": img.format, "mode": img.mode, "size": f"{img.size[0]}x{img.size[1]}"}
            except Exception as exc:  # noqa: BLE001
                info["image_error"] = str(exc)
        return info

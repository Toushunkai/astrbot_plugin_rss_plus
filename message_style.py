"""推送文本样式与智能换行。

老版本的推送文案（「更新了喵❤」「内容：」「链接：」「---」以及换行规则）全部写死在
``main.py`` 里，想换个说法就得改代码。现在这些都搬到了插件配置的 ``message_style``
段，AstrBot 的插件配置面板（``_conf_schema.json``）和插件页面的「推送样式」页签
都能直接改，保存后立即生效。

模板变量（写在 ``template`` / ``template_hide_url`` 里）：

    {chan_title}  频道名
    {title}       标题；单独占一行时，如果没有标题（或关闭了「显示标题」）整行会消失
    {content}     正文（已经过智能换行与长度截断）
    {link}        原文链接
    {video}       视频帖的播放页链接；不是视频帖时为空
    {video_cover} 视频封面**图片**的插入位置；不是视频帖时整行消失。
                  不写这个变量时，封面会作为第一张图跟在文字后面（旧行为）
    {pub_date}    发布时间
    {feed_url}    订阅源地址

一行里用到的变量全是空的话（没有标题时的 ``{title}``、不是视频帖时的 ``{video}``），
这一行会整行消失——所以 ``视频：{video}`` 这种写法不会留下光秃秃的「视频：」，
也不会多出一个空行。不认识的 ``{xxx}`` 会原样保留，不会报错。

分隔线（默认 ``---``）没有单独的配置项，它只是模板里的普通文字，
想换成 ``━━━━`` 或干脆删掉，直接改模板即可。

老配置里如果还留着 v1.3.3 的 ``{separator}`` 变量，
会自动替换成分隔线文字，不会把花括号发出去。

换行规则（``smart_break`` 打开时生效）：

1. 句末标点（内置 ``。！？!?…``，见 :data:`SENTENCE_END`，不提供配置项）后换行，
   且不会在标点与内容之间留下多余空格；
2. 英文句号 / 叹号 / 问号 + 空格 + 大写字母视为句子边界，但 ``Mr.`` ``e.g.`` ``U.S.``
   之类的缩写不会误断；
3. 链接（``http(s)://`` 与 ``www.`` 开头）先被保护起来，绝不会从中间断开；
4. 话题标签：结尾处的一串 ``#xxx#`` / ``#xxx`` 各自单独成行，夹在句子中间、
   后面还有正文的标签保持原样；
5. 已有的换行、连续空行会被整理，行尾空格被清掉。

超过 ``max_content_length`` 时，会优先在句子结束处截断，其次是空白处，
并且不会把链接切成两半。
"""

from __future__ import annotations

import re
from typing import Any, Optional

__all__ = ["MessageStyle", "DEFAULT_STYLE", "STYLE_KEYS", "DEMO_SAMPLE", "COVER_MARKER"]

# 默认样式：与旧版本硬编码出来的排版完全一致
DEFAULT_STYLE: dict[str, Any] = {
    "template": (
        "{chan_title} 更新了喵❤\n"
        "---\n"
        "{title}\n"
        "内容：{content}\n"
        "视频：{video}\n"
        "\n"
        "链接：{link}\n"
        "---"
    ),
    "template_hide_url": (
        "{chan_title} 更新了喵❤\n"
        "---\n"
        "{title}\n"
        "内容：{content}\n"
        "---"
    ),
    "smart_break": True,
    "hashtag_own_line": True,
    "collapse_blank_lines": True,
    "max_content_length": 200,
    "pic_error": "图片链接读取失败",
    "pic_error_detail": "图片处理失败: {url}",
}

# 允许从插件页面 / 配置里修改的键（与 _conf_schema.json 保持一致）
STYLE_KEYS: tuple[str, ...] = tuple(DEFAULT_STYLE.keys())

# 模板里的 {video_cover} 会替换成这个标记；main.py 据此把视频封面**图片**
# 插到模板里那个位置（想放哪就放哪：正文前、正文后、链接前……）。
COVER_MARKER = "\x02"

# 句末标点：智能换行遇到它们就断行。属于内置规则，不提供配置项，
# 想调整断句行为只能从代码这里改（``sentence_end`` 配置项自 v1.3.5 起移除）。
SENTENCE_END = "。！？!?…"

# v1.3.3 里独立配置项 separator 的默认值；老配置迁移时用它兜底
LEGACY_SEPARATOR = "---"

# 「样式预览」用的示例数据
DEMO_SAMPLE: dict[str, str] = {
    "chan_title": "示例频道",
    "title": "换行终于聪明了一点的示例标题",
    "content": (
        "这是一段示例正文，用来演示智能换行！它会在句末标点后自动断行，"
        "而不会把链接拆断。This is an English sentence. It also breaks at "
        "the sentence boundary, without ruining abbreviations like Mr. Smith. "
        "还能让话题标签单独成行 #RSS# #AstrBot#"
    ),
    "link": "https://example.com/post/123",
    "video": "https://video.weibo.com/show?fid=1034%3A5345988664295458",
    "pub_date": "Wed, 01 Jan 2025 12:00:00 GMT",
    "feed_url": "https://example.com/feed.xml",
}

# ---------------------------------------------------------------- 正则与常量
_SOFT_BREAK = "\x01"  # 内部使用的「软换行」标记
_STASH_MARK = "\x00"  # 链接占位符使用的标记

_URL_RE = re.compile(r"(?:https?://|www\.)[^\s<>\"'）】」》〉」，。！？；]+", re.I)
# #话题# 与 #话题 两种写法
_HASHTAG_RE = re.compile(r"#[^#\s][^#\s]{0,60}?#|#[^\s#，。！？、,.;!?]+")
# 一「串」换行 / 软换行（连同两侧空格）
_BREAK_RUN_RE = re.compile(r"[ \t]*(?:[\x01\n])[ \t\x01\n]*")
# 英文句子边界：标点 + 空格 + 大写字母 / 数字 / 中日韩文字（或引号、括号开头）
_EN_SENT_RE = re.compile(
    r"(?<=[.!?])[ \t]+(?=[\"'“”‘’(（\[【]?[A-Z0-9\u3400-\u4dbf\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af])"
)
# 模板里 {xxx} 形式的变量
_PLACEHOLDER_RE = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")
# 常见的英文缩写，遇到它们不加换行
_ABBREVIATIONS = {
    "mr", "mrs", "ms", "dr", "prof", "sr", "jr", "st", "vs", "etc", "eg", "ie",
    "no", "fig", "al", "inc", "ltd", "co", "corp", "dept", "approx", "est",
    "min", "max", "vol", "pp", "ca", "cf", "ed", "eds", "trans", "univ",
    "gov", "sen", "rep", "gen", "col", "capt", "lt", "sgt", "phd", "esq",
}
# 话题标签前面出现这些字符时，也认为可以断行
_PUNCT_BEFORE_TAG = "，。！？；：、,.!?;:）)】」》〉\"'“”‘’…—"
# 只有空白与标点（用于判断「结尾一长串话题标签」）
_SEPARATOR_ONLY_RE = re.compile(rf"[\s{re.escape(_PUNCT_BEFORE_TAG)}]*")


def _as_bool(value: Any, default: bool) -> bool:
    """认识的写法就转成布尔，不认识的（例如 off / abc）回退到默认值。"""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        text = value.strip().lower()
        if text in ("1", "true", "yes", "on", "是", "开", "启用", "开启"):
            return True
        if text in ("0", "false", "no", "off", "否", "关", "关闭", ""):
            return False
    return default


def _as_text(value: Any, default: str, limit: int = 4000) -> str:
    if value is None:
        return default
    text = value if isinstance(value, str) else str(value)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    if not text.strip():
        return default
    return text[:limit]


class MessageStyle:
    """把一条 RSS 条目渲染成推送文案。"""

    def __init__(self, config: Optional[dict] = None) -> None:
        self.update_config(config)

    # ------------------------------------------------------------------ 配置
    def update_config(self, config: Optional[dict]) -> None:
        """从插件配置刷新样式。缺省 / 非法值一律回退到默认样式。"""
        cfg = config if isinstance(config, dict) else {}
        self.template = _as_text(cfg.get("template"), DEFAULT_STYLE["template"], 4000)
        self.template_hide_url = _as_text(
            cfg.get("template_hide_url"), DEFAULT_STYLE["template_hide_url"], 4000
        )
        # v1.3.3 的分隔线是独立配置项，现在直接写在模板里。老配置自动迁移：
        # 有 separator 就用它的值，否则用默认的 ---，绝不让 {separator} 被原样发出去。
        legacy = cfg.get("separator")
        legacy = legacy.strip()[:200] if isinstance(legacy, str) and legacy.strip() else LEGACY_SEPARATOR
        self.template = self.template.replace("{separator}", legacy)
        self.template_hide_url = self.template_hide_url.replace("{separator}", legacy)

        self.smart_break = _as_bool(cfg.get("smart_break"), DEFAULT_STYLE["smart_break"])
        self.hashtag_own_line = _as_bool(
            cfg.get("hashtag_own_line"), DEFAULT_STYLE["hashtag_own_line"]
        )
        self.collapse_blank_lines = _as_bool(
            cfg.get("collapse_blank_lines"), DEFAULT_STYLE["collapse_blank_lines"]
        )

        try:
            self.max_content_length = int(cfg.get("max_content_length", DEFAULT_STYLE["max_content_length"]))
        except (TypeError, ValueError):
            self.max_content_length = DEFAULT_STYLE["max_content_length"]
        self.max_content_length = max(-1, min(20000, self.max_content_length))

        self.pic_error = _as_text(cfg.get("pic_error"), DEFAULT_STYLE["pic_error"], 200)
        self.pic_error_detail = _as_text(
            cfg.get("pic_error_detail"), DEFAULT_STYLE["pic_error_detail"], 200
        )

    def to_dict(self) -> dict:
        """返回可以写回配置文件的当前样式。"""
        return {
            "template": self.template,
            "template_hide_url": self.template_hide_url,
            "smart_break": self.smart_break,
            "hashtag_own_line": self.hashtag_own_line,
            "collapse_blank_lines": self.collapse_blank_lines,
            "max_content_length": self.max_content_length,
            "pic_error": self.pic_error,
            "pic_error_detail": self.pic_error_detail,
        }

    @staticmethod
    def default_dict() -> dict:
        return dict(DEFAULT_STYLE)

    # ------------------------------------------------------------------ 渲染
    def render(
        self,
        *,
        chan_title: str = "",
        title: str = "",
        content: str = "",
        link: str = "",
        video: str = "",
        video_cover: bool = False,
        pub_date: str = "",
        feed_url: str = "",
        show_title: bool = True,
        hide_url: bool = False,
    ) -> str:
        """按模板渲染一条推送文案。"""
        template = self.template_hide_url if hide_url else self.template
        if not isinstance(template, str) or not template.strip():
            template = DEFAULT_STYLE["template_hide_url" if hide_url else "template"]

        title = (title or "").strip()
        has_title = bool(show_title and title)

        values = {
            "chan_title": chan_title or "",
            "title": title if has_title else "",
            "content": self.format_content(content),
            "link": link or "",
            "video": (video or "").strip(),
            # 有封面就放一个标记，main.py 会在标记处插入图片；没有就当空值（整行消失）
            "video_cover": COVER_MARKER if video_cover else "",
            "pub_date": pub_date or "",
            "feed_url": feed_url or "",
        }

        text = self.tidy_message(self._substitute(template, values))
        if not text:
            # 模板被写成空（例如整条模板只有 {title} 而没有标题）时兜底，
            # 免得真的推送一条空消息出去
            fallback = DEFAULT_STYLE["template_hide_url" if hide_url else "template"]
            text = self.tidy_message(self._substitute(fallback, values))
        return text

    @staticmethod
    def _substitute(template: str, values: dict) -> str:
        """替换模板变量。

        规则：**一行里用到的变量全是空的，这一行就整行不显示**。
        这样 ``{title}``（没标题时）、``视频：{video}``（不是视频帖时）这类
        可选行会连标签一起消失，不会留下空行或光秃秃的「视频：」。
        不认识的 ``{xxx}`` 视为有内容，原样保留。
        """
        out: list[str] = []
        for line in template.split("\n"):
            keys = _PLACEHOLDER_RE.findall(line)
            known = [k for k in keys if k in values]
            if known and all(not str(values[k]).strip() for k in known):
                continue  # 整行都是空变量 → 这行不要了

            def _replace(match: "re.Match") -> str:
                key = match.group(1)
                return str(values[key]) if key in values else match.group(0)

            out.append(_PLACEHOLDER_RE.sub(_replace, line))
        return "\n".join(out)

    def pic_error_text(self, url: str = "") -> str:
        """图片出问题时的提示文案。url 为空表示「读取失败」的通用提示。"""
        if not url:
            return self.pic_error
        return self.pic_error_detail.replace("{url}", url)

    # ------------------------------------------------------------------ 正文
    def format_content(self, text: str) -> str:
        """正文排版：清理 → 智能换行 → 截断 → 收尾。"""
        text = self._normalize(text)
        if not text:
            return ""
        if self.smart_break:
            text = self._smart_break(text)
        text = self._truncate(text)
        return self._tidy(text)

    def _normalize(self, text: str) -> str:
        text = text if isinstance(text, str) else str(text or "")
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        # 内部标记与零宽字符不允许出现在原文里
        text = text.replace(_SOFT_BREAK, "").replace(_STASH_MARK, "").replace(COVER_MARKER, "")
        text = text.replace("\u200b", "").replace("\ufeff", "")
        text = text.replace("\u00a0", " ")
        text = re.sub(r"[ \t]+\n", "\n", text)
        return text.strip()

    def _truncate(self, text: str) -> str:
        limit = self.max_content_length
        if limit <= 0 or len(text) <= limit:
            return text
        head = text[:limit]
        cut_in_url = False
        for match in _URL_RE.finditer(text):
            if match.start() < limit < match.end():
                head = text[: match.start()].rstrip()
                cut_in_url = True
                break
        if not cut_in_url:
            # 优先在句末标点处断开，其次退到最后一个空白处
            last = None
            for match in re.finditer(rf"[{re.escape(SENTENCE_END)}]", head):
                last = match
            if last is not None and last.end() >= limit * 0.6:
                head = head[: last.end()].rstrip()
            else:
                space = max(head.rfind(" "), head.rfind("\n"))
                if space >= limit * 0.6:
                    head = head[:space].rstrip()
        return head + "..."

    def _smart_break(self, text: str) -> str:
        if not text:
            return ""
        if self.collapse_blank_lines:
            text = re.sub(r"\n{3,}", "\n\n", text)

        # 1) 先保护链接，避免在 URL 内部插入换行
        stash: list[str] = []

        def _stash(match: "re.Match") -> str:
            stash.append(match.group(0))
            return f"{_STASH_MARK}{len(stash) - 1}{_STASH_MARK}"

        text = _URL_RE.sub(_stash, text)

        # 2) 句末标点后换行（允许一段标点 + 收尾引号，尾随空格被吃掉）
        cls = re.escape(SENTENCE_END)
        text = re.sub(
            rf"([{cls}]+[”’\"'』」）】\]]*)[ \t]*(?=\S)",
            lambda m: m.group(1) + _SOFT_BREAK,
            text,
        )

        # 3) 英文句子边界
        text = _EN_SENT_RE.sub(self._break_english, text)

        # 4) 话题标签单独成行
        if self.hashtag_own_line:
            text = self._break_hashtags(text)

        # 5) 合并软换行、清理空行与行尾空格
        text = _BREAK_RUN_RE.sub(self._merge_breaks, text)

        # 6) 还原链接
        text = re.sub(
            rf"{_STASH_MARK}(\d+){_STASH_MARK}",
            lambda m: stash[int(m.group(1))] if int(m.group(1)) < len(stash) else "",
            text,
        )
        return text

    def _break_english(self, match: "re.Match") -> str:
        """英文句子边界：缩写、单个字母缩写保持不断行。"""
        source = match.string
        punct = match.start() - 1
        start = punct - 1
        while start >= 0 and (source[start].isalpha() or source[start] == "."):
            start -= 1
        word = source[start + 1 : punct].rsplit(".", 1)[-1].strip().lower()
        if word and (word in _ABBREVIATIONS or (len(word) == 1 and word.isalpha())):
            return match.group(0)
        return _SOFT_BREAK

    def _break_hashtags(self, text: str) -> str:
        matches = list(_HASHTAG_RE.finditer(text))
        if not matches:
            return text

        positions: set[int] = set()

        # 结尾连着的一串话题标签（中间只有空白/标点）视为一个整体，各自单独成行
        block_start = len(matches)
        next_start: Optional[int] = None
        for index in range(len(matches) - 1, -1, -1):
            match = matches[index]
            tail = text[match.end() :] if next_start is None else text[match.end() : next_start]
            if not _SEPARATOR_ONLY_RE.fullmatch(tail):
                break
            block_start = index
            next_start = match.start()

        for index, match in enumerate(matches):
            start = match.start()
            prev = text[start - 1] if start > 0 else ""
            if index >= block_start or prev == "" or prev == "\n":
                # 结尾的标签串：每个标签单独一行；本来就在行首的保持原样
                positions.add(start)

        if not positions:
            return text
        parts: list[str] = []
        for index, char in enumerate(text):
            if index in positions:
                parts.append(_SOFT_BREAK)
            parts.append(char)
        return "".join(parts)

    def _merge_breaks(self, match: "re.Match") -> str:
        """把一串「软换行 + 已有换行 + 空格」整理成合适的换行。"""
        newlines = match.group(0).count("\n")
        if newlines == 0:
            return "\n"  # 纯软换行
        if self.collapse_blank_lines:
            return "\n" * min(2, newlines)
        return "\n" * newlines

    def _tidy(self, text: str) -> str:
        text = re.sub(r"[ \t]+\n", "\n", text)
        if self.collapse_blank_lines:
            text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()

    def tidy_message(self, text: str) -> str:
        """整条消息的收尾整理（不改变模板本身的段落结构）。"""
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        text = re.sub(r"[ \t]+\n", "\n", text)
        if self.collapse_blank_lines:
            text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()

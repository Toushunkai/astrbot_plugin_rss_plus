from dataclasses import dataclass, field


@dataclass
class RSSItem:
    """一条 RSS 条目。"""

    chan_title: str
    title: str
    link: str
    description: str
    pubDate: str
    pubDate_timestamp: int
    pic_urls: list[str] = field(default_factory=list)
    feed_url: str = ""  # 该条目所属的订阅源地址，用于查该订阅的独立配置
    video_url: str = ""  # 视频帖的播放页链接（没有视频时为空）
    video_covers: list[str] = field(default_factory=list)  # 视频帖的封面图（<video poster>）
    block_word: str = ""  # 原文命中的屏蔽词（解析时就查好，含被长度截断掉的部分）
    block_where: str = ""  # 命中位置：标题 / 正文

    def __str__(self) -> str:
        return f"{self.title} - {self.link} - {self.description} - {self.pubDate}"

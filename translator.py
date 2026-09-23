"""内置 AI 翻译服务。

设计目标：
- 完全复用 AstrBot 已配置的大语言模型（Provider），插件不再需要任何第三方
  API Key，也不再依赖 openai SDK；
- 全程异步，绝不会阻塞事件循环（旧实现用的是同步 requests，会卡住整个 Bot）；
- 带 LRU 缓存、超时保护、语言自动判断与失败兜底。

Provider 解析顺序：
1. 配置里显式指定的 provider_id（WebUI 插件页 / _conf_schema 的 select_provider）；
2. 当前会话（umo）绑定的大模型；
3. AstrBot 默认的大模型。
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections import OrderedDict
from typing import Any, Optional

__all__ = ["TranslatorService"]

_CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
_LATIN_RE = re.compile(r"[A-Za-z]")

_DEFAULT_SYSTEM_PROMPT = (
    "你是一个专业的翻译引擎。请把用户输入的内容翻译成{target_lang}，"
    "要求翻译自然、准确、符合目标语言的表达习惯。"
    "只输出译文本身，不要输出解释、不要添加任何前后缀、不要复述原文。"
    "保持原文的换行结构，链接、#话题、@用户名、数字与表情符号原样保留。"
)


class TranslatorService:
    """基于 AstrBot Provider 的翻译服务。"""

    def __init__(self, context: Any, config: Optional[dict] = None) -> None:
        self.context = context
        self.logger = logging.getLogger("astrbot")

        # ---- 运行期配置（由 update_config 注入） ----
        self.enabled: bool = True
        self.provider_id: str = ""
        self.target_lang: str = "中文"
        self.sources: list[str] = ["x.com", "twitter.com"]
        self.max_chars: int = 1500
        self.timeout: int = 40
        self.temperature: float = 0.2
        self.cache_size: int = 256
        self.keep_original_on_fail: bool = True
        self.translate_title: bool = False
        self.skip_if_translated: bool = True

        self._cache: "OrderedDict[str, str]" = OrderedDict()
        self._lock = asyncio.Lock()

        if config:
            self.update_config(config)

    # ------------------------------------------------------------------ 配置
    def update_config(self, config: dict) -> None:
        """从插件配置中刷新翻译相关设置。"""
        cfg = config or {}
        self.enabled = bool(cfg.get("enable", True))
        self.provider_id = str(cfg.get("provider_id") or "").strip()
        self.target_lang = str(cfg.get("target_lang") or "中文").strip() or "中文"
        sources = cfg.get("sources")
        if isinstance(sources, str):
            sources = [s.strip() for s in sources.split(",") if s.strip()]
        if isinstance(sources, list):
            self.sources = [str(s).strip().lower() for s in sources if str(s).strip()]
        try:
            self.max_chars = max(50, int(cfg.get("max_chars", 1500)))
            self.timeout = max(5, int(cfg.get("timeout", 40)))
            self.cache_size = max(0, int(cfg.get("cache_size", 256)))
            self.temperature = float(cfg.get("temperature", 0.2))
        except (TypeError, ValueError):
            self.max_chars, self.timeout, self.cache_size, self.temperature = 1500, 40, 256, 0.2
        self.keep_original_on_fail = bool(cfg.get("keep_original_on_fail", True))
        self.translate_title = bool(cfg.get("translate_title", False))
        self.skip_if_translated = bool(cfg.get("skip_if_translated", True))
        self._trim_cache()

    # ------------------------------------------------------------------ 工具
    def should_translate(self, link: str = "", text: str = "") -> bool:
        """是否需要对这条内容发起翻译。"""
        if not self.enabled:
            return False
        if self.sources:
            target = f"{link} {text[:200]}".lower()
            if not any(s in target for s in self.sources):
                return False
        return True

    def needs_translation(self, text: str) -> bool:
        """粗略判断文本是否已经是目标语言，避免浪费模型调用。"""
        if not text or not text.strip():
            return False
        if not self.skip_if_translated:
            return True
        cjk = len(_CJK_RE.findall(text))
        latin = len(_LATIN_RE.findall(text))
        if self.target_lang.lower() in ("中文", "chinese", "zh", "zh-cn", "简体中文"):
            # 中文占比已经很高就不翻译
            return not (cjk >= 10 and cjk >= latin * 0.6)
        return True

    def _trim_cache(self) -> None:
        if self.cache_size <= 0:
            self._cache.clear()
            return
        while len(self._cache) > self.cache_size:
            self._cache.popitem(last=False)

    def _cache_key(self, text: str) -> str:
        return f"{self.target_lang}\u0000{text}"

    # ------------------------------------------------------------------ 主流程
    async def translate(self, text: str, umo: Optional[str] = None) -> str:
        """把文本翻译为目标语言；失败时按配置返回原文。"""
        original = text or ""
        if not self.enabled or not original.strip():
            return original
        if not self.needs_translation(original):
            return original

        snippet = original[: self.max_chars]
        key = self._cache_key(snippet)
        cached = self._cache.get(key)
        if cached is not None:
            self._cache.move_to_end(key)
            return cached

        try:
            result = await asyncio.wait_for(
                self._generate(snippet, umo=umo), timeout=self.timeout
            )
        except asyncio.TimeoutError:
            self.logger.warning("rss 翻译超时（%ss），本次保留原文", self.timeout)
            result = None
        except Exception as exc:  # noqa: BLE001 - 翻译失败不应该影响推送
            self.logger.warning("rss 翻译失败: %s", exc)
            result = None

        result = (result or "").strip()
        if not result:
            return original if self.keep_original_on_fail else original

        if self.cache_size > 0:
            async with self._lock:
                self._cache[key] = result
                self._trim_cache()
        return result

    # ------------------------------------------------------------------ Provider
    async def _generate(self, text: str, umo: Optional[str] = None) -> Optional[str]:
        system_prompt = _DEFAULT_SYSTEM_PROMPT.format(target_lang=self.target_lang)
        last_error: Optional[Exception] = None

        # 1) 配置中显式指定的 Provider
        provider_id = self.provider_id or await self._session_provider_id(umo)
        if provider_id:
            try:
                resp = await self._llm_generate(provider_id, text, system_prompt)
                if resp:
                    return resp
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                self.logger.warning("rss 翻译：Provider %s 调用失败: %s", provider_id, exc)

        # 2) 回退到 AstrBot 默认 Provider
        try:
            provider = await self._default_provider(umo)
            if provider is not None:
                resp = await self._provider_text_chat(provider, text, system_prompt)
                if resp:
                    return resp
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            self.logger.warning("rss 翻译：默认模型调用失败: %s", exc)

        if last_error:
            self.logger.warning("rss 翻译：所有模型均不可用（%s）", last_error)
        return None

    async def _llm_generate(self, provider_id: str, text: str, system_prompt: str) -> Optional[str]:
        kwargs: dict[str, Any] = {}
        if self.temperature is not None:
            kwargs["temperature"] = self.temperature
        try:
            resp = await self.context.llm_generate(
                chat_provider_id=provider_id,
                prompt=text,
                system_prompt=system_prompt,
                **kwargs,
            )
        except TypeError:
            # 某些 Provider 不接受 temperature 之类的透传参数
            resp = await self.context.llm_generate(
                chat_provider_id=provider_id,
                prompt=text,
                system_prompt=system_prompt,
            )
        return getattr(resp, "completion_text", None)

    async def _provider_text_chat(self, provider: Any, text: str, system_prompt: str) -> Optional[str]:
        try:
            resp = await provider.text_chat(
                prompt=text, system_prompt=system_prompt, temperature=self.temperature
            )
        except TypeError:
            resp = await provider.text_chat(prompt=text, system_prompt=system_prompt)
        return getattr(resp, "completion_text", None)

    async def _session_provider_id(self, umo: Optional[str]) -> Optional[str]:
        if not umo:
            return None
        try:
            return await self.context.get_current_chat_provider_id(umo=umo)
        except Exception:  # noqa: BLE001 - 未配置会话模型时属于正常情况
            return None

    async def _default_provider(self, umo: Optional[str]) -> Any:
        getter = getattr(self.context, "get_using_provider_async", None)
        if callable(getter):
            return await getter(umo)
        return self.context.get_using_provider(umo)

    # ------------------------------------------------------------------ 调试
    async def describe_provider(self, umo: Optional[str] = None) -> dict:
        """给插件页面用：报告当前会使用哪个模型。"""
        info: dict[str, Any] = {
            "configured_provider_id": self.provider_id,
            "session_provider_id": None,
            "default_provider": None,
        }
        info["session_provider_id"] = await self._session_provider_id(umo)
        try:
            provider = await self._default_provider(umo)
            if provider is not None:
                meta = provider.meta()
                info["default_provider"] = {
                    "id": meta.id,
                    "model": meta.model,
                    "type": meta.type,
                }
        except Exception as exc:  # noqa: BLE001
            info["default_provider_error"] = str(exc)
        return info

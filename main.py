"""AnimeTrace 二次元角色识别插件

用法：引用一张图片并回复 `anime`（无需唤醒词），即可识别图中的动漫 / Galgame 角色。

设计要点：
- 用 event_message_type(ALL) 而非 filter.command，因此不需要 @ 或唤醒前缀；
  AstrBot 的唤醒检查阶段在任意 handler 的 filter 通过时都会置 is_wake=True。
- 不匹配触发词时直接 return，不 yield 任何结果，因此不会产生任何回复噪音。
- 模型名不写死：优先读配置，否则实时向 /v1/model/list 取当前 enabled 的默认模型。
- 兼容 HTTP/HTTPS 图片、file:// 与本地路径图片。
"""

from __future__ import annotations

import asyncio
import os
import time
from collections import OrderedDict
from typing import Any

import aiohttp

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.event.filter import EventMessageType
from astrbot.api.message_components import Image, Reply
from astrbot.api.star import Context, Star, register

API_SEARCH = "https://api.animetrace.com/v1/search"
API_MODELS = "https://api.animetrace.com/v1/model/list"

PLUGIN_NAME = "astrbot_plugin_animetrace"

# 业务状态码 -> 给用户看的友好提示
CODE_HINTS: dict[int, str] = {
    17701: "图片太大了，换一张小一点的试试～",
    17702: "服务器正忙，等一会儿再试吧",
    17703: "请求参数不对，这是插件的问题，塞拉去修",
    17704: "接口正在维护中，晚点再来",
    17705: "这个图片格式不支持呢",
    17706: "这次没识别出来，再试一次看看",
    17707: "接口内部出错了",
    17708: "图里人物太多啦，裁剪一下再发过来吧",
    17722: "图片下载失败了，重新发一次吧",
    17728: "已达到本次使用上限，歇一会儿再试",
    17731: "现在用的人有点多，稍等一下再试",
    404: "接口地址失效了，塞拉去更新",
    -1: "接口出错了，换个清晰点的图试试",
}

MODEL_CACHE_TTL = 600  # 模型列表缓存 10 分钟


@register(PLUGIN_NAME, "Seira", "二次元角色识别（AnimeTrace）：引用图片回复 anime 即可", "1.0.0")
class AnimeTracePlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.cfg = config
        self._session: aiohttp.ClientSession | None = None
        self._model_cache: tuple[float, str] | None = None
        self._cooldown: OrderedDict[str, float] = OrderedDict()

    async def terminate(self):
        """插件卸载时释放连接池"""
        if self._session and not self._session.closed:
            await self._session.close()
        self._session = None

    # ------------------------------------------------------------------ 配置

    def _cfg(self, key: str, default: Any) -> Any:
        try:
            value = self.cfg.get(key, default)
        except Exception:
            return default
        return default if value is None else value

    def _triggers(self) -> set[str]:
        raw = self._cfg("trigger_words", ["anime"])
        if isinstance(raw, str):
            raw = [w for w in raw.replace("，", ",").split(",")]
        words = {str(w).strip().lower() for w in raw if str(w).strip()}
        return words or {"anime"}

    def _cooldown_ok(self, event: AstrMessageEvent) -> bool:
        """同一会话+同一用户的冷却，防止刷接口"""
        seconds = float(self._cfg("cooldown_seconds", 5) or 0)
        if seconds <= 0:
            return True
        try:
            key = f"{event.get_session_id()}:{event.get_sender_id()}"
        except Exception:
            return True
        now = time.time()
        last = self._cooldown.get(key)
        if last is not None and now - last < seconds:
            return False
        self._cooldown[key] = now
        self._cooldown.move_to_end(key)
        while len(self._cooldown) > 512:
            self._cooldown.popitem(last=False)
        return True

    # ------------------------------------------------------------ 图片获取

    @staticmethod
    def _usable(img: Image | None) -> Image | None:
        if img is None:
            return None
        url = (img.url or "").strip()
        file = (img.file or "").strip()
        if url or file:
            return img
        return None

    @staticmethod
    def _find_reply(event: AstrMessageEvent) -> Reply | None:
        for comp in event.get_messages():
            if isinstance(comp, Reply):
                return comp
        return None

    async def _image_from_reply(
        self, event: AstrMessageEvent, reply: Reply
    ) -> Image | None:
        """从被引用的消息里取出第一张图片"""
        direct = self._usable(
            next(
                (c for c in (getattr(reply, "chain", None) or []) if isinstance(c, Image)),
                None,
            )
        )
        if direct is not None:
            return direct

        # 引用链里没有图片（多数平台只给 reply id），走 Bot API 取原消息
        bot = getattr(event, "bot", None)
        reply_id = getattr(reply, "id", None)
        if bot is None or reply_id is None:
            return None
        try:
            resp = await asyncio.wait_for(
                bot.call_action("get_msg", message_id=int(reply_id)), timeout=15
            )
        except Exception as e:
            logger.debug(f"[AnimeTrace] get_msg 取原消息失败: {e}")
            return None

        if not isinstance(resp, dict):
            return None
        segments = resp.get("message")
        if isinstance(segments, str):
            return None
        for seg in segments if isinstance(segments, list) else []:
            if not isinstance(seg, dict) or seg.get("type") != "image":
                continue
            data = seg.get("data")
            if not isinstance(data, dict):
                continue
            url = data.get("url") if isinstance(data.get("url"), str) else None
            file = data.get("file") if isinstance(data.get("file"), str) else None
            found = self._usable(Image(file=file, url=url))
            if found is not None:
                return found
        return None

    async def _session_get(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(
                total=float(self._cfg("request_timeout", 40) or 40)
            )
            self._session = aiohttp.ClientSession(timeout=timeout)
        return self._session

    async def _read_image(self, img: Image) -> tuple[bytes, str] | None:
        """把图片组件读成 (字节, 文件名)。支持 http(s)、base64、file:// 和本地路径。"""
        raw = (img.url or "").strip()
        if not raw:
            raw = (img.file or "").strip()

        if raw.startswith(("http://", "https://")):
            session = await self._session_get()
            headers = {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
                ),
                "Referer": "https://qq.com/",
            }
            try:
                async with session.get(raw, headers=headers) as resp:
                    if resp.status != 200:
                        logger.warning(f"[AnimeTrace] 下载图片失败 HTTP {resp.status}")
                        return None
                    data = await resp.read()
            except Exception as e:
                logger.warning(f"[AnimeTrace] 下载图片异常: {e}")
                return None
            return data, self._filename(raw)

        if raw.startswith("base64://"):
            import base64

            try:
                return base64.b64decode(raw[len("base64://") :]), "image.png"
            except Exception as e:
                logger.warning(f"[AnimeTrace] base64 解码失败: {e}")
                return None

        path = raw
        if path.startswith("file:///"):
            path = path[len("file:///") :].replace("/", os.sep)
        elif path.startswith("file://"):
            path = path[len("file://") :]
        if os.path.isfile(path):
            try:
                with open(path, "rb") as f:
                    return f.read(), os.path.basename(path)
            except OSError as e:
                logger.warning(f"[AnimeTrace] 读取本地图片失败: {e}")
                return None
        return None

    @staticmethod
    def _filename(source: str) -> str:
        name = source.split("?")[0].rsplit("/", 1)[-1]
        if not name or "." not in name:
            name = "image.jpg"
        return name

    # -------------------------------------------------------------- 接口调用

    async def _resolve_model(self) -> str | None:
        """取当前可用的模型 id；配置里指定了就直接用，且不写死默认名"""
        configured = str(self._cfg("model", "") or "").strip()
        if configured:
            return configured

        now = time.time()
        if self._model_cache and now - self._model_cache[0] < MODEL_CACHE_TTL:
            return self._model_cache[1]

        session = await self._session_get()
        try:
            async with session.get(API_MODELS) as resp:
                payload = await resp.json(content_type=None)
        except Exception as e:
            logger.warning(f"[AnimeTrace] 获取模型列表失败: {e}")
            return None

        models = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(models, list):
            return None

        picked = None
        for m in models:
            if isinstance(m, dict) and m.get("enabled") and m.get("default"):
                picked = m.get("id")
                break
        if picked is None:
            for m in models:
                if isinstance(m, dict) and m.get("enabled"):
                    picked = m.get("id")
                    break
        if isinstance(picked, str) and picked:
            self._model_cache = (now, picked)
            return picked
        return None

    async def _search(
        self, model: str, data: bytes, filename: str
    ) -> dict[str, Any] | None:
        session = await self._session_get()
        form = aiohttp.FormData()
        form.add_field("model", model)
        form.add_field(
            "is_multi", "1" if bool(self._cfg("is_multi", True)) else "0"
        )
        form.add_field(
            "ai_detect", "1" if bool(self._cfg("ai_detect", True)) else "0"
        )
        form.add_field(
            "file", data, filename=filename, content_type="application/octet-stream"
        )
        try:
            async with session.post(API_SEARCH, data=form) as resp:
                return await resp.json(content_type=None)
        except Exception as e:
            logger.warning(f"[AnimeTrace] 调用识别接口失败: {e}")
            return None

    # ---------------------------------------------------------------- 结果渲染

    def _format(self, payload: dict[str, Any]) -> str:
        boxes = payload.get("data")
        if not isinstance(boxes, list) or not boxes:
            return "没能认出图里的角色…换张更清晰、人物更居中的图试试？"

        per_box = max(1, int(self._cfg("max_candidates", 3) or 3))
        max_boxes = max(1, int(self._cfg("max_faces", 3) or 3))

        lines: list[str] = []
        for idx, box in enumerate(boxes[:max_boxes], 1):
            if not isinstance(box, dict):
                continue
            chars = [c for c in (box.get("character") or []) if isinstance(c, dict)]
            if not chars:
                continue
            best = chars[0]
            work = str(best.get("work") or "?")
            name = str(best.get("character") or "?")
            line = f"{idx}. {work} · {name}" if len(boxes) > 1 else f"{work} · {name}"
            extra = chars[1:per_box]
            if extra:
                others = " ／ ".join(
                    f"{c.get('work') or '?'} · {c.get('character') or '?'}"
                    for c in extra
                )
                line += f"\n    其他可能：{others}"
            if box.get("not_confident"):
                line += "（置信度偏低）"
            lines.append(line)

        if not lines:
            return "没能认出图里的角色…换张更清晰、人物更居中的图试试？"

        if payload.get("ai") is True:
            lines.append("提示：这张图疑似 AI 生成")
        return "\n".join(lines)

    # ----------------------------------------------------------------- 主流程

    @filter.event_message_type(EventMessageType.ALL)
    async def on_message(self, event: AstrMessageEvent):
        text = (event.message_str or "").strip().lower()
        if text not in self._triggers():
            return  # 不是触发词，静默放行，不打扰其他插件

        if not self._cooldown_ok(event):
            yield event.plain_result("稍微等一下下再试哦～")
            return

        # 1) 优先用被引用的那张图
        image: Image | None = None
        reply = self._find_reply(event)
        if reply is not None:
            image = await self._image_from_reply(event, reply)
        # 2) 退而求其次：本条消息自带的图
        if image is None:
            image = self._usable(
                next(
                    (c for c in event.get_messages() if isinstance(c, Image)),
                    None,
                )
            )
        if image is None:
            yield event.plain_result(
                "引用一张图片再回复 anime，或者把图片和 anime 一起发过来～"
            )
            return

        read = await self._read_image(image)
        if read is None:
            yield event.plain_result("这张图取不到呢，重新发一次试试？")
            return
        data, filename = read

        model = await self._resolve_model()
        if model is None:
            yield event.plain_result("识别服务暂时连不上，稍后再试吧")
            return

        payload = await self._search(model, data, filename)
        if payload is None:
            yield event.plain_result("识别失败，稍后再试一次吧")
            return

        code = payload.get("code")
        if code not in (0, 17720, 200):
            hint = CODE_HINTS.get(code, f"识别失败（错误码 {code}）")
            yield event.plain_result(hint)
            return

        yield event.plain_result(self._format(payload))

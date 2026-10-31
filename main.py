"""astrbot_plugin_idle_mind —— 杂念系统

别的插件让它"说句话"，这个插件让它"想点事"。差别在于：它会有想说不说的时候。

核心机制：念头 → 自评想说程度 → 加噪声 → 分层落地（只想 / 小动作 / 说出 / 忍住）。
大部分轮次的结果是什么都没做，这恰恰是它像人的地方；说不出口的念头沉进心事单，
按 7 天半衰期慢慢变淡，凌晨做一次清算。

独立安装：本插件不依赖任何其他插件，装好填两个会话 ID 就能跑。
"""

from __future__ import annotations

import asyncio
from datetime import datetime

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star

from .core import Core, clip, to_float
from .mind import Mind
from .worries import Worries

PLUGIN_NAME = "astrbot_plugin_idle_mind"


class IdleMind(Star):
    def __init__(self, context: Context, config: AstrBotConfig | None = None):
        super().__init__(context)
        self.config = config or {}

        self.core = Core(context, self.config)
        self.worries = Worries(self.core)
        self.mind = Mind(self.core, self.worries)

        self._task: asyncio.Task | None = None
        self._start_loop()

        logger.info(
            f"[idle_mind] 已就绪：心里挂着 {len(self.worries.items)} 件事，心情 {self.core.mood.word()}"
        )
        self._warn_if_unconfigured()

    def _warn_if_unconfigured(self) -> None:
        """没配目标时给一句明确提示，否则用户会以为是插件坏了。"""
        if not self.core.cfg_list("private_targets", []) and not self.core.cfg_list("group_targets", []):
            logger.warning(
                "[idle_mind] 还没配置会话目标，插件不会主动发消息。"
                "请在插件配置里填 private_targets 或 group_targets（格式：平台:会话类型:ID）"
            )

    # ══════════════════════════════════════════════════════
    # 后台循环
    # ══════════════════════════════════════════════════════

    def _start_loop(self) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            logger.debug("[idle_mind] 当前无事件循环，跳过自动循环")
            return
        self._task = loop.create_task(self._loop())

    async def _loop(self) -> None:
        """主循环：睡一会儿 → 检查条件 → 想一次。

        为什么所有异常都在这里就地吞掉：定时任务里抛出去的异常不会有人接，
        结果是功能静默停止，而日志里只留一行 traceback 之后再也没有动静。
        """
        await asyncio.sleep(60)  # 启动后先缓一分钟，别和别的插件抢初始化
        while True:
            try:
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error(f"[idle_mind] 循环出错: {exc}")
                await asyncio.sleep(600)
                continue
            await asyncio.sleep(self.core.random_gap("min_gap_minutes", "max_gap_minutes", 20, 60))

    async def _tick(self) -> None:
        if not self.core.cfg("enabled", True):
            return
        if self.core.in_quiet_hours(str(self.core.cfg("quiet_hours", "1-7"))):
            return

        # 深夜整理先做，它不消耗模型调用
        self.mind.maybe_sleep()

        if not self._within_daily_limit():
            return
        if not (self.core.cfg_list("private_targets", []) or self.core.cfg_list("group_targets", [])):
            return

        self.worries.decay()
        result = await self.mind.cycle()

        if result.get("ok"):
            self._bump_daily()
            logger.info(
                f"[idle_mind] {result.get('kind')} → {clip(result.get('text') or result.get('kind'), 40)}"
            )
        elif result.get("held"):
            logger.info(f"[idle_mind] 忍住没说：{clip(result['held'], 30)}")
        else:
            logger.debug(f"[idle_mind] {result.get('reason')}")

    def _within_daily_limit(self) -> bool:
        """每日次数上限，防止模型抽风时把 API 额度烧光。"""
        limit = int(to_float(self.core.cfg("daily_limit", 20), 20))
        if limit <= 0:
            return True
        today = f"{datetime.now():%Y-%m-%d}"
        if self.core.state.get("count_day") != today:
            return True
        return int(to_float(self.core.state.get("count_today"), 0)) < limit

    def _bump_daily(self) -> None:
        today = f"{datetime.now():%Y-%m-%d}"
        if self.core.state.get("count_day") != today:
            self.core.state.update({"count_day": today, "count_today": 0})
        self.core.state["count_today"] = int(to_float(self.core.state.get("count_today"), 0)) + 1
        self.core.save_state()

    # ══════════════════════════════════════════════════════
    # 钩子
    # ══════════════════════════════════════════════════════

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_message(self, event: AstrMessageEvent):
        """旁听每条消息：记进缓冲、捕捉情绪、识别被纠正。

        刻意不 yield、不 stop_event —— 这个 handler 只旁听，
        不能影响群里其他插件对"要不要回话"的判断。
        """
        try:
            text = event.message_str or ""
            if not text:
                return
            self.core.store.record(
                session=str(getattr(event, "unified_msg_origin", "") or ""),
                sender=str(getattr(event, "sender_name", "") or ""),
                text=text,
            )
            self.mind.observe(text)
        except Exception as exc:
            logger.debug(f"[idle_mind] 旁听失败: {exc}")

    @filter.on_llm_request()
    async def inject_mood(self, event: AstrMessageEvent, req=None):
        """把此刻的心情状态注入提示词，让正常对话也带上情绪底色。

        只在 mood_inject 开启时做。其它插件如果也注入，内容会并列而不是互相覆盖。
        """
        if not self.core.cfg("mood_inject", True):
            return
        try:
            if getattr(req, "prompt", None) is not None:
                req.prompt = self.core.mood.hint() + "\n" + (req.prompt or "")
        except Exception as exc:
            logger.debug(f"[idle_mind] 注入心情失败: {exc}")

    # ══════════════════════════════════════════════════════
    # 命令
    # ══════════════════════════════════════════════════════

    @filter.command("mind")
    async def cmd_mind(self, event: AstrMessageEvent):
        """查看杂念系统状态。用法：/mind | /mind now | /mind clear"""
        args = (event.message_str or "").strip().split(maxsplit=1)
        sub = args[1].strip().lower() if len(args) > 1 else ""

        if sub in ("now", "think", "想"):
            result = await self.mind.cycle()
            yield event.plain_result(self._describe(result))
            return

        if sub in ("clear", "清空"):
            if not event.is_admin():
                yield event.plain_result("只有管理员能清空心事。")
                return
            n = self.worries.clear()
            yield event.plain_result(f"清掉了 {n} 件事。")
            return

        if sub:
            yield event.plain_result("用法：/mind 看状态，/mind now 立刻想一次，/mind clear 清空心事")
            return

        yield event.plain_result(self._status_text())

    def _status_text(self) -> str:
        st = self.core.state
        stats = self.core.store.stats()
        lib, agg = self.core.mood.drives()
        lines = [
            f"心情：{self.core.mood.word()}（想亲近 {lib:.2f} / 想怼人 {agg:.2f}）",
            f"心里挂着：{len(self.worries.items)} 件事",
        ]
        for w in self.worries.top(5):
            lines.append(f"  · {clip(w['text'], 30)}（{to_float(w.get('tension'), 0):.2f}）")
        lines.append(
            f"累计：说出口 {int(to_float(st.get('spoken'), 0))} 次，"
            f"忍住 {int(to_float(st.get('held'), 0))} 次，"
            f"连续安静 {int(to_float(st.get('silent_streak'), 0))} 轮"
        )
        lines.append(f"数据：{stats['msgs']} 条消息，{stats['notes']} 条记录")
        if st.get("last_thought"):
            lines.append(f"上个念头：{clip(st['last_thought'], 50)}")
        if st.get("last_sleep"):
            lines.append(f"上次整理：{st['last_sleep']}")
        return "\n".join(lines)

    def _describe(self, result: dict) -> str:
        if result.get("ok"):
            return f"想了，而且做了：{result.get('kind')} — {clip(result.get('text') or '', 80)}"
        if result.get("held"):
            return f"想说什么，但忍住了，记进心事单：{clip(result['held'], 60)}"
        return f"这一轮什么都没做（{result.get('reason')}）"

    @filter.command("worry")
    async def cmd_worry(self, event: AstrMessageEvent):
        """往心里塞一件事。用法：/worry 明天要交的东西还没写"""
        args = (event.message_str or "").strip().split(maxsplit=1)
        text = args[1].strip() if len(args) > 1 else ""
        if not text:
            yield event.plain_result("要说清楚挂的是什么。用法：/worry 内容")
            return
        self.worries.add(text, tension=0.7, kind="manual")
        yield event.plain_result(f"记下了，现在心里挂着 {len(self.worries.items)} 件事。")

    # ══════════════════════════════════════════════════════
    # 给模型用的工具
    # ══════════════════════════════════════════════════════

    @filter.llm_tool(name="mind_note")
    async def tool_mind_note(self, event: AstrMessageEvent, content: str = "", tension: float = 0.6):
        """在心里记下一件事，之后会反复想起它。

        Args:
            content(string): 要记住的事，一句话
            tension(number): 这件事有多重要，0 到 1
        """
        if not content:
            return "没说要记什么。"
        self.worries.add(content, tension=max(0.1, min(1.0, to_float(tension, 0.6))), kind="manual")
        return f"记下了：{clip(content, 40)}，现在心里挂着 {len(self.worries.items)} 件事。"

    @filter.llm_tool(name="mind_recall")
    async def tool_mind_recall(self, event: AstrMessageEvent):
        """看看自己心里现在挂着什么事。"""
        if not self.worries.items:
            return "心里空着，没什么挂着的。"
        return self.worries.prompt_block(8)

    async def terminate(self):
        if self._task and not self._task.done():
            self._task.cancel()
        try:
            self.core.save_state()
            self.worries.save()
        except Exception:
            pass
        logger.info("[idle_mind] 已停止")

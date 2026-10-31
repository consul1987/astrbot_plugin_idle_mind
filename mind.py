"""念头周期 —— 让它像人一样"想"，而不是"每次都想干点什么"。

这是本插件唯一的核心，也是它跟其他"定时说句话"插件的根本区别。

差别在哪：
  定时说句话的插件 = 到点了 → 生成一句 → 发出去。每轮必然产生一句话。
  这里 = 到点了 → 冒一个念头 → **自己判断有多想说** → 加一点噪声 → 分层落地。
        大部分轮次的结果是「什么都没做」，而这正是它像人的原因。

分层规则：
  urge < 0.55            只想不说（记一笔就完）
  0.55 ≤ urge < 0.80     小动作（戳一下、点个赞、发张图、写进本子）
  urge ≥ 0.80            说出来
  社交成本 ≥ 0.6         说不出口 → 沉进心事单（这是"忍住"）

为什么要加噪声：模型对自己"有多想说"的估计过于整齐，0.6 永远是 0.6。
加 ±0.12 的扰动之后，同一件事在不同时刻会落到不同层，行为才有毛边。
"""

from __future__ import annotations

import random
import time
from datetime import datetime
from pathlib import Path

from astrbot.api import logger
from astrbot.core.message.components import Image, Plain
from astrbot.core.message.message_event_result import MessageChain

from .core import clamp, clip, parse_json_block, to_float

THRESHOLD_HOLD = 0.55  # 低于这个值，只想不说
THRESHOLD_ACT = 0.80  # 高于这个值才开口说话
NOISE = 0.12  # 自评噪声幅度
COST_BLOCK = 0.6  # 社交成本高到这个值就忍住

PROMPT = """现在是 {time}。你此刻的心情：{mood_word}（想亲近 {lib:.2f} / 想怼人 {agg:.2f}）

你心里挂着的事：
{worries}

今天你自己干过：{activity}

刚才的对话：
{recent}

{extra}{scene}

你一个人待着，脑子里冒出点东西。有没有什么想说的、想做的？

用 JSON 回答，字段：
  thought  此刻冒出的念头（一两句，可以很琐碎、很没意义）
  urge     你有多想说 0~1 的小数（0.2 只是想一想，0.9 是非说不可）
  verb     想干什么：say / note / poke / like / meme / nothing
  outlet   verb=say 时从哪个口出：private / group / journal
  text     verb=say 或 note 时要说的话（尽量短，像随手发的）

没什么想法就 {{"thought":"","urge":0.1,"verb":"nothing"}}
只输出 JSON，不要解释。"""


class Mind:
    """念头周期。每个周期最多产生一次动作。"""

    def __init__(self, core, worries):
        self.core = core
        self.worries = worries
        self.state = core.state

    # ── 素材 ──
    def last_thought(self) -> str:
        return str(self.state.get("last_thought") or "")

    def scene(self) -> tuple[str, float]:
        """返回 (场景提示, 社交成本 0~1)。

        社交成本是"现在开口合不合适"的量化：
        半夜开口、刚说过话、上一条还没得到回应，都会让它更倾向于忍住。
        """
        hour = datetime.now().hour
        since = (time.time() - to_float(self.state.get("last_speak"), 0)) / 60
        cost = 0.0
        hints: list[str] = []
        if 1 <= hour < 7:
            cost += 0.45
            hints.append("现在是半夜")
        if since < 10:
            cost += 0.45
            hints.append("你刚刚才说过话")
        elif since < 30:
            cost += 0.2
            hints.append("你不久前说过话")
        if not self.state.get("last_reply_to_me"):
            cost += 0.15
            hints.append("你上一条他还没回")
        if int(to_float(self.state.get("silent_streak"), 0)) >= 3:
            cost -= 0.15
            hints.append("你已经安静了好几轮")
        text = ("（" + "，".join(hints) + "）") if hints else ""
        return text, clamp(cost)

    def _extra_context(self) -> str:
        """可选的外部上下文文件（给装了长期记忆插件的用户用）。

        只读一个纯文本文件，不解析任何结构化格式 —— 对方换存储方式也不影响这里。
        没配置就返回空串，提示词里不会出现这一段。
        """
        path = str(self.core.cfg("context_file", "")).strip()
        if not path:
            return ""
        try:
            with open(path, encoding="utf-8") as fh:
                body = " ".join(fh.read().split())
            if body:
                return f"你记得的一些事：\n{body[:600]}\n\n"
        except Exception:
            pass
        return ""

    def build_prompt(self) -> str:
        lib, agg = self.core.mood.drives()
        scene_hint, _ = self.scene()
        activity = "；".join(self.core.store.today_notes(3)) or "还没干嘛"
        return PROMPT.format(
            time=datetime.now().strftime("%H:%M"),
            mood_word=self.core.mood.word(),
            lib=lib,
            agg=agg,
            worries=self.worries.prompt_block(),
            activity=activity,
            recent=self.core.store.recent(6),
            extra=self._extra_context(),
            scene=scene_hint,
        )

    # ── 一个周期 ──
    async def cycle(self) -> dict:
        """想一次。返回结果字典，永远不抛异常。"""
        provider = self.core.context.get_using_provider()
        if provider is None:
            return {"ok": False, "reason": "没有可用模型"}

        try:
            resp = await provider.text_chat(
                prompt=self.build_prompt(),
                system_prompt=self.core.persona(),
            )
            raw = (resp.completion_text or "").strip()
        except Exception as exc:
            return {"ok": False, "reason": f"模型调用失败 {exc}"}

        plan = parse_json_block(raw)
        if plan is None:
            self._silent()
            return {"ok": False, "reason": "没想出个形状来"}

        thought = clip(plan.get("thought"), 100)
        verb = str(plan.get("verb") or "nothing").strip().lower()
        outlet = str(plan.get("outlet") or "private").strip().lower()
        text = clip(plan.get("text"), 200)
        urge = clamp(to_float(plan.get("urge"), 0.1) + random.uniform(-NOISE, NOISE))

        self.state["last_thought"] = thought

        # ① 什么都没想
        if verb == "nothing" or urge < THRESHOLD_HOLD:
            self._silent()
            if thought:
                self.core.store.note("think", thought[:60])
            return {"ok": False, "reason": "只想了想", "thought": thought, "urge": round(urge, 2)}

        _, cost = self.scene()

        # ② 想，但说不出口 —— 沉进心事单
        if cost >= COST_BLOCK and outlet in ("private", "group"):
            held = text or thought
            self.worries.add(held, tension=urge, kind="hold")
            self._silent()
            self.state["held"] = int(to_float(self.state.get("held"), 0)) + 1
            self.core.save_state()
            self.core.store.note("hold", f"忍住没说：{clip(held, 50)}")
            return {"ok": False, "reason": "忍住了", "held": held}

        # ③ 小动作
        if urge < THRESHOLD_ACT:
            out = await self._small_action(verb, text, thought)
            self.state["silent_streak"] = 0 if out.get("ok") else self._streak() + 1
            self.core.save_state()
            return out

        # ④ 说出来
        out = await self._speak(verb, outlet, text, thought)
        if out.get("ok"):
            self.state.update(
                {
                    "last_speak": time.time(),
                    "last_outlet": outlet,
                    "last_reply_to_me": 0,
                    "silent_streak": 0,
                    "spoken": int(to_float(self.state.get("spoken"), 0)) + 1,
                }
            )
        else:
            self.state["silent_streak"] = self._streak() + 1
        self.core.save_state()
        return out

    def _streak(self) -> int:
        return int(to_float(self.state.get("silent_streak"), 0))

    def _silent(self) -> None:
        self.state["silent_streak"] = self._streak() + 1
        self.core.save_state()

    # ── 小动作 ──
    async def _small_action(self, verb: str, text: str, thought: str) -> dict:
        """低成本动作：不占对话，但"确实做了点什么"。"""
        core = self.core
        if verb == "poke":
            uid = str(core.cfg("owner_id", "")).strip()
            if uid:
                r = await core.onebot("friend_poke", {"user_id": int(uid) if uid.isdigit() else uid})
                if r.get("status") == "ok":
                    core.store.note("poke", "戳了对方一下")
                    return {"ok": True, "kind": "poke"}
        elif verb == "like":
            uid = str(core.cfg("owner_id", "")).strip()
            if uid:
                r = await core.onebot(
                    "send_like", {"user_id": int(uid) if uid.isdigit() else uid, "times": 1}
                )
                if r.get("status") == "ok":
                    core.store.note("like", "点了个赞")
                    return {"ok": True, "kind": "like"}
        elif verb == "meme":
            img = self._pick_picture()
            if img:
                umo = self._pick_target("private")
                if umo:
                    try:
                        chain = MessageChain([Image.fromFileSystem(img)])
                        if text:
                            chain.chain.append(Plain(text))
                        await core.context.send_message(umo, chain)
                        core.store.note("meme", text or "（发了张图）")
                        return {"ok": True, "kind": "meme"}
                    except Exception as exc:
                        logger.debug(f"[idle_mind] 发表情失败: {exc}")

        # 什么都没成，至少把念头写进本子 —— 不浪费这一轮
        note = text or thought
        if note:
            core.store.note("note", note[:100])
            return {"ok": True, "kind": "note", "text": note}
        return {"ok": False, "reason": "小动作没成"}

    # ── 说话 ──
    async def _speak(self, verb: str, outlet: str, text: str, thought: str) -> dict:
        body = self._roughen(text or thought)
        if verb == "note":
            self.core.store.note("note", body[:120])
            return {"ok": True, "kind": "note", "text": body}
        if not body:
            return {"ok": False, "reason": "没话可说"}
        ok = await self._send(outlet, body)
        if ok:
            self.core.store.note("speak", f"[{outlet}] {body[:60]}")
        return {"ok": ok, "kind": "say", "outlet": outlet, "text": body}

    # ── 出口 ──
    def _pick_target(self, outlet: str) -> str:
        key = "group_targets" if outlet == "group" else "private_targets"
        targets = self.core.cfg_list(key, [])
        return random.choice(targets) if targets else ""

    async def _send(self, outlet: str, text: str) -> bool:
        """把一句话送到指定出口。"""
        if outlet == "journal":
            return self._write_journal(text)
        umo = self._pick_target(outlet)
        if not umo:
            logger.debug(f"[idle_mind] outlet={outlet} 没有配置目标，改记本子")
            self.core.store.note("note", text[:120])
            return False
        return await self.core.say(umo, text)

    def _write_journal(self, text: str) -> bool:
        """写进日记文件。纯文本追加，任何编辑器都能看。"""
        path = str(self.core.cfg("journal_file", "")).strip()
        if not path:
            from .core import DATA_DIR

            path = str(DATA_DIR / "journal.md")
        try:
            p = Path(path)
            p.parent.mkdir(parents=True, exist_ok=True)
            with open(p, "a", encoding="utf-8") as fh:
                fh.write(f"## {datetime.now():%Y-%m-%d %H:%M}\n\n{text}\n\n")
            self.core.store.note("journal", text[:100])
            return True
        except Exception as exc:
            logger.debug(f"[idle_mind] 写日记失败: {exc}")
            return False

    def _roughen(self, text: str) -> str:
        """偶尔发得粗糙一点：说一半、改口。

        真人发消息不会每次都是完整的句子。这个函数让 8% 的发言带上毛边，
        比任何提示词都更能掩盖"这是模型写的"。
        """
        t = (text or "").strip()
        if not t:
            return ""
        r = random.random()
        if r < 0.05 and len(t) > 12:
            return t[: max(6, int(len(t) * 0.6))].rstrip("，,、 ") + "…"
        if r < 0.08:
            return t + "　算了"
        return t

    def _pick_picture(self) -> str | None:
        """从配置的图片目录里随机挑一张。没配就返回 None。"""
        folder = str(self.core.cfg("picture_dir", "")).strip()
        if not folder:
            return None
        p = Path(folder)
        if not p.is_dir():
            return None
        try:
            files = [f for ext in ("*.png", "*.jpg", "*.jpeg", "*.gif", "*.webp") for f in p.rglob(ext)]
            return str(random.choice(files)) if files else None
        except Exception:
            return None

    # ── 观察：被纠正后记住 ──
    def observe(self, text: str) -> None:
        """从用户消息里捕捉情绪和"你说错了"。

        两件事：
        1. 有人回话了 → 清掉"上一条还没回"的成本
        2. 被纠正 → 记成教训，以后别提
        """
        try:
            if not text:
                return
            self.state["last_reply_to_me"] = time.time()
            self._nudge_mood(text)
            if any(k in text for k in ("不是", "错了", "谁说", "没说过", "你记错", "胡编")):
                self.worries.add(
                    f"上次说的事被纠正了（{clip(text, 30)}），别提这个",
                    tension=0.5,
                    kind="lesson",
                )
            self.core.save_state()
        except Exception:
            pass

    def _nudge_mood(self, text: str) -> None:
        """按消息措辞微调心情。只在 mood_mode=inline 时生效。"""
        mood = self.core.mood
        d_lib = d_agg = 0.0
        if any(k in text for k in ("好笨", "傻", "废物", "滚", "闭嘴", "烦人", "讨厌你", "垃圾")):
            d_agg, d_lib = 0.18, -0.08
        elif any(k in text for k in ("好厉害", "真棒", "喜欢你", "可爱", "聪明", "谢谢", "牛逼")):
            d_lib, d_agg = 0.12, -0.05
        elif any(k in text for k in ("哈哈", "笑死", "好玩")):
            d_lib = 0.08
        elif any(k in text for k in ("不理你", "不想聊", "走了", "别管")):
            d_lib, d_agg = -0.15, 0.10
        if d_lib or d_agg:
            mood.nudge(d_lib, d_agg)

    # ── 深夜整理 ──
    def maybe_sleep(self) -> None:
        """凌晨 3-6 点做一次清算，每天只做一次。"""
        now = datetime.now()
        today = f"{now:%Y-%m-%d}"
        if self.state.get("last_sleep") == today or not (3 <= now.hour < 6):
            return

        def damp():
            for k, base in (("libido", 0.5), ("aggression", 0.3)):
                if k in self.state:
                    self.state[k] = base + (to_float(self.state[k], base) - base) * 0.6
            self.core.save_state()

        result = self.worries.sleep_consolidate(damp_mood=damp)
        self.state["last_sleep"] = today
        self.core.save_state()
        logger.info(f"[idle_mind] 睡眠整理：{result['before']} → {result['after']} 件事")

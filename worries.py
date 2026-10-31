"""心事单 —— 它心里挂着的事。

这是整个插件里最"人"的部分。人不是每个念头都会说出口，说不出口的那些不会消失，
而是沉下去、慢慢变淡，被别的事盖过，偶尔又突然冒上来。这个文件就是那个"沉下去的地方"。

三条规则：
  叠加    同一件事再次想起 → 张力升高，而不是记两条
  半衰    张力按 7 天半衰期衰减，不清除、只变淡（淡到 0.12 以下才真正丢掉）
  整理    凌晨 3-6 点做一次清算，旧的归档、情绪冲淡

文件格式（公开契约，别的插件可以按这个格式读）：
  {"text": "心事内容", "tension": 0.62, "at": 1761900000.0, "kind": "hold"}

  text     内容，最多 80 字
  tension  张力 0~1，1 表示"非说不可"
  at       最后一次被想起的时间戳
  kind     hold（想说但忍住）/ manual（手动塞的）/ lesson（被纠正的教训）
"""

from __future__ import annotations

import json
import os
import time

from astrbot.api import logger

from .core import WORRIES_FILE, clip, to_float

HALF_LIFE_DAYS = 7.0  # 张力半衰期
DECAY_FLOOR = 0.12  # 淡到这个值以下就真正忘掉
DAMP_ON_SLEEP = 0.7  # 凌晨整理时张力打几折
MAX_WORRIES = 200  # 上限，防止无限堆积


class Worries:
    """心事单的内存副本 + 磁盘读写。

    内存里常驻一份，因为每次思考都要读 top N，反复开文件没必要。
    """

    def __init__(self, core):
        self.core = core
        self.items: list[dict] = self._load()
        self.decay()

    # ── 读写 ──
    def _load(self) -> list[dict]:
        out: list[dict] = []
        try:
            if not WORRIES_FILE.is_file():
                return out
            with open(WORRIES_FILE, encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        d = json.loads(line)
                    except Exception:
                        continue  # 单行损坏不影响其他行
                    if not isinstance(d, dict):
                        continue
                    text = clip(d.get("text"), 80)
                    if not text:
                        continue
                    out.append(
                        {
                            "text": text,
                            "tension": max(0.0, min(1.0, to_float(d.get("tension"), 0.5))),
                            "at": to_float(d.get("at"), time.time()),
                            "kind": str(d.get("kind") or "hold")[:12],
                        }
                    )
        except Exception as exc:
            logger.debug(f"[idle_mind] 读心事失败: {exc}")
        return out

    def save(self) -> None:
        """原子写：先写 .tmp 再替换。"""
        try:
            WORRIES_FILE.parent.mkdir(parents=True, exist_ok=True)
            tmp = str(WORRIES_FILE) + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                for w in self.items:
                    fh.write(json.dumps(w, ensure_ascii=False) + "\n")
            os.replace(tmp, WORRIES_FILE)
        except Exception as exc:
            logger.debug(f"[idle_mind] 写心事失败: {exc}")

    # ── 衰减 ──
    def decay(self) -> int:
        """按半衰期衰减张力，返回变化条数。"""
        now = time.time()
        changed = 0
        for w in self.items:
            days = max(0.0, (now - to_float(w.get("at"), now)) / 86400)
            factor = 0.5 ** (days / HALF_LIFE_DAYS)
            old = to_float(w.get("tension"), 0.5)
            new = old * factor
            if abs(new - old) > 0.01:
                w["tension"] = round(new, 4)
                w["at"] = now  # 只在真变化时更新时间，避免反复写盘
                changed += 1
        if changed:
            self.save()
        return changed

    # ── 写入 ──
    def add(self, text: str, tension: float = 0.6, kind: str = "hold") -> bool:
        """加一件心事。已经存在的就提高张力，而不是重复记一条。"""
        text = clip(text, 80)
        if not text:
            return False
        for w in self.items:
            if w["text"] == text:
                w["tension"] = min(1.0, to_float(w.get("tension"), 0.5) + 0.2)
                w["at"] = time.time()
                self.save()
                return True
        self.items.append(
            {
                "text": text,
                "tension": max(0.1, min(1.0, tension)),
                "at": time.time(),
                "kind": kind[:12],
            }
        )
        # 超上限时丢掉最淡的几条，优先保住"想得厉害"的
        if len(self.items) > MAX_WORRIES:
            self.items.sort(key=lambda x: -to_float(x.get("tension"), 0))
            dropped = self.items[MAX_WORRIES:]
            self.items = self.items[:MAX_WORRIES]
            for d in dropped:
                self.core.store.note("archive", f"装不下了：{d['text'][:40]}")
        self.save()
        return True

    def consume(self, text: str) -> bool:
        """说出口了就放下。返回是否确实移除了。"""
        text = clip(text, 80)
        for i, w in enumerate(self.items):
            if w["text"] == text:
                self.items.pop(i)
                self.save()
                return True
        return False

    def clear(self) -> int:
        n = len(self.items)
        self.items = []
        self.save()
        return n

    # ── 读取 ──
    def top(self, n: int = 4) -> list[dict]:
        return sorted(self.items, key=lambda w: -to_float(w.get("tension"), 0))[:n]

    def prompt_block(self, n: int = 4) -> str:
        """给模型看的心事段落。没有心事时明确说"没有"，别让模型自己编。"""
        items = self.top(n)
        if not items:
            return "（没有）"
        return "\n".join(f"- {w['text']}（想得厉害 {to_float(w.get('tension'), 0):.2f}）" for w in items)

    # ── 深夜整理 ──
    def sleep_consolidate(self, damp_mood=None) -> dict:
        """凌晨清算：张力打折、太淡的归档、情绪冲淡。

        只在 3-6 点、且当天没做过时执行。由主循环调用。
        damp_mood 是一个可选回调，用来顺带冲淡心情。
        """
        before = len(self.items)
        kept: list[dict] = []
        for w in self.items:
            w["tension"] = to_float(w.get("tension"), 0.4) * DAMP_ON_SLEEP
            if w["tension"] < DECAY_FLOOR:
                self.core.store.note("archive", f"淡忘了：{w['text'][:40]}")
            else:
                kept.append(w)
        self.items = kept
        self.save()
        if callable(damp_mood):
            try:
                damp_mood()
            except Exception:
                pass
        return {"before": before, "after": len(kept)}

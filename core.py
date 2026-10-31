"""底座：数据、人格、上下文、发送、心情。

这个文件的存在理由只有一个：让插件**可以单独安装**。

它不 import 任何其他插件，也不读任何其他插件的数据目录。别的插件装没装、
装的是什么版本，都与这里无关。唯一的外部读取是 AstrBot 自己的人设表，
读不到就用配置里的文本，再读不到就用一句通用的默认值。

三条纪律：
1. 所有对外部世界的读操作都必须能失败 —— 失败返回占位文本，不抛异常。
2. 所有写操作走临时文件 + 原子替换 —— 断电不会留下半个 JSON。
3. 定时循环里的异常就地消化 —— 抛出去等于让整个功能静默停摆。
"""

from __future__ import annotations

import json
import os
import random
import sqlite3
import time
from datetime import datetime
from pathlib import Path

import aiohttp

from astrbot.api import logger
from astrbot.core.message.components import Plain
from astrbot.core.message.message_event_result import MessageChain

# ── 路径 ──
# 持久化数据一律放在 data/plugin_data/ 下，不放在插件自身目录。
# 插件更新或重装时插件目录会被覆盖，数据放里面必丢。
# 环境变量只用于本地单测，线上不需要设置。
_DATA_ROOT = os.environ.get("IDLE_MIND_DATA") or "/AstrBot/data/plugin_data/astrbot_plugin_idle_mind"
DATA_DIR = Path(_DATA_ROOT)

# AstrBot 核心库，只读人设。属于宿主自带，不算第三方依赖。
CORE_DB = Path(os.environ.get("IDLE_MIND_CORE_DB") or "/AstrBot/data/data_v4.db")

WORRIES_FILE = DATA_DIR / "worries.jsonl"  # 心事单（对外公开格式，见 README）
STATE_FILE = DATA_DIR / "state.json"  # 自身状态
LOG_DIR = DATA_DIR / "activity"  # 活动记录，按天分文件

MAX_LOG_LINES = 4000  # 单个活动文件上限，超出后滚动
MAX_MSG_ROWS = 800  # 消息缓冲表上限
MAX_WORRY_TEXT = 80  # 单条心事文本上限


# ══════════════════════════════════════════════════════════
# 小工具
# ══════════════════════════════════════════════════════════


def read_json(path: Path, default):
    """读 JSON，任何异常都退回默认值。"""
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return default


def write_json(path: Path, data) -> bool:
    """原子写 JSON：先写 .tmp 再替换，避免写一半断电留下坏文件。"""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = str(path) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
        return True
    except Exception as exc:
        logger.debug(f"[idle_mind] 写 {path.name} 失败: {exc}")
        return False


def clip(text: str, limit: int) -> str:
    """压缩空白并截断，用于所有要进提示词或落盘的文本。"""
    t = " ".join(str(text or "").split())
    return t[:limit]


def parse_json_block(raw: str) -> dict | None:
    """从模型回复里抠出 JSON 对象。

    模型经常在 JSON 前后加解释、加 markdown 代码块，所以不能直接 json.loads。
    这里取第一个 { 到最后一个 } —— 对嵌套对象也成立。
    """
    if not raw:
        return None
    lo, hi = raw.find("{"), raw.rfind("}")
    if lo == -1 or hi <= lo:
        return None
    try:
        data = json.loads(raw[lo : hi + 1])
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def to_float(value, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


# ══════════════════════════════════════════════════════════
# 数据层
# ══════════════════════════════════════════════════════════


class MindStore:
    """极简 SQLite：消息缓冲 + 键值状态 + 笔记流水。

    为什么用 SQLite 而不是全用 JSON：消息缓冲要按会话倒序取最近 N 条，
    用 JSON 每次都要全量加载再排序，群多了会明显卡。
    """

    def __init__(self, db_path: Path | None = None):
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        self.db_path = str(db_path or (DATA_DIR / "mind.db"))
        self.ready = self._ensure()

    def _ensure(self) -> bool:
        try:
            with self._conn() as con:
                con.execute(
                    "CREATE TABLE IF NOT EXISTS msgs ("
                    " id INTEGER PRIMARY KEY AUTOINCREMENT,"
                    " session TEXT, sender TEXT, text TEXT, ts REAL)"
                )
                con.execute("CREATE INDEX IF NOT EXISTS idx_msgs_ts ON msgs(ts)")
                con.execute(
                    "CREATE TABLE IF NOT EXISTS notes ("
                    " id INTEGER PRIMARY KEY AUTOINCREMENT,"
                    " kind TEXT, text TEXT, ts REAL)"
                )
                con.execute("CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT)")
            return True
        except Exception as exc:
            logger.warning(f"[idle_mind] 数据库初始化失败，功能降级: {exc}")
            return False

    def _conn(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.db_path, timeout=8)
        con.row_factory = sqlite3.Row
        return con

    # ── 消息缓冲 ──
    def record(self, session: str, sender: str, text: str) -> None:
        """记一条消息。这是旁听，不参与回复流程。"""
        if not self.ready or not text:
            return
        try:
            with self._conn() as con:
                con.execute(
                    "INSERT INTO msgs (session, sender, text, ts) VALUES (?,?,?,?)",
                    (session or "", clip(sender, 32), clip(text, 300), time.time()),
                )
                # 超量后裁掉最老的，保持表小、查询快
                total = con.execute("SELECT COUNT(*) FROM msgs").fetchone()[0]
                if total > MAX_MSG_ROWS:
                    con.execute(
                        "DELETE FROM msgs WHERE id IN ( SELECT id FROM msgs ORDER BY id ASC LIMIT ?)",
                        (total - MAX_MSG_ROWS,),
                    )
        except Exception as exc:
            logger.debug(f"[idle_mind] 记录消息失败: {exc}")

    def recent(self, n: int = 6, session: str | None = None) -> str:
        """最近 n 条消息，格式「昵称：内容」，按时间正序。"""
        if not self.ready:
            return "（读不到）"
        try:
            with self._conn() as con:
                if session:
                    rows = con.execute(
                        "SELECT sender, text FROM msgs WHERE session = ? ORDER BY id DESC LIMIT ?",
                        (session, n),
                    ).fetchall()
                else:
                    rows = con.execute(
                        "SELECT sender, text FROM msgs ORDER BY id DESC LIMIT ?", (n,)
                    ).fetchall()
            lines = [f"{r['sender'] or '?'}：{r['text']}" for r in reversed(rows) if r["text"]]
            return "\n".join(lines) if lines else "（安静）"
        except Exception:
            return "（读不到）"

    # ── 笔记 / 活动 ──
    def note(self, kind: str, text: str) -> None:
        text = clip(text, 200)
        if not text:
            return
        try:
            with self._conn() as con:
                con.execute(
                    "INSERT INTO notes (kind, text, ts) VALUES (?,?,?)",
                    (clip(kind, 20), text, time.time()),
                )
        except Exception:
            pass
        # 同时按天写一份可读日志：用户想看"它今天想了什么"时直接 cat 就行
        try:
            path = LOG_DIR / f"activity-{datetime.now():%Y-%m-%d}.jsonl"
            if path.is_file() and sum(1 for _ in open(path, encoding="utf-8")) > MAX_LOG_LINES:
                return
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(
                    json.dumps(
                        {"at": datetime.now().isoformat(timespec="seconds"), "kind": kind, "text": text},
                        ensure_ascii=False,
                    )
                    + "\n"
                )
        except Exception:
            pass

    def today_notes(self, limit: int = 5) -> list[str]:
        """今天它自己做过的事，给提示词用。"""
        start = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        try:
            with self._conn() as con:
                rows = con.execute(
                    "SELECT text FROM notes WHERE ts >= ? ORDER BY id DESC LIMIT ?",
                    (start, limit),
                ).fetchall()
            return [r["text"] for r in reversed(rows)]
        except Exception:
            return []

    def stats(self) -> dict:
        if not self.ready:
            return {"msgs": 0, "notes": 0}
        try:
            with self._conn() as con:
                return {
                    "msgs": con.execute("SELECT COUNT(*) FROM msgs").fetchone()[0],
                    "notes": con.execute("SELECT COUNT(*) FROM notes").fetchone()[0],
                }
        except Exception:
            return {"msgs": 0, "notes": 0}


# ══════════════════════════════════════════════════════════
# 心情探针
# ══════════════════════════════════════════════════════════


class MoodProbe:
    """两个数值：想亲近（libido）、想怼人（aggression）。

    为什么自己算而不去读别的插件：读别人的文件就等于给自己装了个定时炸弹，
    对方改一次结构这边就静默读到空值，而且用户看不到任何报错。

    三种模式：
      inline  自己按聊天内容微调，随时间回落到基线（默认，零依赖）
      file    读一个 json 文件里的 libido/aggression（用户自己指定路径）
      none    固定中性值，只当常量用
    """

    BASELINE = (0.5, 0.3)  # 基线：中等亲近、低攻击
    DRIFT_PER_HOUR = 0.06  # 每小时向基线回落多少

    def __init__(self, state: dict, mode: str = "inline", path: str = ""):
        self.state = state
        self.mode = mode
        self.path = path

    def drives(self) -> tuple[float, float]:
        if self.mode == "none":
            return self.BASELINE
        if self.mode == "file" and self.path:
            ext = read_json(Path(self.path), {})
            if isinstance(ext, dict) and "libido" in ext:
                return (clamp(to_float(ext.get("libido"), 0.5)), clamp(to_float(ext.get("aggression"), 0.3)))
            # 文件读不到就退回自己的值，不要因为外部原因变成中性
        now = time.time()
        hours = max(0.0, (now - to_float(self.state.get("ts"), now)) / 3600)
        drift = self.DRIFT_PER_HOUR * hours
        lib = to_float(self.state.get("libido"), self.BASELINE[0])
        agg = to_float(self.state.get("aggression"), self.BASELINE[1])
        # 向基线靠，不越过基线
        lib = (
            max(self.BASELINE[0], lib - drift)
            if lib > self.BASELINE[0]
            else min(self.BASELINE[0], lib + drift)
        )
        agg = (
            max(self.BASELINE[1], agg - drift)
            if agg > self.BASELINE[1]
            else min(self.BASELINE[1], agg + drift)
        )
        lib, agg = clamp(lib), clamp(agg)
        self.state.update({"libido": lib, "aggression": agg, "ts": now})
        return lib, agg

    def nudge(self, d_lib: float, d_agg: float) -> None:
        """按一条消息的情绪微调。只在本模式为 inline 时生效。"""
        if self.mode != "inline":
            return
        lib, agg = self.drives()
        self.state.update({"libido": clamp(lib + d_lib), "aggression": clamp(agg + d_agg), "ts": time.time()})

    def word(self) -> str:
        lib, agg = self.drives()
        if lib >= 0.6 and agg >= 0.55:
            return "矛盾（想亲近又嘴硬）"
        if lib >= 0.6:
            return "粘人"
        if agg >= 0.6:
            return "炸毛"
        if lib >= 0.45 and agg <= 0.35:
            return "温和"
        if agg >= 0.45:
            return "有点烦"
        return "平淡"

    def hint(self) -> str:
        lib, agg = self.drives()
        w = self.word()
        tail = {
            "粘人": "想找他说话，语气软一点，会主动提话题。",
            "炸毛": "现在容易不耐烦，话短、带刺，不哄人。",
            "温和": "心情平稳偏亲近，正常聊。",
            "有点烦": "有点不耐烦，能一个字就别两个字。",
        }.get(w, "想靠近，但嘴上不肯承认，可以嘴硬、可以阴阳。" if w.startswith("矛盾") else "没什么波动。")
        return f"【此刻状态】{w}（想亲近 {lib:.2f} / 想怼人 {agg:.2f}）\n{tail}\n"


# ══════════════════════════════════════════════════════════
# 底座
# ══════════════════════════════════════════════════════════


class Core:
    """插件共用底座：配置、人格、发送、上下文。"""

    def __init__(self, context, config):
        self.context = context
        self.config = config or {}
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        self.store = MindStore()
        self.state = read_json(STATE_FILE, {})
        self.mood = MoodProbe(
            self.state,
            mode=str(self.cfg("mood_mode", "inline")),
            path=str(self.cfg("mood_file", "")),
        )
        self._persona_cache: tuple[float, str] = (0.0, "")

    # ── 配置 ──
    def cfg(self, key, default=None):
        """读配置。兼容 dict 和 AstrBotConfig，缺键时返回默认值。"""
        try:
            v = self.config.get(key, default)
            return default if v is None else v
        except Exception:
            return default

    def cfg_list(self, key, default: list[str]) -> list[str]:
        """读列表配置，允许用户手写逗号分隔的字符串。"""
        v = self.cfg(key, default)
        if isinstance(v, str):
            v = [t.strip() for t in v.split(",") if t.strip()]
        return list(v) if v else list(default)

    # ── 状态 ──
    def save_state(self) -> None:
        write_json(STATE_FILE, self.state)

    # ── 人格 ──
    def persona(self) -> str:
        """取人格提示词。

        顺序：自定义文本 → AstrBot 人设表 → 兜底。
        人设表结果缓存 5 分钟，避免每个周期都开一次数据库。
        """
        mode = str(self.cfg("persona_mode", "auto"))
        if mode == "custom":
            return str(self.cfg("persona_prompt", "")).strip() or self._fallback_persona()
        if mode == "none":
            return self._fallback_persona()

        now = time.time()
        cached_at, cached = self._persona_cache
        if cached and now - cached_at < 300:
            return cached

        text = str(self.cfg("persona_prompt", "")).strip()
        if not text:
            text = self._read_persona_from_core()
        text = text or self._fallback_persona()
        self._persona_cache = (now, text)
        return text

    def _read_persona_from_core(self) -> str:
        """从 AstrBot 自己的人设表读 default 人格。读不到返回空串。"""
        try:
            con = sqlite3.connect(f"file:{CORE_DB}?mode=ro", uri=True, timeout=5)
            try:
                row = con.execute("SELECT system_prompt FROM personas WHERE persona_id='default'").fetchone()
            finally:
                con.close()
            return (row[0] or "").strip() if row else ""
        except Exception:
            return ""

    def _fallback_persona(self) -> str:
        return "你是个有自己情绪和想法的普通网友，说话自然、口语化、不用书面语。"

    # ── 发送 ──
    async def say(self, umo: str, text: str) -> bool:
        """往指定会话发一条纯文本。失败返回 False，不抛。"""
        text = str(text or "").strip()
        if not umo or not text:
            return False
        try:
            await self.context.send_message(umo, MessageChain([Plain(text)]))
            return True
        except Exception as exc:
            logger.warning(f"[idle_mind] 发送到 {umo} 失败: {exc}")
            return False

    # ── OneBot 小动作（可选）──
    async def onebot(self, action: str, params: dict) -> dict:
        """调 OneBot HTTP 接口。没配地址就返回 failed，调用方自行降级。"""
        base = str(self.cfg("onebot_base", "")).rstrip("/")
        if not base:
            return {"status": "failed", "reason": "未配置 onebot_base"}
        token = str(self.cfg("onebot_token", ""))
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        try:
            timeout = aiohttp.ClientTimeout(total=12)
            async with aiohttp.ClientSession(timeout=timeout) as sess:
                async with sess.post(f"{base}/{action}", json=params, headers=headers) as resp:
                    data = await resp.json(content_type=None)
                    return data if isinstance(data, dict) else {"status": "failed"}
        except Exception as exc:
            logger.debug(f"[idle_mind] onebot {action} 失败: {exc}")
            return {"status": "failed"}

    # ── 时段 ──
    def in_quiet_hours(self, spec: str) -> bool:
        """判断当前是否落在免打扰时段。spec 形如 "1-7" 或 "23-6"。"""
        try:
            a, b = (int(x) for x in str(spec).replace(" ", "").split("-")[:2])
        except Exception:
            return False
        h = datetime.now().hour
        return a <= h < b if a <= b else (h >= a or h < b)

    def random_gap(self, lo_key: str, hi_key: str, lo_def: int, hi_def: int) -> int:
        """随机间隔（秒）。上下限配反了也能纠正。"""
        lo = int(to_float(self.cfg(lo_key), lo_def))
        hi = int(to_float(self.cfg(hi_key), hi_def))
        if hi < lo:
            lo, hi = hi, lo
        # 下限至少 1 分钟；注意这里的单位是分钟，写成 max(60, lo) 会变成至少 60 分钟
        lo = max(1, lo)
        hi = max(lo, hi)
        return random.randint(lo, hi) * 60

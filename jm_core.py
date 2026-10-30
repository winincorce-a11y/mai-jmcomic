"""JMComic 插件的纯逻辑层。

这一层刻意不依赖 ``maibot_sdk``、也不依赖 ``jmcomic``，只做这些事：

1. 从聊天文本里认出「车号」（JM123456 / 350234 / 禁漫链接 / p123456）；
2. 识别 AI 作品（禁漫在 tag 里给出 ``AI上色`` 之类的标记）；
3. 维护下载任务的状态机与按群/按人的每日配额；
4. 把任务、体积、耗时格式化成适合 QQ 群阅读的短文本。

这样既方便单测，也让 ``plugin.py`` 只负责把它接到 MaiBot 的 SDK 上。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence, TypeVar

import re
import time
import uuid

__all__ = [
    "AI_TAG_PATTERN",
    "JmRef",
    "Job",
    "QuotaBook",
    "chunked",
    "detect_ai_reason",
    "format_job_line",
    "human_duration",
    "human_size",
    "match_tags",
    "normalize_tag",
    "parse_tag_query",
    "natural_key",
    "parse_jm_ref",
    "parse_jm_refs",
    "short",
]

T = TypeVar("T")

# ---------------------------------------------------------------------------
# 车号解析
# ---------------------------------------------------------------------------

#: 禁漫常见 id 长度：4~8 位。纯数字只认 5~8 位，避免把年份（2024）当车号。
_MIN_ID = 1000
_MAX_ID = 9_999_999

_REF_RE = re.compile(
    r"""(?ix)
    (?:
        # JM123456 / jm-123456 / 禁漫 123456 / 车号：123456
        (?<![0-9a-z])(?P<prefix>jmcomic|jm|禁漫|车号|車號)\s*[-_:#：]?\s*
        (?P<prefix_id>\d{4,8})(?!\d)
      |
        # https://18comic.vip/album/123456/ 与 /photo/123456
        /(?:album|photo|photos|albums)/(?P<url_id>\d{4,8})(?!\d)
      |
        # p123456 / photo-123456
        (?<![0-9a-z])(?P<photo_tag>photo|p)\s*[-_:#]?\s*(?P<photo_id>\d{4,8})(?!\d)
      |
        # 直接甩一串数字
        (?<![\d.])(?P<plain_id>\d{5,8})(?![\d.])
    )
    """
)


@dataclass(frozen=True)
class JmRef:
    """一个车号引用。

    ``kind`` 为 ``album``（整本）或 ``photo``（单章）。
    """

    jm_id: str
    kind: str = "album"
    raw: str = ""

    @property
    def is_photo(self) -> bool:
        """是否指向单个章节。"""

        return self.kind == "photo"

    @property
    def label(self) -> str:
        """展示用短标签。"""

        return f"JM{self.jm_id}" + ("（单章）" if self.is_photo else "")


def _normalize_id(value: str) -> str | None:
    """把捕获到的数字规范成车号；超出范围返回 ``None``。"""

    text = str(value or "").strip()
    if not text.isdigit():
        return None
    number = int(text)
    if number < _MIN_ID or number > _MAX_ID:
        return None
    return str(number)


def parse_jm_refs(text: str, *, limit: int = 3) -> list[JmRef]:
    """从任意文本里解析车号，按出现顺序去重，最多 ``limit`` 个。

    >>> parse_jm_refs("看看 JM350234")[0].jm_id
    '350234'
    >>> parse_jm_refs("https://18comic.vip/photo/123456")[0].kind
    'photo'
    """

    if not text or limit <= 0:
        return []

    found: list[JmRef] = []
    seen: set[str] = set()

    for match in _REF_RE.finditer(text):
        raw = match.group(0).strip()
        kind = "album"

        if match.group("url_id"):
            value = match.group("url_id")
            kind = "photo" if "photo" in raw.lower() else "album"
        elif match.group("photo_id"):
            value = match.group("photo_id")
            kind = "photo"
        elif match.group("prefix_id"):
            value = match.group("prefix_id")
        else:
            value = match.group("plain_id")

        jm_id = _normalize_id(value or "")
        if jm_id is None or jm_id in seen:
            continue

        seen.add(jm_id)
        found.append(JmRef(jm_id=jm_id, kind=kind, raw=raw))
        if len(found) >= limit:
            break

    return found


def parse_jm_ref(text: str) -> JmRef | None:
    """只取第一个车号。"""

    refs = parse_jm_refs(text, limit=1)
    return refs[0] if refs else None


def looks_like_jm_text(text: str) -> bool:
    """判断一段文本是否值得当作「找本子」的请求。"""

    return parse_jm_ref(text) is not None


# ---------------------------------------------------------------------------
# 通用小工具
# ---------------------------------------------------------------------------


def chunked(items: Sequence[T], size: int) -> Iterator[list[T]]:
    """把序列切成固定大小的块。"""

    step = max(1, int(size))
    for start in range(0, len(items), step):
        yield list(items[start : start + step])


def short(text: Any, limit: int = 40) -> str:
    """压成单行短文本。"""

    value = re.sub(r"\s+", " ", str(text or "")).strip()
    if limit <= 0 or len(value) <= limit:
        return value
    return value[: max(1, limit - 1)].rstrip() + "…"


def natural_key(value: str) -> tuple[tuple[int, Any], ...]:
    """自然排序 key：``10.jpg`` 排在 ``2.jpg`` 后面。"""

    parts = re.split(r"(\d+)", str(value).lower())
    return tuple((0, int(part)) if part.isdigit() else (1, part) for part in parts if part != "")


def human_size(num_bytes: float) -> str:
    """字节数转人类可读。"""

    value = float(num_bytes or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f}{unit}" if unit == "B" else f"{value:.1f}{unit}"
        value /= 1024
    return f"{value:.1f}GB"


def human_duration(seconds: float) -> str:
    """秒数转人类可读。"""

    total = max(0, int(seconds or 0))
    if total < 60:
        return f"{total} 秒"
    minutes, secs = divmod(total, 60)
    if minutes < 60:
        return f"{minutes} 分 {secs} 秒"
    hours, minutes = divmod(minutes, 60)
    return f"{hours} 小时 {minutes} 分"


def count_images(directory: str | Path) -> int:
    """统计目录下已落盘的图片数量（用于下载进度）。"""

    root = Path(directory)
    if not root.is_dir():
        return 0
    total = 0
    try:
        for path in root.rglob("*"):
            if path.is_file() and path.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp", ".gif"}:
                total += 1
    except OSError:
        return total
    return total


# ---------------------------------------------------------------------------
# 任务状态机
# ---------------------------------------------------------------------------

STATE_QUEUED = "queued"
STATE_RUNNING = "running"
STATE_SENDING = "sending"
STATE_DONE = "done"
STATE_FAILED = "failed"
STATE_CANCELLED = "cancelled"

TERMINAL_STATES = frozenset({STATE_DONE, STATE_FAILED, STATE_CANCELLED})

_STATE_LABEL = {
    STATE_QUEUED: "排队",
    STATE_RUNNING: "下载中",
    STATE_SENDING: "发送中",
    STATE_DONE: "完成",
    STATE_FAILED: "失败",
    STATE_CANCELLED: "已取消",
}

_STATE_ICON = {
    STATE_QUEUED: "🕒",
    STATE_RUNNING: "📥",
    STATE_SENDING: "📤",
    STATE_DONE: "✅",
    STATE_FAILED: "❌",
    STATE_CANCELLED: "🚫",
}


@dataclass
class Job:
    """一次下载任务的完整生命周期。"""

    ref: JmRef
    stream_id: str
    user_id: str
    group_id: str = ""
    platform: str = ""

    task_id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    state: str = STATE_QUEUED
    stage: str = "排队中"

    title: str = ""
    author: str = ""
    page_count: int = 0
    chapter_count: int = 0
    total_images: int = 0
    done_images: int = 0
    sent_images: int = 0
    #: 已发出但平台没有回执的张数（QQ 常见，消息通常已在聊天记录里）
    unconfirmed_images: int = 0

    files: list[str] = field(default_factory=list)
    directory: str = ""
    error: str = ""
    warning: str = ""
    #: 命中 AI 过滤的原因；空串表示不是 AI 作品
    ai_reason: str = ""

    created_at: float = field(default_factory=time.time)
    started_at: float = 0.0
    finished_at: float = 0.0

    control: Any = field(default=None, repr=False, compare=False)
    task: Any = field(default=None, repr=False, compare=False)

    # -- 状态迁移 ---------------------------------------------------------
    def mark_running(self) -> None:
        """进入下载阶段。"""

        self.state = STATE_RUNNING
        self.stage = "下载中"
        self.started_at = time.time()

    def mark_sending(self) -> None:
        """进入发送阶段。"""

        self.state = STATE_SENDING
        self.stage = "发送中"

    def mark_done(self) -> None:
        """任务成功结束。"""

        self.state = STATE_DONE
        self.stage = "完成"
        self.finished_at = time.time()

    def mark_failed(self, reason: Any) -> None:
        """任务失败。"""

        self.state = STATE_FAILED
        self.stage = "失败"
        self.error = short(reason or "未知错误", 200)
        self.finished_at = time.time()

    def mark_cancelled(self, reason: Any = "已取消") -> None:
        """任务被取消。"""

        self.state = STATE_CANCELLED
        self.stage = "已取消"
        self.error = short(reason, 120)
        self.finished_at = time.time()

    @property
    def terminal(self) -> bool:
        """是否已经进入终态。"""

        return self.state in TERMINAL_STATES

    @property
    def duration(self) -> float:
        """任务耗时（秒）。"""

        start = self.started_at or self.created_at
        end = self.finished_at or time.time()
        return max(0.0, end - start)

    @property
    def display_title(self) -> str:
        """带书名的展示标题。"""

        if self.title:
            return f"JM{self.ref.jm_id}《{short(self.title, 28)}》"
        return f"JM{self.ref.jm_id}"

    def progress(self) -> str:
        """一行进度文本。"""

        if self.total_images:
            return f"{self.done_images}/{self.total_images} 张"
        if self.done_images:
            return f"{self.done_images} 张"
        return ""

    def to_line(self) -> str:
        """一行状态，用于 ``/jm status``。"""

        icon = _STATE_ICON.get(self.state, "•")
        label = _STATE_LABEL.get(self.state, self.state)
        progress = self.progress()
        suffix = f" {progress}" if progress and self.state == STATE_RUNNING else ""
        if self.state == STATE_FAILED and self.error:
            return f"{icon} [{label}] {self.display_title}：{self.error}（任务 {self.task_id}）"
        if self.state == STATE_DONE:
            return (
                f"{icon} [{label}] {self.display_title}"
                f"（下载 {self.done_images}，发送 {self.sent_images}，{human_duration(self.duration)}，任务 {self.task_id}）"
            )
        return f"{icon} [{label}] {self.display_title}{suffix}（任务 {self.task_id}）"


def format_job_line(job: Job) -> str:
    """``Job.to_line`` 的函数式包装。"""

    return job.to_line()


# ---------------------------------------------------------------------------
# 每日配额
# ---------------------------------------------------------------------------


@dataclass
class QuotaBook:
    """按会话记录每日下载次数。

    ``limit <= 0`` 表示不限制。记录以 ``g:<群号>`` / ``u:<QQ号>`` 为 key，
    跨天自动清零，可由插件持久化到 ``quota.json``。
    """

    limit: int = 0
    day: str = ""
    used: dict[str, int] = field(default_factory=dict)

    def _today(self) -> str:
        return date.today().isoformat()

    def roll(self) -> None:
        """跨天时清零。"""

        today = self._today()
        if self.day != today:
            self.day = today
            self.used = {}

    def remaining(self, key: str) -> int:
        """剩余次数；不限量时返回一个很大的数。"""

        self.roll()
        if self.limit <= 0:
            return 10**9
        return max(0, self.limit - int(self.used.get(key, 0)))

    def consume(self, key: str, amount: int = 1) -> bool:
        """尝试扣一次配额。"""

        self.roll()
        if self.limit <= 0:
            return True
        used = int(self.used.get(key, 0))
        if used + amount > self.limit:
            return False
        self.used[key] = used + amount
        return True

    def refund(self, key: str, amount: int = 1) -> None:
        """退回配额（任务失败时用）。"""

        self.roll()
        used = int(self.used.get(key, 0)) - amount
        if used <= 0:
            self.used.pop(key, None)
        else:
            self.used[key] = used

    def to_dict(self) -> dict[str, Any]:
        """转成可 JSON 序列化的结构。"""

        return {"day": self.day or self._today(), "used": dict(self.used)}

    @classmethod
    def from_dict(cls, data: Any, *, limit: int = 0) -> "QuotaBook":
        """从持久化数据恢复；坏数据直接丢弃。"""

        book = cls(limit=limit)
        if not isinstance(data, dict):
            book.day = book._today()
            return book

        day = str(data.get("day") or "")
        used = data.get("used")
        if day:
            book.day = day
        else:
            book.day = book._today()
        if isinstance(used, dict):
            clean: dict[str, int] = {}
            for key, value in used.items():
                try:
                    number = int(value)
                except (TypeError, ValueError):
                    continue
                if number > 0:
                    clean[str(key)] = number
            book.used = clean
        book.roll()
        return book


# ---------------------------------------------------------------------------
# 文件收集
# ---------------------------------------------------------------------------

_IMAGE_SUFFIXES = frozenset({".jpg", ".jpeg", ".png", ".webp", ".gif"})


def collect_images(directory: str | Path) -> list[str]:
    """收集目录下所有图片，按章节/文件名自然排序。"""

    root = Path(directory)
    if not root.is_dir():
        return []

    files = [
        str(path)
        for path in root.rglob("*")
        if path.is_file() and path.suffix.lower() in _IMAGE_SUFFIXES
    ]
    files.sort(key=lambda item: natural_key(str(Path(item).relative_to(root))))
    return files


def is_relative_to(path: Path, parent: Path) -> bool:
    """兼容 Python 3.9 的 ``Path.is_relative_to``。"""

    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except (OSError, ValueError):
        return False


# ---------------------------------------------------------------------------
# AI 作品识别
# ---------------------------------------------------------------------------

#: 禁漫用 tag 标注 AI 参与的作品，实测见过 ``AI上色``、``AI``、``AI生成``、
#: ``AI繪製`` 等写法；另外标题/描述里也会出现 ``[AI]``、``AI generated``。
#: 这里大小写不敏感地匹配「AI」相关的 tag 与文字片段。
AI_TAG_PATTERN = re.compile(
    r"""(?ix)
    \bai\b          # 独立的 AI
  | ai[繪绘]
  | ai生成
  | ai上色
  | ai着色
  | ai作画
  | aigenerated
  """
)

#: 命中后要忽略的干扰词（避免把 ``Raid``、``aid`` 之类误判成 AI）。
_AI_FALSE_POSITIVE = re.compile(r"(?i)^(?:raid|aid|said|again)$")


def detect_ai_reason(
    *,
    tags: Sequence[str] | None = None,
    title: str = "",
    description: str = "",
) -> str:
    """判断是否 AI 作品，返回命中的原因；不是则返回空串。

    只根据禁漫自己给出的结构化数据判定（tag 优先，其次标题/描述），
    不联网、不调用模型，保证搜索结果过滤和下载前校验结果一致。
    """

    for tag in tags or ():
        text = str(tag or "").strip()
        if not text or _AI_FALSE_POSITIVE.fullmatch(text):
            continue
        if AI_TAG_PATTERN.search(text):
            return f"标签「{short(text, 20)}」"

    for label, value in (("标题", title), ("描述", description)):
        text = str(value or "").strip()
        if not text:
            continue
        if AI_TAG_PATTERN.search(text):
            return f"{label}含 AI 标记"

    return ""


def iter_images(files: Iterable[str], limit: int | None = None) -> list[str]:
    """截取前 ``limit`` 张图片。"""

    items = list(files)
    if limit is None or limit <= 0:
        return items
    return items[:limit]


# ---------------------------------------------------------------------------
# 标签搜索
# ---------------------------------------------------------------------------

#: 标签分隔符：禁漫的标签既可能是「、」，也可能是空格或逗号。
_TAG_SPLIT = re.compile(r"[,，、\s]+")


def normalize_tag(value: Any) -> str:
    """把标签统一成可比较的形式（去空白、全角转半角、小写）。"""

    text = str(value or "").strip()
    if not text:
        return ""
    # 全角字母数字转半角，避免「ＡＩ」与「AI」被视为不同标签
    text = text.translate(
        {code: code - 0xFEE0 for code in range(0xFF01, 0xFF5F)}
    )
    return text.strip().lower()


def parse_tag_query(text: str) -> list[str]:
    """把用户输入拆成一组标签。

    支持 ``、``、``，``、``,`` 与空格分隔，例如：
    ``巨乳、NTR`` / ``巨乳,NTR`` / ``巨乳 NTR``。
    """

    return [tag for tag in (normalize_tag(part) for part in _TAG_SPLIT.split(str(text or ""))) if tag]


def match_tags(wanted: Iterable[str], actual: Iterable[str]) -> tuple[bool, tuple[str, ...]]:
    """判断作品标签是否满足要求。

    默认语义为 **AND**：要求用户给的每个标签都出现在作品标签里。
    返回 ``(是否全部命中, 命中的标签列表)``。

    匹配规则：
    - 大小写不敏感、忽略全半角差异；
    - 支持「包含」匹配，例如 ``汉化`` 能命中 ``中文汉化``；
    - 但反过来不成立：``中文汉化`` 不会命中作品里的 ``中文``。
    """

    actual_norm = [normalize_tag(tag) for tag in actual if normalize_tag(tag)]
    wanted_norm = [tag for tag in (normalize_tag(w) for w in wanted) if tag]
    if not wanted_norm:
        return False, ()

    matched: list[str] = []
    for want in wanted_norm:
        if any(want in have for have in actual_norm):
            matched.append(want)

    return len(matched) == len(wanted_norm), tuple(matched)

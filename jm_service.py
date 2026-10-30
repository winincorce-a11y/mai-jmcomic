"""对 ``jmcomic`` 的薄封装。

插件只依赖这一个文件里的接口，所有和 jmcomic 直接打交道的代码都集中在
这里，方便：

- 在没装 jmcomic 时把错误转成一句人话，而不是让插件加载失败；
- 单测里用假的 ``jmcomic`` 模块替换；
- 跟着 jmcomic 版本变化时只改一处。

jmcomic 的下载是同步阻塞的，调用方负责用 ``asyncio.to_thread`` 包起来。
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

import importlib
import logging
import threading
import time

logger = logging.getLogger(__name__)

try:  # 作为 MaiBot 插件包加载
    from .jm_core import JmRef, collect_images, match_tags, normalize_tag, short
except ImportError:  # 作为独立模块加载（单测 / 手动 import）
    from jm_core import JmRef, collect_images, match_tags, normalize_tag, short  # type: ignore[no-redef]

__all__ = [
    "DownloadOutcome",
    "DownloadSettings",
    "JmCancelled",
    "JmError",
    "JmService",
    "JmUnavailable",
    "MediaInfo",
    "SearchHit",
    "SearchResult",
    "TagSearchResult",
    "build_option_dict",
    "describe_media",
]

#: jmcomic 支持的目标图片后缀。
_IMAGE_SUFFIXES = frozenset({".jpg", ".jpeg", ".png", ".webp", ".gif"})

#: 标签搜索时并发查详情的线程数。实测 8 左右收益最好，但候选池变大后
#: 需要更高并发才不至于太慢，16 仍未见明显限流。
_TAG_PROBE_WORKERS = 16

#: 标签探测专用重试次数。详情请求失败时默认会重试 5 次并在多个域名间回退，
#: 实测单条最坏要拖到 ~30 秒。标签搜索的候选很多、个别失败不影响结果，
#: 所以这里用「快速失败」策略换整体耗时。
_TAG_PROBE_RETRY_TIMES = 1

#: 标签搜索每轮核对多少条候选（只用于多标签）。翻一页大概等多久由它决定：
#: 20 条约 20 秒。
_TAG_PROBE_CHUNK = 20

#: 服务端搜索/标签搜索每页的条数（实测 80）。
_SERVER_PAGE_SIZE = 80

#: 服务端给的总条数上限。实测热门搜索词、热门标签都返回 10000，说明这是封顶值，
#: 达到它只能对外说「至少 10000 部」。
_TOTAL_CAP = 10000

#: 标签搜索探测进度的缓存时长（秒）。
#:
#: 多标签会缓存**探测进度**（候选流 + 游标 + 已命中），用户翻到第 2 页时从
#: 上次的位置继续核对，既不会重头再查，也不会一次把候选全查完。
_TAG_CACHE_TTL = 600.0

#: 探测进度缓存条数上限。
_TAG_CACHE_MAX = 12

#: 服务端结果页缓存条数上限（单标签翻页用）。
_TAG_PAGE_CACHE_MAX = 40

#: 为了 AI 过滤而「补 tag」时最多愿意多等几秒。到点就走，补不到的条目就当
#: 没有 tag（AI 判定退回只看标题），绝不把翻页拖住。
_TAG_ENRICH_BUDGET = 8.0

#: 搜索结果封面用的尺寸后缀。``_3x4`` 是禁漫列表页的缩略图（实测 40~100KB），
#: 比原图小一个数量级，10 张封面也远小于单条合并转发的帧预算。
_COVER_SIZE = "_3x4"

#: 一次最多给多少条结果配封面。搜索页默认 10 条，这是兜底上限。
_COVER_MAX = 30

#: 下载封面的总时间预算（秒）。封面是锦上添花，到点就走，缺几张不影响列表。
_COVER_BUDGET = 30.0

#: 并发下载封面的线程数。
_COVER_WORKERS = 8


class JmError(Exception):
    """jmcomic 相关错误的统一基类。"""


class JmUnavailable(JmError):
    """jmcomic 未安装或导入失败。"""


class JmCancelled(JmError):
    """下载被用户取消。"""


@dataclass(frozen=True)
class DownloadSettings:
    """一次下载使用的 jmcomic 参数（由插件配置转换而来）。"""

    dir_rule: str = "Bd_Pname"
    threads: int = 8
    image_suffix: str = ""
    client_impl: str = "api"
    domains: tuple[str, ...] = ()
    proxy: str = ""
    decode: bool = True
    retry_times: int = 3


@dataclass(frozen=True)
class MediaInfo:
    """一本/一章的展示信息。"""

    jm_id: str
    kind: str
    title: str
    author: str = ""
    tags: tuple[str, ...] = ()
    page_count: int = 0
    chapter_count: int = 0
    album_id: str = ""


@dataclass(frozen=True)
class SearchHit:
    """搜索结果里的一条。"""

    jm_id: str
    title: str
    tags: tuple[str, ...] = ()


@dataclass(frozen=True)
class SearchResult:
    """一页搜索结果。

    服务端每页 80 条、支持真翻页，所以 ``total`` / ``page_count`` 都是服务端
    给的真实值；展示页按 ``page_size`` 切片后可能一页只占服务端页的一部分。
    """

    keyword: str
    page: int = 1
    page_count: int = 1
    total: int = 0
    hits: tuple[SearchHit, ...] = ()
    #: 本页在全部结果中的起始序号（从 1 开始；空页为 0）
    offset: int = 0
    #: 搜索模式：``site`` 站内 / ``work`` 作品系列 / ``tag`` 标签 / ``actor`` 角色
    mode: str = "site"
    #: 总数被服务端封顶（10000），真实数量可能更多
    capped: bool = False


@dataclass(frozen=True)
class TagSearchResult:
    """按标签搜索的**一页**结果。

    两种来源：``mode="server"`` 直接来自服务端标签搜索（单标签，数量与网页端
    同量级）；``mode="probe"`` 是逐条核对详情得来的（多标签 AND），因此额外
    报告候选与失败数量，方便向用户解释「为什么只找到这么多」。
    """

    tags: tuple[str, ...]
    #: 本页的作品
    hits: tuple[SearchHit, ...] = ()
    #: 命中的作品总数（分页前）
    total: int = 0
    page: int = 1
    page_count: int = 1
    #: 本页在全部命中里的起始序号（从 1 开始；空页为 0）
    offset: int = 0
    #: 候选池大小
    candidates: int = 0
    #: 已经查过详情的候选数（不是整个候选池）
    checked: int = 0
    #: 查详情失败的候选数（网络/限流）
    failed: int = 0
    #: 是否复用了上一轮的探测进度（候选池 + 已核对结果）
    cached: bool = False
    #: 候选池是否已经核对完。``False`` 表示后面还有候选没查，命中部数会继续增长
    exhausted: bool = False
    #: 总数被服务端封顶（10000），真实数量可能更多
    capped: bool = False
    #: ``server``=服务端标签搜索；``probe``=逐条核对详情
    mode: str = "probe"


@dataclass(frozen=True)
class _TagPage:
    """服务端标签搜索的一页（80 条）。"""

    items: tuple[tuple[str, str], ...]
    total: int
    page_count: int
    created: float


@dataclass
class _TagSession:
    """多标签搜索的**探测进度**，可以跨多次请求继续。

    候选流来自「结果最少的那个标签」的服务端分页，命中列表随用户翻页一点点
    攒，第 1 页不会先把整个候选池扒完。
    """

    option: Any
    wanted: tuple[str, ...] = ()
    #: 拿来当候选流的标签（结果最少的那个）
    base_tag: str = ""
    #: 最多核对多少条候选
    want_count: int = 0
    #: 候选流已经取到基准标签的第几页（服务端页号）
    server_page: int = 0
    #: 基准标签一共有几页
    server_pages: int = 0
    #: 待核对队列
    pending: list[tuple[str, str]] = field(default_factory=list)
    #: 已见过的车号，用于去重
    seen: set[str] = field(default_factory=set)
    #: 已经从候选流里取到多少条（去重后）
    pulled: int = 0
    #: 队列里下一条待核对的下标
    cursor: int = 0
    hits: list[SearchHit] = field(default_factory=list)
    checked: int = 0
    failed: int = 0
    #: 候选流是否已经取完（再取也没有了）
    pool_done: bool = False
    exhausted: bool = False
    created: float = 0.0


@dataclass
class DownloadOutcome:
    """一次下载的产物。"""

    info: MediaInfo
    files: list[str] = field(default_factory=list)
    directory: Path = Path(".")
    #: 部分图片失败等非致命问题的描述
    warning: str = ""


def _normalize_suffix(value: str) -> str:
    """把配置里的后缀规范成 ``.jpg`` 形式。"""

    text = str(value or "").strip().lower()
    if not text:
        return ""
    if not text.startswith("."):
        text = "." + text
    return text if text in _IMAGE_SUFFIXES else ""


def build_option_dict(base_dir: str | Path, settings: DownloadSettings) -> dict[str, Any]:
    """生成 ``JmOption.construct()`` 需要的配置字典。

    与 jmcomic 默认配置做深度合并，所以这里只写和默认值不同的部分。
    """

    option: dict[str, Any] = {
        # 群聊里下载不需要 jmcomic 往终端刷日志
        "log": False,
        "dir_rule": {
            "base_dir": str(Path(base_dir)),
            "rule": settings.dir_rule or "Bd_Pname",
        },
        "download": {
            "cache": True,
            "image": {"decode": bool(settings.decode)},
            "threading": {
                "image": max(1, int(settings.threads or 1)),
                # 多章节并发会让进度和取消都变得不可控，固定串行
                "photo": 1,
            },
        },
        "client": {
            "impl": settings.client_impl or "api",
            "retry_times": max(0, int(settings.retry_times)),
        },
    }

    suffix = _normalize_suffix(settings.image_suffix)
    if suffix:
        option["download"]["image"]["suffix"] = suffix

    domains = [item.strip() for item in settings.domains if str(item).strip()]
    if domains:
        option["client"]["domain"] = domains

    proxy = str(settings.proxy or "").strip()
    if proxy:
        option["client"]["postman"] = {
            "type": "curl_cffi",
            "meta_data": {"proxies": proxy},
        }

    return option


def _describe_exception(exc: BaseException) -> str:
    """把 jmcomic 的异常压成一句可读的原因。"""

    text = str(exc or "").strip()
    if not text:
        text = type(exc).__name__
    name = type(exc).__name__
    lowered = text.lower()

    if "cancelled" in lowered or name == "DownloadCancelledException":
        return "下载已取消"
    if "partial" in lowered or name == "PartialDownloadFailedException":
        return f"部分图片下载失败：{short(text, 120)}"
    if "retry" in lowered and "fail" in lowered:
        return f"请求重试全部失败，可能是域名被墙或代理不可用：{short(text, 100)}"
    if "timeout" in lowered or "timed out" in lowered:
        return f"请求超时：{short(text, 100)}"
    return short(text, 180)


class JmService:
    """jmcomic 的同步门面。

    实例内部缓存导入结果；所有方法都是阻塞的，插件侧必须放到线程里跑。
    """

    def __init__(self) -> None:
        self._module: Any = None
        self._load_error: str = ""
        #: 多标签的探测进度，key = (规范化后的标签元组, 候选上限)
        self._tag_cache: dict[tuple[Any, ...], _TagSession] = {}
        #: 服务端标签搜索的结果页，key = (规范化标签, 服务端页号)
        self._tag_page_cache: dict[tuple[Any, ...], _TagPage] = {}
        self._tag_cache_lock = threading.Lock()

    # -- 加载 ---------------------------------------------------------------
    def _load(self) -> Any:
        """导入 jmcomic；失败时抛出 :class:`JmUnavailable`。"""

        if self._module is not None:
            return self._module
        if self._load_error:
            raise JmUnavailable(self._load_error)

        try:
            module = importlib.import_module("jmcomic")
        except Exception as exc:  # ImportError / 依赖缺失 / 初始化异常
            self._load_error = (
                f"jmcomic 不可用（{type(exc).__name__}: {exc}）。"
                "请在 MaiBot 的运行环境中安装 jmcomic，或在插件清单里声明 python_package 依赖。"
            )
            raise JmUnavailable(self._load_error) from exc

        self._module = module
        return module

    def probe(self) -> tuple[bool, str]:
        """尝试导入，返回 ``(是否可用, 版本或错误信息)``。"""

        try:
            module = self._load()
        except JmUnavailable as exc:
            return False, str(exc)
        version = str(getattr(module, "__version__", "") or "unknown")
        return True, version

    # -- option -------------------------------------------------------------
    def build_option(self, base_dir: str | Path, settings: DownloadSettings) -> Any:
        """构造 jmcomic 的 ``JmOption``。"""

        module = self._load()
        option_dict = build_option_dict(base_dir, settings)
        try:
            return module.JmOption.construct(option_dict)
        except Exception as exc:
            raise JmError(f"jmcomic 配置构造失败：{_describe_exception(exc)}") from exc

    def new_control(self) -> Any:
        """创建一个可取消的 ``DownloadControl``。"""

        module = self._load()
        return module.DownloadControl()

    # -- 查询 ---------------------------------------------------------------
    def fetch(self, ref: JmRef, base_dir: str | Path, settings: DownloadSettings) -> MediaInfo:
        """读取整本/单章详情。"""

        option = self.build_option(base_dir, settings)
        try:
            client = option.new_jm_client()
            if ref.is_photo:
                detail = client.get_photo_detail(ref.jm_id)
            else:
                detail = client.get_album_detail(ref.jm_id)
        except Exception as exc:
            raise JmError(f"读取 JM{ref.jm_id} 详情失败：{_describe_exception(exc)}") from exc
        return self._info_from_detail(detail, ref)

    def search(
        self,
        keyword: str,
        page: int,
        base_dir: str | Path,
        settings: DownloadSettings,
        *,
        page_size: int = 10,
        mode: str = "site",
    ) -> SearchResult:
        """关键词搜索，**服务端真分页**。

        ``mode`` 就是接口的 ``main_tag`` 模式开关（实测都能翻页，每页 80 条）：

        | mode | main_tag | 含义 |
        | --- | --- | --- |
        | ``site`` | 0 | 站内搜索（标题/作者/标签等全字段） |
        | ``work`` | 1 | 按**作品系列**（原作）搜，如「蔚蓝档案」 |
        | ``author`` | 2 | 按**作者/画师**搜 |
        | ``tag`` | 3 | 按标签名搜（精确标签） |
        | ``actor`` | 4 | 按**登场人物/角色**搜 |

        注意：这里按「展示页 10 条」给结果，而服务端一页是 80 条，所以会把
        服务端页缓存起来在本地切（翻 8 页只请求一次）。
        """

        text = str(keyword or "").strip()
        if not text:
            raise JmError("搜索关键词不能为空")

        size = max(1, int(page_size or 1))
        page_number = max(1, int(page or 1))
        start = (page_number - 1) * size
        server_page = start // _SERVER_PAGE_SIZE + 1
        data = self._server_search_page(text, mode, server_page, base_dir, settings)

        total = data.total
        capped = total >= _TOTAL_CAP
        available = data.page_count * _SERVER_PAGE_SIZE if capped else total
        page_count = max(1, (available + size - 1) // size) if available else 1

        if total and page_number <= page_count:
            inner = start % _SERVER_PAGE_SIZE
            picked = data.items[inner : inner + size]
        else:
            picked = ()

        hits = tuple(SearchHit(jm_id=jm_id, title=title) for jm_id, title in picked)
        return SearchResult(
            keyword=text,
            page=page_number,
            page_count=page_count,
            total=total,
            hits=hits,
            offset=start + 1 if hits else 0,
            mode=mode,
            capped=capped,
        )

    def _server_search_page(
        self,
        keyword: str,
        mode: str,
        server_page: int,
        base_dir: str | Path,
        settings: DownloadSettings,
    ) -> _TagPage:
        """取服务端搜索的第 ``server_page`` 页（80 条），带缓存。"""

        key = ("search", mode, normalize_tag(keyword), server_page)
        cached = self._page_cache_get(key)
        if cached is not None:
            return cached

        option = self.build_option(base_dir, settings)
        try:
            client = option.new_jm_client()
            if mode == "work":
                res = client.search_work(keyword, page=server_page)
            elif mode == "author":
                res = client.search_author(keyword, page=server_page)
            elif mode == "tag":
                res = client.search_tag(keyword, page=server_page)
            elif mode == "actor":
                res = client.search_actor(keyword, page=server_page)
            else:
                res = client.search_site(keyword, page=server_page)
        except Exception as exc:
            raise JmError(f"搜索「{short(keyword, 30)}」失败：{_describe_exception(exc)}") from exc

        items = tuple((str(jm_id), str(title or "")) for jm_id, title, _tags in res.iter_id_title_tag())
        total = max(0, int(getattr(res, "total", 0) or 0))
        pages = int(getattr(res, "page_count", 0) or 0)
        if pages <= 0 and total > 0:
            pages = (total + _SERVER_PAGE_SIZE - 1) // _SERVER_PAGE_SIZE

        page = _TagPage(items=items, total=total, page_count=max(0, pages), created=time.monotonic())
        self._page_cache_put(key, page)
        return page

    def fetch_covers(
        self,
        album_ids: list[str],
        target_dir: str | Path,
        settings: DownloadSettings,
        *,
        budget: float = _COVER_BUDGET,
    ) -> dict[str, str]:
        """并发下载搜索结果封面，返回 ``{album_id: 本地文件路径}``。

        封面 URL 由车号直接拼出来（``/media/albums/<id>_3x4.jpg``），不需要
        先查详情，所以是「一次列表 + N 次小图」。下载失败或超时的条目直接
        缺席，调用方降级成纯文字即可。

        注意：``download_image`` 的域名回退是 jmcomic 内建的，失败重试沿用
        option 里的 ``retry_times``；这里额外套一层总预算，避免个别车号把
        整页搜索拖住。
        """

        ids = [str(item).strip() for item in album_ids if str(item).strip()]
        if not ids:
            return {}
        ids = ids[:_COVER_MAX]

        target = Path(target_dir)
        target.mkdir(parents=True, exist_ok=True)

        option = self.build_option(target, settings)
        module = self._load()
        text_cls = getattr(module, "JmcomicText", None)
        local = threading.local()

        def get_client() -> Any:
            client = getattr(local, "client", None)
            if client is None:
                client = option.new_jm_client()
                local.client = client
            return client

        def one(album_id: str) -> tuple[str, str]:
            path = target / f"{album_id}{_COVER_SIZE}.jpg"
            # 已经下过就直接复用：翻页/重复搜索不会重新拉图
            if path.exists() and path.stat().st_size > 0:
                return album_id, str(path)
            try:
                if text_cls is not None:
                    url = text_cls.get_album_cover_url(album_id, size=_COVER_SIZE)
                else:  # 兜底：老版本没有 JmcomicText 时自己拼
                    url = f"/media/albums/{album_id}{_COVER_SIZE}.jpg"
                get_client().download_image(
                    img_url=url,
                    img_save_path=str(path),
                    scramble_id=None,
                    decode_image=False,
                )
            except Exception:
                return album_id, ""
            if not path.exists() or path.stat().st_size <= 0:
                return album_id, ""
            return album_id, str(path)

        found: dict[str, str] = {}
        pool = ThreadPoolExecutor(max_workers=min(_COVER_WORKERS, len(ids)))
        futures = [pool.submit(one, album_id) for album_id in ids]
        try:
            for future in as_completed(futures, timeout=budget):
                album_id, path = future.result()
                if path:
                    found[album_id] = path
        except TimeoutError:
            logger.info("封面下载超过 %s 秒，已拿到 %s/%s 张", budget, len(found), len(ids))
        finally:
            pool.shutdown(wait=False, cancel_futures=True)
        return found

    #: 分类接口每页固定 80 条（实测 page_size=80）。
    _CATEGORY_PAGE_SIZE = 80

    # -- 标签搜索 -----------------------------------------------------------

    def clear_tag_cache(self) -> None:
        """清空标签搜索缓存（单测与手动排查时用）。"""

        with self._tag_cache_lock:
            self._tag_cache.clear()
            self._tag_page_cache.clear()

    def _probe_option(self, base_dir: str | Path, settings: DownloadSettings) -> Any:
        """构造探测专用 option（低重试、快速失败），client 由调用方按需创建。"""

        probe_settings = DownloadSettings(
            dir_rule=settings.dir_rule,
            threads=settings.threads,
            image_suffix=settings.image_suffix,
            client_impl=settings.client_impl,
            domains=settings.domains,
            proxy=settings.proxy,
            decode=settings.decode,
            retry_times=_TAG_PROBE_RETRY_TIMES,
        )
        return self.build_option(base_dir, probe_settings)

    def _tag_cache_get(self, key: tuple[Any, ...]) -> _TagSession | None:
        """取未过期的多标签探测进度。"""

        with self._tag_cache_lock:
            session = self._tag_cache.get(key)
            if session is None:
                return None
            if time.monotonic() - session.created > _TAG_CACHE_TTL:
                self._tag_cache.pop(key, None)
                return None
            return session

    def _tag_cache_put(self, key: tuple[Any, ...], session: _TagSession) -> None:
        """写入探测进度，顺带清理过期与超量条目。"""

        with self._tag_cache_lock:
            if len(self._tag_cache) >= _TAG_CACHE_MAX:
                now = time.monotonic()
                for stale in [k for k, v in self._tag_cache.items() if now - v.created > _TAG_CACHE_TTL]:
                    self._tag_cache.pop(stale, None)
                while len(self._tag_cache) >= _TAG_CACHE_MAX:
                    oldest = min(self._tag_cache, key=lambda k: self._tag_cache[k].created)
                    self._tag_cache.pop(oldest, None)
            self._tag_cache[key] = session

    def _page_cache_get(self, key: tuple[Any, ...]) -> _TagPage | None:
        """取未过期的服务端结果页。"""

        with self._tag_cache_lock:
            page = self._tag_page_cache.get(key)
            if page is None:
                return None
            if time.monotonic() - page.created > _TAG_CACHE_TTL:
                self._tag_page_cache.pop(key, None)
                return None
            return page

    def _page_cache_put(self, key: tuple[Any, ...], page: _TagPage) -> None:
        """写入服务端结果页，顺带清理过期与超量条目。"""

        with self._tag_cache_lock:
            if len(self._tag_page_cache) >= _TAG_PAGE_CACHE_MAX:
                now = time.monotonic()
                for stale in [k for k, v in self._tag_page_cache.items() if now - v.created > _TAG_CACHE_TTL]:
                    self._tag_page_cache.pop(stale, None)
                while len(self._tag_page_cache) >= _TAG_PAGE_CACHE_MAX:
                    oldest = min(self._tag_page_cache, key=lambda k: self._tag_page_cache[k].created)
                    self._tag_page_cache.pop(oldest, None)
            self._tag_page_cache[key] = page

    def _server_tag_page(self, option: Any, tag: str, server_page: int) -> _TagPage:
        """取服务端标签搜索的第 ``server_page`` 页（80 条），带缓存。

        这是 ``main_tag=3``（jmcomic 的 ``search_tag``）：**服务端按标签过滤、
        并且真的支持翻页**，所以单标签搜索不需要逐条查详情。
        """

        key = ("tag", normalize_tag(tag), server_page)
        cached = self._page_cache_get(key)
        if cached is not None:
            return cached

        try:
            client = option.new_jm_client()
        except Exception as exc:
            raise JmError(f"按标签搜索「{short(tag, 30)}」失败：{_describe_exception(exc)}") from exc

        try:
            res = client.search_tag(tag, page=server_page)
        except Exception as exc:
            raise JmError(f"按标签搜索「{short(tag, 30)}」失败：{_describe_exception(exc)}") from exc

        items = tuple((str(jm_id), str(title or "")) for jm_id, title, _tags in res.iter_id_title_tag())
        total = max(0, int(getattr(res, "total", 0) or 0))
        pages = int(getattr(res, "page_count", 0) or 0)
        if pages <= 0 and total > 0:
            pages = (total + _SERVER_PAGE_SIZE - 1) // _SERVER_PAGE_SIZE

        page = _TagPage(items=items, total=total, page_count=max(0, pages), created=time.monotonic())
        self._page_cache_put(key, page)
        return page

    def _probe_tag_map(
        self,
        option: Any,
        ids: list[str],
        *,
        budget: float = _TAG_ENRICH_BUDGET,
    ) -> dict[str, tuple[str, ...]]:
        """并发查详情补 tag，返回 ``{jm_id: tags}``；查不到或超时的条目直接缺席。

        这是**锦上添花**：列表本身已经可用，所以给一个总时间预算，到点就走，
        不会因为站点抽风把翻页拖成几十秒。
        """

        if not ids:
            return {}
        local = threading.local()

        def get_client() -> Any:
            client = getattr(local, "client", None)
            if client is None:
                client = option.new_jm_client()
                try:
                    client.retry_times = _TAG_PROBE_RETRY_TIMES
                except Exception:
                    pass
                local.client = client
            return client

        def one(jm_id: str) -> tuple[str, tuple[str, ...] | None]:
            try:
                detail = get_client().get_album_detail(jm_id)
            except Exception:
                return jm_id, None
            return jm_id, tuple(str(t) for t in (getattr(detail, "tags", ()) or ()))

        found: dict[str, tuple[str, ...]] = {}
        pool = ThreadPoolExecutor(max_workers=min(_TAG_PROBE_WORKERS, max(1, len(ids))))
        futures = [pool.submit(one, jm_id) for jm_id in ids]
        try:
            for future in as_completed(futures, timeout=budget):
                jm_id, tags = future.result()
                if tags is not None:
                    found[jm_id] = tags
        except TimeoutError:
            logger.info("标签补全超过 %s 秒，已补 %s/%s 条", budget, len(found), len(ids))
        finally:
            # 不等没回来的请求，避免把翻页拖住
            pool.shutdown(wait=False, cancel_futures=True)
        return found

    def _search_tag_only(
        self,
        tags: list[str],
        base_dir: str | Path,
        settings: DownloadSettings,
        page_number: int,
        size: int,
        with_tags: bool = False,
    ) -> TagSearchResult:
        """单标签：直接用服务端标签搜索，**一页只发一个请求**。

        服务端一次给 80 条且支持真翻页，所以翻一页就是一次请求。列表接口不
        返回每条作品的 tag；需要 tag（例如开了 AI 过滤）时再并发查一轮详情
        补上，补不到就空着，不影响列表本身。
        """

        query = tags[0]
        option = self._probe_option(base_dir, settings)
        start = (page_number - 1) * size
        server_page = start // _SERVER_PAGE_SIZE + 1
        data = self._server_tag_page(option, query, server_page)

        total = data.total
        #: 服务端把总条数封顶在 10000，达到上限就只知道「至少这么多」
        capped = total >= _TOTAL_CAP
        #: 服务端实际能翻到的条目数（封顶时就按 页数 × 80 算）
        available = data.page_count * _SERVER_PAGE_SIZE if capped else total
        page_count = max(1, (available + size - 1) // size) if available else 1

        if total and page_number <= page_count:
            inner = start % _SERVER_PAGE_SIZE
            picked = data.items[inner : inner + size]
        else:
            picked = ()

        tag_map: dict[str, tuple[str, ...]] = {}
        if with_tags and picked:
            tag_map = self._probe_tag_map(option, [jm_id for jm_id, _title in picked])
        hits = tuple(
            SearchHit(jm_id=jm_id, title=title, tags=tag_map.get(jm_id, ()))
            for jm_id, title in picked
        )

        return TagSearchResult(
            tags=tuple(tags),
            hits=hits,
            total=total,
            page=page_number,
            page_count=page_count,
            offset=start + 1 if hits else 0,
            checked=len(picked),
            failed=len(picked) - len(tag_map),
            exhausted=True,
            capped=capped,
            mode="server",
        )

    # -- 多标签（逐条核对详情做 AND） ----------------------------------------
    def _absorb(self, session: _TagSession, page: _TagPage) -> None:
        """把服务端一页候选并进待核对队列（去重）。"""

        for jm_id, title in page.items:
            if jm_id in session.seen:
                continue
            session.seen.add(jm_id)
            session.pending.append((jm_id, title))
        session.pulled = len(session.seen)

    def _new_tag_session(self, option: Any, tags: list[str], want_count: int) -> _TagSession:
        """建探测进度：先看每个标签各有多少结果，挑最稀有的当候选流。

        多标签是 AND 语义，而服务端的 ``search_query`` 是多词「或」匹配
        （实测同时给「巨乳 汉化」会把只带其中之一的作品也搜出来），所以必须
        逐条核对详情。用**结果最少的那个标签**当候选流，命中率最高、最省请求。
        """

        pages: dict[str, _TagPage] = {}

        def check(tag: str) -> tuple[str, _TagPage | None]:
            try:
                return tag, self._server_tag_page(option, tag, 1)
            except JmError as exc:
                logger.warning("标签「%s」探测失败：%s", tag, exc)
                return tag, None

        with ThreadPoolExecutor(max_workers=min(4, max(1, len(tags)))) as pool:
            for tag, page in pool.map(check, tags):
                if page is not None:
                    pages[tag] = page

        if not pages:
            raise JmError("标签搜索失败：逐个标签都没能拿到结果")

        base = min(pages, key=lambda tag: pages[tag].total)
        first = pages[base]
        session = _TagSession(
            option=option,
            wanted=tuple(tags),
            base_tag=base,
            want_count=max(1, int(want_count)),
            server_page=1,
            server_pages=first.page_count,
            created=time.monotonic(),
        )
        self._absorb(session, first)
        return session

    def _extend_pool(self, session: _TagSession) -> None:
        """候选流不够了：取基准标签的下一页（服务端 80 条一页）。"""

        if session.pool_done:
            return
        if session.pulled >= session.want_count:
            session.pool_done = True
            return

        next_page = session.server_page + 1
        if session.server_pages and next_page > session.server_pages:
            session.pool_done = True
            return

        try:
            page = self._server_tag_page(session.option, session.base_tag, next_page)
        except JmError as exc:
            logger.warning("标签搜索取候选第 %s 页失败：%s", next_page, exc)
            session.pool_done = True
            return

        session.server_page = next_page
        self._absorb(session, page)
        if not page.items or session.pulled >= session.want_count:
            session.pool_done = True

    def _fill_tag_session(self, session: _TagSession, wanted: list[str], size: int, target: int) -> None:
        """核对候选详情，直到「够 ``target`` 个命中」或候选核对完。"""

        local = threading.local()

        def get_client() -> Any:
            client = getattr(local, "client", None)
            if client is None:
                client = session.option.new_jm_client()
                # 快速失败：详情请求失败时默认会重试 5 次并跨域回退，
                # 单条最坏约 30 秒。标签搜索候选多、个别失败可接受，
                # 这里把重试降到 1 次换取整体耗时。
                try:
                    client.retry_times = _TAG_PROBE_RETRY_TIMES
                except Exception:
                    pass
                local.client = client
            return client

        def probe(item: tuple[str, str]) -> tuple[SearchHit | None, bool]:
            """查一条候选的详情，返回 (命中则 SearchHit, 是否请求失败)。"""

            jm_id, title = item
            try:
                detail = get_client().get_album_detail(jm_id)
            except Exception:
                return None, True
            actual = tuple(str(t) for t in (getattr(detail, "tags", ()) or ()))
            ok, _matched = match_tags(wanted, actual)
            if not ok:
                return None, False
            return SearchHit(jm_id=jm_id, title=str(getattr(detail, "title", "") or title), tags=actual), False

        chunk = max(_TAG_PROBE_CHUNK, size * 2)
        while len(session.hits) < target and not session.exhausted:
            if session.checked >= session.want_count:
                # 到达「最多核对多少条」的上限
                session.exhausted = True
                break
            if session.cursor >= len(session.pending):
                self._extend_pool(session)
            if session.cursor >= len(session.pending):
                session.exhausted = True
                break

            need = target - len(session.hits)
            take = min(max(chunk, need), session.want_count - session.checked)
            batch = session.pending[session.cursor : session.cursor + take]
            if not batch:
                session.exhausted = True
                break

            with ThreadPoolExecutor(max_workers=_TAG_PROBE_WORKERS) as pool:
                for hit, errored in pool.map(probe, batch):
                    session.checked += 1
                    if errored:
                        session.failed += 1
                        continue
                    if hit is not None:
                        session.hits.append(hit)

            session.cursor += len(batch)
            if session.pool_done and session.cursor >= len(session.pending):
                session.exhausted = True

        # 命中已经够这一页了：如果候选也已经取到头，就说明全都核对完了
        if (
            not session.exhausted
            and session.cursor >= len(session.pending)
            and (
                session.pool_done
                or (session.server_pages and session.server_page >= session.server_pages)
            )
        ):
            session.exhausted = True

    def _search_tags_verified(
        self,
        tags: list[str],
        base_dir: str | Path,
        settings: DownloadSettings,
        page_number: int,
        size: int,
        want_count: int,
    ) -> TagSearchResult:
        """多标签：用最稀有标签的候选流 + 逐条核对详情实现 AND 语义。"""

        option = self._probe_option(base_dir, settings)
        key = (tuple(normalize_tag(tag) for tag in tags), want_count)
        session = self._tag_cache_get(key)
        cached = session is not None
        if session is None:
            session = self._new_tag_session(option, tags, want_count)
            self._tag_cache_put(key, session)

        target = page_number * size
        self._fill_tag_session(session, tags, size, target)

        total = len(session.hits)
        if session.exhausted:
            page_count = max(1, (total + size - 1) // size)
        else:
            # 还有候选没核对，真实页数未知，只能保证「至少到这一页」
            page_count = page_number
        start = (page_number - 1) * size
        page_hits = session.hits[start : start + size]

        return TagSearchResult(
            tags=tuple(tags),
            hits=tuple(page_hits),
            total=total,
            page=page_number,
            page_count=page_count,
            offset=start + 1 if page_hits else 0,
            candidates=session.pulled,
            checked=session.checked,
            failed=session.failed,
            cached=cached,
            exhausted=session.exhausted,
            mode="probe",
        )

    def search_by_tags(
        self,
        tags: list[str],
        base_dir: str | Path,
        settings: DownloadSettings,
        *,
        max_candidates: int = 200,
        page: int = 1,
        page_size: int = 10,
        with_tags: bool = False,
        order_by: str = "latest",
    ) -> TagSearchResult:
        """按标签搜索第 ``page`` 页。

        两条路径：

        - **单标签**：走服务端标签搜索 ``search_tag``（``main_tag=3``）。实测它
          真的按标签过滤、并且**真的支持翻页**（每页 80 条，``巨乳`` 总条数
          封顶 10000、可翻 125 页），所以单标签是「一次请求一页」，数量级和
          网页端一致；
        - **多标签**：服务端 ``search_query`` 是多词「或」匹配，做不到 AND，
          于是挑结果最少的那个标签当候选流，**按页增量**逐条查详情核对，
          凑够这一页就停，不会一上来把整个候选池扒完。

        ``with_tags`` 只影响单标签：为真时额外查一轮详情把标签补齐（AI 过滤
        需要 tag），代价是多 10 个左右的详情请求。
        """

        wanted = [tag for tag in tags if str(tag or "").strip()]
        if not wanted:
            return TagSearchResult(tags=(), hits=(), total=0, page=1, page_count=1, offset=0)

        size = max(1, int(page_size or 1))
        page_number = max(1, int(page or 1))
        want_count = max(1, int(max_candidates))

        if len(wanted) == 1:
            return self._search_tag_only(wanted, base_dir, settings, page_number, size, with_tags)
        return self._search_tags_verified(wanted, base_dir, settings, page_number, size, want_count)

    # -- 下载 ---------------------------------------------------------------
    def download(
        self,
        ref: JmRef,
        directory: str | Path,
        settings: DownloadSettings,
        *,
        control: Any = None,
    ) -> DownloadOutcome:
        """下载整本/单章到 ``directory``，返回落盘图片列表。"""

        module = self._load()
        option = self.build_option(directory, settings)
        target = Path(directory)
        target.mkdir(parents=True, exist_ok=True)

        warning = ""
        try:
            if ref.is_photo:
                detail = module.download_photo(
                    ref.jm_id,
                    option,
                    check_exception=True,
                    control=control,
                )
            else:
                detail = module.download_album(
                    ref.jm_id,
                    option,
                    check_exception=True,
                    control=control,
                )
        except Exception as exc:
            name = type(exc).__name__.lower()
            if "cancelled" in name or "cancel" in str(exc).lower():
                raise JmCancelled(_describe_exception(exc)) from exc
            if "partial" in name:
                # 有一部分图片失败时，先把已经下载好的发出去，再在结尾提示
                warning = _describe_exception(exc)
                detail = None
            else:
                raise JmError(f"下载 JM{ref.jm_id} 失败：{_describe_exception(exc)}") from exc

        files = collect_images(target)
        if not files:
            reason = warning or "jmcomic 没有落盘任何图片"
            raise JmError(f"下载 JM{ref.jm_id} 失败：{reason}")

        if detail is None:
            info = MediaInfo(jm_id=ref.jm_id, kind=ref.kind, title="")
        else:
            # jmcomic >= 2.7 的 download_* 返回 DownloadResult(detail, downloader)，
            # 早期版本直接返回 detail；这里统一取实体。
            entity = getattr(detail, "detail", detail)
            info = self._info_from_detail(entity, ref, fallback=target)

        return DownloadOutcome(info=info, files=files, directory=target, warning=warning)

    # -- 内部 ---------------------------------------------------------------
    @staticmethod
    def _info_from_detail(detail: Any, ref: JmRef, *, fallback: Path | None = None) -> MediaInfo:
        """从 jmcomic 的实体对象提取展示信息，字段缺失时全部走默认值。"""

        title = str(getattr(detail, "title", "") or getattr(detail, "name", "") or "").strip()
        if not title and fallback is not None:
            title = fallback.name

        author = str(getattr(detail, "author", "") or "").strip()
        tags_value: Iterable[Any] = getattr(detail, "tags", ()) or ()
        tags = tuple(str(tag).strip() for tag in tags_value if str(tag).strip())

        if ref.is_photo:
            page_count = int(len(detail)) if hasattr(detail, "__len__") else 0
            chapter_count = 1
            album_id = str(getattr(detail, "album_id", "") or "")
        else:
            page_count = int(getattr(detail, "page_count", 0) or 0)
            chapter_count = len(getattr(detail, "episode_list", ()) or ())
            album_id = str(getattr(detail, "album_id", ref.jm_id) or ref.jm_id)

        return MediaInfo(
            jm_id=ref.jm_id,
            kind=ref.kind,
            title=title,
            author=author,
            tags=tags,
            page_count=page_count,
            chapter_count=chapter_count,
            album_id=album_id,
        )


def describe_media(info: MediaInfo) -> str:
    """把 :class:`MediaInfo` 排成群聊里能直接看的一段话。"""

    lines = [f"📖 JM{info.jm_id}《{short(info.title, 60)}》"]

    kind_text = "单章" if info.kind == "photo" else "整本"
    parts = [f"类型：{kind_text}"]
    if info.chapter_count > 1:
        parts.append(f"章节：{info.chapter_count}")
    if info.page_count:
        parts.append(f"页数：{info.page_count}")
    lines.append(" ｜ ".join(parts))

    if info.author:
        lines.append(f"作者：{short(info.author, 40)}")
    if info.tags:
        tags = " ".join(f"#{short(tag, 12)}" for tag in info.tags[:8])
        lines.append(f"标签：{tags}")

    return "\n".join(lines)

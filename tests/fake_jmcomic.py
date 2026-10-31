"""测试用的假 ``jmcomic`` 模块。

只实现插件实际用到的接口，行为足够真：会按 option 里的 ``base_dir`` 落盘
图片，支持取消，也能返回固定的搜索/详情数据。这样 CI 里不需要联网，也不
需要安装 curl_cffi。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import sys
import time
import types

IMAGE_BYTES = b"\xff\xd8\xff\xe0fake-jpeg-payload"

#: 每次下载暂停的秒数，取消测试会把它调大。
DELAY = 0.0

#: 是否把下载结果包一层（模拟 jmcomic 2.7 的 DownloadResult）。
WRAP_RESULT = False

#: 是否在写完图片后抛「部分失败」。
PARTIAL = False

#: 大本子测试：>0 时下载会写这么多张图（覆盖默认 6 张）
BIG_ALBUM = 0


class DownloadCancelledException(Exception):
    """对应 jmcomic 的取消异常。"""


class PartialDownloadFailedException(Exception):
    """对应 jmcomic 的部分失败异常。"""


class FakeControl:
    """最小可用的 DownloadControl。"""

    def __init__(self) -> None:
        self.cancelled = False
        self.reason = ""

    def cancel(self, reason: str = "download cancelled") -> bool:
        self.cancelled = True
        self.reason = reason
        return True

    def is_cancelled(self) -> bool:
        return self.cancelled


class FakeAlbum:
    """假的本子详情。"""

    #: 默认 tag，测试可通过修改它注入 AI 标记
    DEFAULT_TAGS = ["测试", "全彩"]

    def __init__(
        self,
        album_id: str,
        *,
        title: str = "",
        author: str = "测试作者",
        tags: list[str] | None = None,
        page_count: int = 6,
        chapters: int = 2,
    ) -> None:
        self.album_id = str(album_id)
        self.name = title or f"测试本子 {album_id}"
        self.authors = [author]
        self.tags = list(tags or self.DEFAULT_TAGS)
        self.description = ""
        self.page_count = page_count
        self.episode_list = [(str(index), str(index), f"第{index}话") for index in range(1, chapters + 1)]

    @property
    def title(self) -> str:
        return self.name

    @property
    def author(self) -> str:
        return self.authors[0] if self.authors else ""

    def __len__(self) -> int:
        return len(self.episode_list)


class FakePhoto:
    """假的章节详情。"""

    def __init__(self, photo_id: str, *, title: str = "", pages: int = 3) -> None:
        self.photo_id = str(photo_id)
        self.album_id = "999999"
        self.name = title or f"测试章节 {photo_id}"
        self._author = "测试作者"
        self.page_arr = [f"{index:05d}.jpg" for index in range(1, pages + 1)]

    @property
    def title(self) -> str:
        return self.name

    @property
    def author(self) -> str:
        return self._author

    @property
    def tags(self) -> list[str]:
        return ["单章"]

    def __len__(self) -> int:
        return len(self.page_arr)


class FakePage:
    """假的搜索结果页（站内 / 作品系列 / 标签 / 角色）。

    还原真实接口的两个关键行为：
    1. ``page`` 真的生效（服务端每页 80 条，翻页就是不同的作品）；
    2. ``total`` 是服务端给的总条数，可能远大于一页能装下的数量。
    """

    #: 服务端每页条数
    PAGE_SIZE = 80

    #: 该关键词一共有多少条结果（模拟服务端 total）
    HITS = 25

    #: 自定义标题列表；``None`` 时用默认标题
    titles: list[str] | None = None

    def __init__(self, keyword: str, page: int = 1, *, mode: str = "site") -> None:
        self.keyword = keyword
        self.page_number = page
        self.mode = mode

        if self.titles is not None:
            total = min(self.HITS, len(self.titles))
        else:
            total = self.HITS

        self.total = total
        self.page_size = self.PAGE_SIZE
        self.page_count = (total + self.PAGE_SIZE - 1) // self.PAGE_SIZE

        start = (page - 1) * self.PAGE_SIZE
        end = min(total, start + self.PAGE_SIZE)
        self.content = [
            (str(500000 + index), {"name": self._name(keyword, index), "tags": ["标签", "测试"]})
            for index in range(start + 1, end + 1)
        ]

    def _name(self, keyword: str, index: int) -> str:
        if self.titles is not None and index <= len(self.titles):
            return self.titles[index - 1]
        return f"{keyword} 第{index}本"

    def iter_id_title_tag(self):
        for album_id, info in self.content:
            yield album_id, info["name"], info.get("tags", [])


class FakeTagPage:
    """假的**服务端标签搜索**结果页（对应 jmcomic 的 ``search_tag``）。

    还原真实行为：服务端真的按标签过滤，``page`` 也真的生效（每页 80 条）。
    """

    #: 服务端每页条数
    PAGE_SIZE = 80

    #: 每个标签总共有多少部作品：{标签: 总数}，由测试按需设置
    TOTAL: dict[str, int] = {}

    def __init__(self, keyword: str, page: int = 1) -> None:
        key = str(keyword).strip().lower()
        total = int(self.TOTAL.get(key, 0))
        self.keyword = keyword
        self.page_number = page
        self.total = total
        self.page_size = self.PAGE_SIZE
        self.page_count = (total + self.PAGE_SIZE - 1) // self.PAGE_SIZE

        start = (page - 1) * self.PAGE_SIZE
        end = min(total, start + self.PAGE_SIZE)
        # 服务端标签搜索的列表接口不返回每条作品的 tag
        self.content = [
            (str(400000 + index), {"name": f"{keyword} 第{index}本", "tags": []})
            for index in range(start + 1, end + 1)
        ]

    def iter_id_title_tag(self):
        for album_id, info in self.content:
            yield album_id, info["name"], info.get("tags", [])


class FakeClient:
    """假的 jmcomic client。"""

    #: 按车号覆盖详情标签：{jm_id: [tag, ...]}，用于多标签 AND 核对测试
    ALBUM_TAGS: dict[str, list[str]] = {}

    def get_album_detail(self, album_id: str, allow_redirect: bool = True) -> FakeAlbum:
        del allow_redirect
        aid = str(album_id)
        if aid in self.ALBUM_TAGS:
            return FakeAlbum(aid, tags=list(self.ALBUM_TAGS[aid]))
        return FakeAlbum(aid)

    def get_photo_detail(self, photo_id: str, **kwargs: Any) -> FakePhoto:
        del kwargs
        return FakePhoto(str(photo_id))

    def search_site(self, keyword: str, page: int = 1, **kwargs: Any) -> FakePage:
        del kwargs
        return FakePage(keyword, page, mode="site")

    def search_work(self, keyword: str, page: int = 1, **kwargs: Any) -> FakePage:
        del kwargs
        return FakePage(keyword, page, mode="work")

    def search_author(self, keyword: str, page: int = 1, **kwargs: Any) -> FakePage:
        del kwargs
        return FakePage(keyword, page, mode="author")

    def search_actor(self, keyword: str, page: int = 1, **kwargs: Any) -> FakePage:
        del kwargs
        return FakePage(keyword, page, mode="actor")

    #: 封面下载失败的 album_id 集合（用于测「缺几张也能发」）
    COVER_FAIL: set[str] = set()

    def download_image(
        self,
        img_url: str,
        img_save_path: str,
        scramble_id: Any = None,
        decode_image: bool = False,
    ) -> None:
        del scramble_id, decode_image
        album_id = img_url.rsplit("/", 1)[-1].split("_")[0].split(".")[0]
        if album_id in self.COVER_FAIL:
            raise RuntimeError("模拟封面下载失败")
        Path(img_save_path).parent.mkdir(parents=True, exist_ok=True)
        Path(img_save_path).write_bytes(IMAGE_BYTES)

    def search_tag(self, keyword: str, page: int = 1, **kwargs: Any) -> FakeTagPage:
        del kwargs
        # main_tag=3：服务端按标签过滤，page 真的生效
        return FakeTagPage(keyword, page)


class FakeJmcomicText:
    """假的 JmcomicText，只需要 get_album_cover_url。"""

    #: 记录被请求过的封面 URL
    requested: list[str] = []

    @classmethod
    def get_album_cover_url(cls, album_id, image_domain=None, size: str = "") -> str:
        del image_domain
        url = f"https://cdn.example/media/albums/{album_id}{size}.jpg"
        cls.requested.append(url)
        return url


class FakeOption:
    """记录构造参数，并提供 client。"""

    last_data: dict[str, Any] = {}

    def __init__(self, data: dict[str, Any]) -> None:
        self.data = data
        type(self).last_data = data

    @classmethod
    def construct(cls, data: dict[str, Any], cover_default: bool = True) -> "FakeOption":
        del cover_default
        return cls(data)

    def new_jm_client(self) -> FakeClient:
        return FakeClient()


def _write_images(base: str | Path, *, chapters: int = 2, per_chapter: int = 3) -> list[Path]:
    """在 base 下写 chapters × per_chapter 张假图片。"""

    root = Path(base)
    files: list[Path] = []
    for chapter in range(1, chapters + 1):
        folder = root / f"第{chapter}话"
        folder.mkdir(parents=True, exist_ok=True)
        for index in range(1, per_chapter + 1):
            path = folder / f"{index:05d}.jpg"
            path.write_bytes(IMAGE_BYTES)
            files.append(path)
    return files


def _pause(control: FakeControl | None, seconds: float = 0.0) -> None:
    """模拟下载耗时，并在取消时抛异常。"""

    if control is not None and control.is_cancelled():
        raise DownloadCancelledException(control.reason or "download cancelled")
    delay = max(seconds, DELAY)
    if delay > 0:
        time.sleep(delay)


def download_album(
    jm_album_id: str,
    option: FakeOption | None = None,
    downloader: Any = None,
    *,
    check_exception: bool = True,
    extra: Any = None,
    control: FakeControl | None = None,
) -> Any:
    """假下载：写 2 章共 6 张图。"""

    del downloader, check_exception, extra
    assert option is not None
    base = option.data["dir_rule"]["base_dir"]
    _pause(control)
    if BIG_ALBUM > 0:
        files = _write_images(base, chapters=1, per_chapter=BIG_ALBUM)
    else:
        files = _write_images(base, chapters=2, per_chapter=3)
    _pause(control)
    album = FakeAlbum(str(jm_album_id), page_count=len(files), chapters=2 if BIG_ALBUM <= 0 else 1)
    if PARTIAL:
        raise PartialDownloadFailedException("2 张图片下载失败")
    if WRAP_RESULT:
        return types.SimpleNamespace(detail=album)
    return album


def download_photo(
    jm_photo_id: str,
    option: FakeOption | None = None,
    downloader: Any = None,
    *,
    check_exception: bool = True,
    extra: Any = None,
    control: FakeControl | None = None,
) -> FakePhoto:
    """假下载：写 3 张图。"""

    del downloader, check_exception, extra
    assert option is not None
    base = option.data["dir_rule"]["base_dir"]
    _pause(control)
    _write_images(base, chapters=1, per_chapter=3)
    _pause(control)
    return FakePhoto(str(jm_photo_id), pages=3)


def build_module() -> types.ModuleType:
    """构造一个可以塞进 ``sys.modules['jmcomic']`` 的模块。"""

    module = types.ModuleType("jmcomic")
    module.__version__ = "9.9.9-fake"
    module.JmOption = FakeOption
    module.DownloadControl = FakeControl
    module.DownloadCancelledException = DownloadCancelledException
    module.PartialDownloadFailedException = PartialDownloadFailedException
    module.JmcomicText = FakeJmcomicText
    module.download_album = download_album
    module.download_photo = download_photo
    return module


class FakeJmcomic:
    """上下文管理器：安装 / 卸载假模块。"""

    def __init__(self) -> None:
        self.previous: Any = None

    def __enter__(self) -> types.ModuleType:
        self.previous = sys.modules.get("jmcomic")
        module = build_module()
        sys.modules["jmcomic"] = module
        return module

    def __exit__(self, *exc_info: Any) -> None:
        if self.previous is None:
            sys.modules.pop("jmcomic", None)
        else:
            sys.modules["jmcomic"] = self.previous

"""jm_service 单测：option 生成、详情、搜索、下载、错误处理。"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
TESTS = Path(__file__).resolve().parent
for entry in (ROOT, TESTS):
    if str(entry) not in sys.path:
        sys.path.insert(0, str(entry))

from fake_jmcomic import IMAGE_BYTES, FakeJmcomic  # noqa: E402
from tmp_utils import make_temp_dir, remove_temp_dir  # noqa: E402

import fake_jmcomic  # noqa: E402
from jm_core import JmRef  # noqa: E402
from jm_service import (  # noqa: E402
    DownloadSettings,
    JmCancelled,
    JmError,
    JmService,
    JmUnavailable,
    build_option_dict,
    describe_media,
)


class OptionTests(unittest.TestCase):
    """option 字典生成。"""

    def test_defaults(self) -> None:
        data = build_option_dict("C:/comics", DownloadSettings())
        self.assertFalse(data["log"])
        self.assertEqual(data["dir_rule"]["base_dir"], str(Path("C:/comics")))
        self.assertEqual(data["dir_rule"]["rule"], "Bd_Pname")
        self.assertEqual(data["download"]["threading"]["photo"], 1)
        self.assertNotIn("postman", data["client"])

    def test_proxy_domains_and_suffix(self) -> None:
        settings = DownloadSettings(
            proxy="http://127.0.0.1:7890",
            domains=("a.example", " b.example "),
            image_suffix="jpg",
        )
        data = build_option_dict("/tmp/jm", settings)
        self.assertEqual(data["client"]["postman"]["meta_data"]["proxies"], "http://127.0.0.1:7890")
        self.assertEqual(data["client"]["domain"], ["a.example", "b.example"])
        self.assertEqual(data["download"]["image"]["suffix"], ".jpg")

    def test_bad_suffix_ignored(self) -> None:
        data = build_option_dict("/tmp/jm", DownloadSettings(image_suffix="exe"))
        self.assertNotIn("suffix", data["download"]["image"])

    def test_threads_floor(self) -> None:
        data = build_option_dict("/tmp/jm", DownloadSettings(threads=0))
        self.assertEqual(data["download"]["threading"]["image"], 1)


class ServiceTests(unittest.TestCase):
    """用假 jmcomic 跑完整流程。"""

    def setUp(self) -> None:
        fake_jmcomic.DELAY = 0.0
        self.module = FakeJmcomic()
        self.module.__enter__()
        self.service = JmService()
        self.base = make_temp_dir("svc-")
        self.settings = DownloadSettings(threads=2)

    def tearDown(self) -> None:
        self.service = JmService()
        self.module.__exit__(None, None, None)
        remove_temp_dir(self.base)
        fake_jmcomic.DELAY = 0.0
        fake_jmcomic.PARTIAL = False
        fake_jmcomic.WRAP_RESULT = False

    def test_probe(self) -> None:
        ok, detail = self.service.probe()
        self.assertTrue(ok)
        self.assertEqual(detail, "9.9.9-fake")

    def test_probe_without_module(self) -> None:
        with patch("jm_service.importlib.import_module", side_effect=ImportError("模拟 jmcomic 缺失")):
            service = JmService()
            ok, detail = service.probe()
            self.assertFalse(ok)
            self.assertIn("jmcomic", detail)
            with self.assertRaises(JmUnavailable):
                service.fetch(JmRef("123456"), self.base, self.settings)

    def test_fetch_album(self) -> None:
        info = self.service.fetch(JmRef("350234"), self.base, self.settings)
        self.assertEqual(info.jm_id, "350234")
        self.assertEqual(info.kind, "album")
        self.assertIn("测试本子", info.title)
        self.assertEqual(info.author, "测试作者")
        self.assertEqual(info.chapter_count, 2)
        self.assertIn("测试", info.tags)

    def test_fetch_photo(self) -> None:
        info = self.service.fetch(JmRef("123456", kind="photo"), self.base, self.settings)
        self.assertEqual(info.kind, "photo")
        self.assertEqual(info.page_count, 3)
        self.assertEqual(info.chapter_count, 1)

    def test_search(self) -> None:
        result = self.service.search("测试", 1, self.base, self.settings)
        self.assertEqual(result.page, 1)
        self.assertEqual(result.total, fake_jmcomic.FakePage.HITS)
        self.assertEqual(len(result.hits), 10)
        self.assertEqual(result.hits[0].jm_id, "500001")
        self.assertEqual(result.mode, "site")

    def test_search_uses_server_page(self) -> None:
        """服务端 page 真的生效：不同的展示页对应不同的作品。"""

        first = self.service.search("测试", 1, self.base, self.settings, page_size=10)
        second = self.service.search("测试", 2, self.base, self.settings, page_size=10)
        self.assertEqual([hit.jm_id for hit in first.hits], [str(500000 + i) for i in range(1, 11)])
        self.assertEqual([hit.jm_id for hit in second.hits], [str(500000 + i) for i in range(11, 21)])

    def test_search_crosses_server_page(self) -> None:
        """展示页 10 条、服务端页 80 条：第 9 页要跨到服务端第 2 页。"""

        fake_jmcomic.FakePage.HITS = 200
        try:
            ninth = self.service.search("测试", 9, self.base, self.settings, page_size=10)
        finally:
            fake_jmcomic.FakePage.HITS = 25
        # 第 9 页 = 第 81-90 部
        self.assertEqual([hit.jm_id for hit in ninth.hits], [str(500080 + i) for i in range(1, 11)])
        self.assertEqual(ninth.offset, 81)

    def test_search_pages_are_distinct(self) -> None:
        page1 = self.service.search("测试", 1, self.base, self.settings, page_size=10)
        page2 = self.service.search("测试", 2, self.base, self.settings, page_size=10)
        page3 = self.service.search("测试", 3, self.base, self.settings, page_size=10)

        self.assertEqual(page1.total, fake_jmcomic.FakePage.HITS)
        self.assertEqual(page1.page_count, 3)
        self.assertEqual([hit.jm_id for hit in page1.hits], [str(500000 + i) for i in range(1, 11)])
        self.assertEqual([hit.jm_id for hit in page2.hits], [str(500000 + i) for i in range(11, 21)])
        self.assertEqual([hit.jm_id for hit in page3.hits], [str(500000 + i) for i in range(21, 26)])
        self.assertNotEqual([hit.jm_id for hit in page1.hits], [hit.jm_id for hit in page2.hits])

    def test_search_out_of_range(self) -> None:
        page = self.service.search("测试", 99, self.base, self.settings, page_size=10)
        self.assertEqual(page.hits, ())
        self.assertEqual(page.page_count, 3)
        self.assertEqual(page.offset, 0)

    def test_search_offset_metadata(self) -> None:
        page2 = self.service.search("测试", 2, self.base, self.settings, page_size=10)
        self.assertEqual(page2.offset, 11)

    def test_search_empty_keyword(self) -> None:
        with self.assertRaises(JmError):
            self.service.search("   ", 1, self.base, self.settings)

    def test_search_mode_work_reports_server_total(self) -> None:
        """回归：作品系列搜索要报服务端真实总数，而不是一页的条数。"""

        fake_jmcomic.FakePage.HITS = 6865
        try:
            result = self.service.search("蔚蓝档案", 1, self.base, self.settings, mode="work")
        finally:
            fake_jmcomic.FakePage.HITS = 25

        self.assertEqual(result.mode, "work")
        self.assertEqual(result.total, 6865)
        self.assertEqual(result.page_count, 687)  # ceil(6865 / 10)
        self.assertEqual(len(result.hits), 10)

    def test_search_modes_author_and_actor(self) -> None:
        """author / actor 走各自的接口，并保留自己的 mode 标识。"""

        for mode in ("author", "actor"):
            with self.subTest(mode=mode):
                result = self.service.search("某人", 1, self.base, self.settings, mode=mode)
                self.assertEqual(result.mode, mode)
                self.assertEqual(result.total, fake_jmcomic.FakePage.HITS)
                self.assertEqual(len(result.hits), 10)

    def test_search_unknown_mode_falls_back_to_site(self) -> None:
        result = self.service.search("测试", 1, self.base, self.settings, mode="不存在的模式")
        self.assertEqual(result.total, fake_jmcomic.FakePage.HITS)
        self.assertEqual(result.hits[0].jm_id, "500001")

    def test_search_cache_is_per_mode(self) -> None:
        """不同 mode 不能共用同一份服务端页缓存。"""

        fake_jmcomic.FakePage.HITS = 200
        try:
            work = self.service.search("测试", 1, self.base, self.settings, mode="work")
            actor = self.service.search("测试", 1, self.base, self.settings, mode="actor")
        finally:
            fake_jmcomic.FakePage.HITS = 25

        self.assertEqual(work.mode, "work")
        self.assertEqual(actor.mode, "actor")

    def test_search_marks_capped_total(self) -> None:
        fake_jmcomic.FakePage.HITS = 10000
        try:
            result = self.service.search("蔚蓝档案", 1, self.base, self.settings)
        finally:
            fake_jmcomic.FakePage.HITS = 25

        self.assertTrue(result.capped)
        self.assertEqual(result.page_count, 1000)

    def test_download_album(self) -> None:
        outcome = self.service.download(JmRef("350234"), self.base / "JM350234", self.settings)
        self.assertEqual(len(outcome.files), 6)
        self.assertTrue(all(Path(path).is_file() for path in outcome.files))
        self.assertEqual(Path(outcome.files[0]).read_bytes(), IMAGE_BYTES)
        self.assertEqual(outcome.info.title, "测试本子 350234")

    def test_download_photo(self) -> None:
        outcome = self.service.download(JmRef("123456", kind="photo"), self.base / "JM123456", self.settings)
        self.assertEqual(len(outcome.files), 3)

    # -- 单标签：服务端标签搜索 ------------------------------------------
    def _tag_totals(self, **totals: int) -> None:
        fake_jmcomic.FakeTagPage.TOTAL = {key.lower(): value for key, value in totals.items()}

    def test_search_tag_uses_server_side_paging(self) -> None:
        """单标签走服务端标签搜索：真分页、不逐条查详情。"""

        self._tag_totals(巨乳=180)
        try:
            first = self.service.search_by_tags(["巨乳"], self.base, self.settings, page_size=10)
            second = self.service.search_by_tags(["巨乳"], self.base, self.settings, page=2, page_size=10)
        finally:
            self._tag_totals()

        self.assertEqual(first.mode, "server")
        self.assertEqual(first.total, 180)
        self.assertEqual(first.page_count, 18)
        self.assertEqual([hit.jm_id for hit in first.hits], [str(400001 + i) for i in range(10)])
        self.assertEqual(first.offset, 1)
        self.assertEqual([hit.jm_id for hit in second.hits], [str(400011 + i) for i in range(10)])
        self.assertEqual(second.offset, 11)
        self.assertTrue(first.exhausted)

    def test_search_tag_crosses_server_page(self) -> None:
        """展示页 10 条、服务端页 80 条：第 9 页要跨到服务端第 2 页。"""

        self._tag_totals(巨乳=180)
        try:
            ninth = self.service.search_by_tags(["巨乳"], self.base, self.settings, page=9, page_size=10)
        finally:
            self._tag_totals()

        # 第 9 页 = 第 81-90 部
        self.assertEqual([hit.jm_id for hit in ninth.hits], [str(400081 + i) for i in range(10)])
        self.assertEqual(ninth.offset, 81)

    def test_search_tag_no_result(self) -> None:
        self._tag_totals()  # 所有标签都是 0 条
        result = self.service.search_by_tags(["不存在的标签"], self.base, self.settings)
        self.assertEqual(result.hits, ())
        self.assertEqual(result.total, 0)
        self.assertEqual(result.mode, "server")

    def test_search_tag_page_out_of_range(self) -> None:
        self._tag_totals(巨乳=25)
        try:
            result = self.service.search_by_tags(["巨乳"], self.base, self.settings, page=9, page_size=10)
        finally:
            self._tag_totals()

        self.assertEqual(result.hits, ())
        self.assertEqual(result.offset, 0)
        self.assertEqual(result.total, 25)
        self.assertEqual(result.page_count, 3)

    def test_search_tag_marks_capped_total(self) -> None:
        """热门标签的总数被服务端封顶在 10000，要标记出来。"""

        self._tag_totals(巨乳=10000)
        try:
            result = self.service.search_by_tags(["巨乳"], self.base, self.settings, page_size=10)
        finally:
            self._tag_totals()

        self.assertTrue(result.capped)
        self.assertEqual(result.total, 10000)
        # 125 服务端页 × 80 = 10000 部 → 每页 10 条就是 1000 页
        self.assertEqual(result.page_count, 1000)

    def test_search_tag_enriches_tags_from_detail(self) -> None:
        """开了 AI 过滤才需要 tag：``with_tags`` 会另外查详情补齐。"""

        fake_jmcomic.FakeClient.ALBUM_TAGS = {"400001": ["巨乳", "中文"]}
        self._tag_totals(巨乳=25)
        try:
            plain = self.service.search_by_tags(["巨乳"], self.base, self.settings, page_size=10)
            enriched = self.service.search_by_tags(
                ["巨乳"], self.base, self.settings, page_size=10, with_tags=True
            )
        finally:
            self._tag_totals()
            fake_jmcomic.FakeClient.ALBUM_TAGS = {}

        # 默认不查详情：列表本身没有 tag
        self.assertEqual(plain.hits[0].tags, ())
        self.assertEqual(enriched.hits[0].tags, ("巨乳", "中文"))
        # 没配 ALBUM_TAGS 的走默认标签
        self.assertIn("测试", enriched.hits[1].tags)

    # -- 多标签：AND 语义，逐条核对 ---------------------------------------
    def _multi_tag_fixture(self, base_total: int = 120, hit_every: int = 6, other_total: int = 200) -> None:
        """NTR 结果少 → 当候选流；每 ``hit_every`` 部里有一部同时带巨乳。"""

        self._tag_totals(巨乳=other_total, NTR=base_total)
        ids = [str(400000 + index) for index in range(1, base_total + 1)]
        fake_jmcomic.FakeClient.ALBUM_TAGS = {
            jm_id: ["NTR", "中文"] for jm_id in ids
        }
        for index in range(hit_every, base_total + 1, hit_every):
            fake_jmcomic.FakeClient.ALBUM_TAGS[str(400000 + index)] = ["巨乳", "NTR", "中文"]

    def test_search_multi_tag_requires_all_tags(self) -> None:
        """多标签是 AND：只带其中一个标签的作品不能出现。"""

        self._multi_tag_fixture(base_total=12, hit_every=3)
        try:
            result = self.service.search_by_tags(["巨乳", "NTR"], self.base, self.settings)
        finally:
            self._tag_totals()
            fake_jmcomic.FakeClient.ALBUM_TAGS = {}

        ids = [hit.jm_id for hit in result.hits]
        self.assertEqual(ids, ["400003", "400006", "400009", "400012"])
        self.assertEqual(result.mode, "probe")
        self.assertTrue(result.exhausted)
        self.assertEqual(result.total, 4)

    def test_search_multi_tag_uses_rarest_as_candidate_stream(self) -> None:
        """候选流必须用结果最少的标签（NTR 120 条，而不是巨乳 200 条）。"""

        self._multi_tag_fixture(base_total=120, hit_every=6)
        try:
            first = self.service.search_by_tags(["巨乳", "NTR"], self.base, self.settings, page_size=10)
        finally:
            self._tag_totals()
            fake_jmcomic.FakeClient.ALBUM_TAGS = {}

        # 每 6 部命中 1 部；凑够 10 个命中要核对 60 条
        self.assertEqual(first.checked, 60)
        self.assertEqual(first.total, 10)
        self.assertFalse(first.exhausted)
        self.assertEqual(first.page_count, 1)

    def test_search_multi_tag_continues_on_next_page(self) -> None:
        self._multi_tag_fixture(base_total=120, hit_every=6)
        try:
            first = self.service.search_by_tags(["巨乳", "NTR"], self.base, self.settings, page_size=10)
            second = self.service.search_by_tags(["巨乳", "NTR"], self.base, self.settings, page=2, page_size=10)
        finally:
            self._tag_totals()
            fake_jmcomic.FakeClient.ALBUM_TAGS = {}

        self.assertTrue(second.cached)
        self.assertGreater(second.total, first.total)
        self.assertEqual([hit.jm_id for hit in second.hits][:1], ["400066"])
        # 第 2 页要凑够 20 个命中，把 120 条候选全核对了
        self.assertEqual(second.checked, 120)
        self.assertTrue(second.exhausted)
        self.assertEqual(second.page_count, 2)

    def test_search_multi_tag_respects_candidate_limit(self) -> None:
        self._multi_tag_fixture(base_total=120, hit_every=6)
        try:
            result = self.service.search_by_tags(
                ["巨乳", "NTR"], self.base, self.settings, max_candidates=20, page_size=10
            )
        finally:
            self._tag_totals()
            fake_jmcomic.FakeClient.ALBUM_TAGS = {}

        self.assertEqual(result.checked, 20)
        self.assertTrue(result.exhausted)
        self.assertEqual(result.total, 3)

    def test_search_multi_tag_page_out_of_range(self) -> None:
        self._multi_tag_fixture(base_total=12, hit_every=3)
        try:
            result = self.service.search_by_tags(["巨乳", "NTR"], self.base, self.settings, page=99)
        finally:
            self._tag_totals()
            fake_jmcomic.FakeClient.ALBUM_TAGS = {}

        self.assertEqual(result.hits, ())
        self.assertEqual(result.total, 4)
        self.assertEqual(result.page_count, 1)

    def test_search_by_tags_empty(self) -> None:
        result = self.service.search_by_tags([], self.base, self.settings)
        self.assertEqual(result.hits, ())
        self.assertEqual(result.total, 0)
        self.assertEqual(result.checked, 0)

    def test_search_multi_tag_cache_can_be_cleared(self) -> None:
        self._multi_tag_fixture(base_total=12, hit_every=3)
        try:
            self.service.search_by_tags(["巨乳", "NTR"], self.base, self.settings)
            self.service.clear_tag_cache()
            again = self.service.search_by_tags(["巨乳", "NTR"], self.base, self.settings)
        finally:
            self._tag_totals()
            fake_jmcomic.FakeClient.ALBUM_TAGS = {}

        self.assertFalse(again.cached)

    def test_search_multi_tag_cache_key_is_normalized(self) -> None:
        """标签大小写不同应该共用同一份探测进度。"""

        self._multi_tag_fixture(base_total=12, hit_every=3)
        try:
            self.service.search_by_tags(["巨乳", "NTR"], self.base, self.settings)
            again = self.service.search_by_tags(["巨乳", "ntr"], self.base, self.settings)
        finally:
            self._tag_totals()
            fake_jmcomic.FakeClient.ALBUM_TAGS = {}

        self.assertTrue(again.cached)

    def test_download_unwraps_result(self) -> None:
        fake_jmcomic.WRAP_RESULT = True
        try:
            outcome = self.service.download(JmRef("350234"), self.base / "JM-wrapped", self.settings)
        finally:
            fake_jmcomic.WRAP_RESULT = False
        self.assertEqual(outcome.info.title, "测试本子 350234")
        self.assertEqual(len(outcome.files), 6)

    def test_partial_failure_keeps_files(self) -> None:
        fake_jmcomic.PARTIAL = True
        try:
            outcome = self.service.download(JmRef("350234"), self.base / "JM-partial", self.settings)
        finally:
            fake_jmcomic.PARTIAL = False
        self.assertEqual(len(outcome.files), 6)
        self.assertIn("部分", outcome.warning)

    def test_download_cancelled(self) -> None:
        control = self.service.new_control()
        control.cancel("测试取消")
        with self.assertRaises(JmCancelled):
            self.service.download(JmRef("350234"), self.base / "JM1", self.settings, control=control)

    def test_describe_media(self) -> None:
        info = self.service.fetch(JmRef("350234"), self.base, self.settings)
        text = describe_media(info)
        self.assertIn("JM350234", text)
        self.assertIn("整本", text)
        self.assertIn("作者", text)


class ErrorTests(unittest.TestCase):
    """jmcomic 抛错时应转成 JmError。"""

    def test_download_error(self) -> None:
        module = FakeJmcomic()
        module.__enter__()
        try:
            import jmcomic

            def boom(*args, **kwargs):
                raise RuntimeError("网络炸了")

            jmcomic.download_album = boom
            service = JmService()
            tmp = make_temp_dir("err-")
            try:
                with self.assertRaises(JmError):
                    service.download(JmRef("350234"), tmp, DownloadSettings())
            finally:
                remove_temp_dir(tmp)
        finally:
            module.__exit__(None, None, None)


if __name__ == "__main__":
    unittest.main()

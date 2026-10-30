"""插件层单测：命令、后台任务、发送与限制。"""

from __future__ import annotations

import asyncio
import base64
import sys
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
TESTS = Path(__file__).resolve().parent
for entry in (ROOT, TESTS):
    if str(entry) not in sys.path:
        sys.path.insert(0, str(entry))

import fake_jmcomic  # noqa: E402
from fake_bot import build_plugin, wait_jobs  # noqa: E402
from fake_jmcomic import IMAGE_BYTES, FakeJmcomic  # noqa: E402
from tmp_utils import make_temp_dir, remove_temp_dir  # noqa: E402

import jm_service as jm_service_module  # noqa: E402
from plugin import JmComicPlugin  # noqa: E402


class PluginTestCase(unittest.IsolatedAsyncioTestCase):
    """每个用例一套干净的插件与假 jmcomic。"""

    config_patch: dict[str, Any] | None = None

    async def asyncSetUp(self) -> None:
        fake_jmcomic.DELAY = 0.0
        self.module = FakeJmcomic()
        self.module.__enter__()
        self.base = make_temp_dir("plg-")
        self.plugin = build_plugin(self.base, self.config_patch)
        await self.plugin.on_load()
        self.send = self.plugin.ctx.send

    async def asyncTearDown(self) -> None:
        await self.plugin.on_unload()
        self.module.__exit__(None, None, None)
        remove_temp_dir(self.base)
        fake_jmcomic.DELAY = 0.0
        fake_jmcomic.PARTIAL = False
        fake_jmcomic.WRAP_RESULT = False
        fake_jmcomic.FakeTagPage.TOTAL = {}
        fake_jmcomic.FakeClient.ALBUM_TAGS = {}

    def tag_totals(self, **totals: int) -> None:
        """设置每个标签在服务端有多少部作品。"""

        fake_jmcomic.FakeTagPage.TOTAL = {key.lower(): value for key, value in totals.items()}

    async def command(self, args: str, **kwargs: Any) -> None:
        """调用 ``/jm <args>``。"""

        await self.plugin.handle_jm(
            text=f"/jm {args}".strip(),
            stream_id=kwargs.get("stream_id", "s1"),
            group_id=kwargs.get("group_id", "g1"),
            user_id=kwargs.get("user_id", "u1"),
            platform="qq",
            matched_groups={"args": args},
        )

    def images_sent(self) -> list[bytes]:
        """把合并转发里的图片解码出来。"""

        result: list[bytes] = []
        for _, nodes in self.send.forwards:
            for node in nodes:
                for segment in node["segments"]:
                    if segment["type"] == "image":
                        result.append(base64.b64decode(segment["content"]))
        for _, data in self.send.images:
            result.append(base64.b64decode(data))
        return result


class BasicCommandTests(PluginTestCase):
    """帮助、详情、搜索、状态。"""

    async def test_help(self) -> None:
        await self.command("")
        self.assertIn("JMComic 下载", self.send.text_body)

    async def test_info(self) -> None:
        await self.command("info 350234")
        self.assertIn("JM350234", self.send.text_body)
        self.assertIn("测试本子", self.send.text_body)
        self.assertIn("作者", self.send.text_body)

    async def test_search(self) -> None:
        """搜索第 2 页：应与第 1 页结果不同，且标注页码。"""

        await self.command("search 测试 2")
        body = self.send.text_body
        self.assertIn("第 2/", body)
        # 第 2 页应从第 11 条开始（display=10）
        self.assertIn("JM500011", body)
        self.assertNotIn("JM500001", body)

    async def test_search_page_one(self) -> None:
        await self.command("search 测试")
        body = self.send.text_body
        self.assertIn("第 1/", body)
        self.assertIn("JM500001", body)
        self.assertIn("下一页", body)

    async def test_search_pages_differ(self) -> None:
        """核心回归：翻页必须返回不同结果。"""

        await self.command("search 测试 1")
        first = self.send.text_body
        await self.command("search 测试 2")
        second = self.send.text_body

        self.assertIn("JM500001", first)
        self.assertIn("JM500011", second)
        self.assertNotEqual(first, second)

    async def test_search_total_uses_actual_count(self) -> None:
        """以实际拿到条数为准，而不是接口里不准的 total。"""

        await self.command("search 测试")
        self.assertIn("共 25 条", self.send.text_body)

    async def test_search_out_of_range(self) -> None:
        await self.command("search 测试 99")
        self.assertIn("没有第 99 页", self.send.text_body)

    async def test_tag_search_finds_match(self) -> None:
        """单标签走服务端标签搜索，一次请求就有结果。"""

        self.tag_totals(巨乳=25)
        try:
            await self.command("tag 巨乳")
        finally:
            self.tag_totals()

        body = self.send.forward_text
        self.assertIn("标签「巨乳」第 1/3 页", body)
        self.assertIn("共命中 25 部", body)
        self.assertIn("JM400001", body)

    async def test_tag_search_multi_tag_needs_all_tags(self) -> None:
        """多标签是 AND：只带其中一个标签的作品不能出现。"""

        self.tag_totals(巨乳=200, NTR=12)
        fake_jmcomic.FakeClient.ALBUM_TAGS = {
            str(400000 + index): (["巨乳", "NTR"] if index % 3 == 0 else ["NTR"])
            for index in range(1, 13)
        }
        try:
            await self.command("tag 巨乳、NTR")
        finally:
            fake_jmcomic.FakeClient.ALBUM_TAGS = {}
            self.tag_totals()

        body = self.send.forward_text
        self.assertIn("JM400003", body)
        self.assertIn("候选", body)
        self.assertNotIn("JM400001", body)

    async def test_tag_search_no_result_hints(self) -> None:
        await self.command("tag 不存在的标签")
        body = self.send.forward_text
        self.assertIn("没有找到", body)

    async def test_tag_search_without_args(self) -> None:
        await self.command("tag")
        self.assertIn("用法", self.send.text_body)

    async def test_tag_search_sends_one_chat_record(self) -> None:
        """结果必须打包成一条合并转发，不能一条条刷屏。"""

        self.tag_totals(巨乳=25)
        try:
            await self.command("tag 巨乳")
        finally:
            self.tag_totals()

        self.assertEqual(len(self.send.forwards), 1)
        _stream_id, nodes = self.send.forwards[0]
        kinds = {node["segments"][0]["type"] for node in nodes}
        self.assertTrue(kinds <= {"text", "image"})
        self.assertIn("text", kinds)
        body = self.send.forward_text
        self.assertIn("JM400001", body)
        # 结果只出现在聊天记录里，普通消息里只有那句「正在搜索」
        plain = "\n".join(text for _, text in self.send.texts)
        self.assertNotIn("JM400001", plain)
        self.assertIn("正在按标签搜索", plain)

    async def test_tag_search_paginates(self) -> None:
        """翻页要返回不同的作品，并提示上一页/下一页。"""

        self.tag_totals(巨乳=180)
        try:
            await self.command("tag 巨乳 2")
        finally:
            self.tag_totals()

        body = self.send.forward_text
        self.assertIn("第 2/18 页", body)
        self.assertIn("JM400011", body)
        self.assertNotIn("JM400010", body)
        self.assertIn("上一页：/jm tag 巨乳 1", body)
        self.assertIn("下一页：/jm tag 巨乳 3", body)

    async def test_tag_search_first_page_hint(self) -> None:
        self.tag_totals(巨乳=180)
        try:
            await self.command("tag 巨乳")
        finally:
            self.tag_totals()

        body = self.send.forward_text
        self.assertIn("第 1/18 页", body)
        self.assertIn("JM400001", body)
        self.assertIn("下一页：/jm tag 巨乳 2", body)
        self.assertNotIn("上一页", body)

    async def test_tag_search_out_of_range(self) -> None:
        self.tag_totals(巨乳=25)
        try:
            await self.command("tag 巨乳 9")
        finally:
            self.tag_totals()

        body = self.send.text_body
        self.assertIn("没有第 9 页（共 3 页）", body)
        self.assertIn("/jm tag 巨乳 1", body)

    async def test_tag_search_marks_capped_total(self) -> None:
        """总数被服务端封顶时要写成 10000+。"""

        self.tag_totals(巨乳=10000)
        try:
            await self.command("tag 巨乳")
        finally:
            self.tag_totals()

        body = self.send.forward_text
        self.assertIn("共命中 10000+ 部", body)

    async def test_tag_search_falls_back_to_text(self) -> None:
        """合并转发被平台拒绝时，退回一条普通文本，不能什么都不发。"""

        self.tag_totals(巨乳=25)
        self.send.fail_forward = True
        try:
            await self.command("tag 巨乳")
        finally:
            self.tag_totals()

        self.assertEqual(self.send.forwards, [])
        self.assertIn("JM400001", self.send.text_body)

    async def test_tag_aliases(self) -> None:
        """标签搜索也认 标签 / t 等别名。"""

        for alias in ("标签 巨乳", "t 巨乳"):
            with self.subTest(alias=alias):
                self.send.texts.clear()
                await self.command(alias)
                self.assertIn("标签搜索", self.send.text_body)

    async def test_tag_search_uses_candidate_limit(self) -> None:
        """多标签时「核对上限」要真的生效。"""

        config = self.plugin.get_default_config()
        config["limits"]["cooldown_seconds"] = 0
        config["search"]["tag_candidates"] = 3
        self.plugin.set_plugin_config(config)
        self.tag_totals(巨乳=200, NTR=12)
        fake_jmcomic.FakeClient.ALBUM_TAGS = {
            str(400000 + index): (["巨乳", "NTR"] if index % 3 == 0 else ["NTR"])
            for index in range(1, 13)
        }
        try:
            await self.command("tag 巨乳、NTR")
        finally:
            fake_jmcomic.FakeClient.ALBUM_TAGS = {}
            self.tag_totals()

        self.assertIn("已核对 3 条", self.send.forward_text)

    async def test_bare_keyword_becomes_search(self) -> None:
        await self.command("不知道什么关键词")
        self.assertIn("搜索", self.send.text_body)

    async def test_invalid_info_arg(self) -> None:
        await self.command("info")
        self.assertIn("用法", self.send.text_body)

    async def test_search_without_keyword(self) -> None:
        await self.command("search")
        self.assertIn("用法", self.send.text_body)

    async def test_status_empty(self) -> None:
        await self.command("status")
        self.assertIn("没有 JM 下载任务", self.send.text_body)

    async def test_unknown_command_help(self) -> None:
        await self.command("help")
        self.assertIn("/jm 350234", self.send.text_body)

    async def test_series_search_reports_server_total(self) -> None:
        """回归：按作品系列搜要报服务端真实总数，而不是一页 80 条。"""

        fake_jmcomic.FakePage.HITS = 6865
        try:
            await self.command("series 蔚蓝档案")
        finally:
            fake_jmcomic.FakePage.HITS = 25

        body = self.send.text_body
        self.assertIn("按作品系列", body)
        self.assertIn("共 6865 条", body)
        self.assertIn("第 1/687 页", body)
        self.assertIn("JM500001", body)
        self.assertIn("下一页：/jm series 蔚蓝档案 2", body)

    async def test_series_search_paginates(self) -> None:
        fake_jmcomic.FakePage.HITS = 6865
        try:
            await self.command("series 蔚蓝档案 2")
        finally:
            fake_jmcomic.FakePage.HITS = 25

        body = self.send.text_body
        self.assertIn("第 2/687 页", body)
        self.assertIn("JM500011", body)
        self.assertIn("上一页：/jm series 蔚蓝档案 1", body)

    async def test_series_without_args(self) -> None:
        await self.command("series")
        self.assertIn("用法", self.send.text_body)

    async def test_series_aliases(self) -> None:
        for alias in ("系列 蔚蓝档案", "作品 蔚蓝档案", "work 蔚蓝档案"):
            with self.subTest(alias=alias):
                self.send.texts.clear()
                await self.command(alias)
                self.assertIn("按作品系列", self.send.text_body)

    async def test_author_search(self) -> None:
        await self.command("author ぽんこつ")
        body = self.send.text_body
        self.assertIn("按作者", body)
        self.assertIn("下一页：/jm author ぽんこつ 2", body)

    async def test_author_aliases(self) -> None:
        for alias in ("画师 ぽんこつ", "artist ぽんこつ", "作者 ぽんこつ"):
            with self.subTest(alias=alias):
                self.send.texts.clear()
                await self.command(alias)
                self.assertIn("按作者", self.send.text_body)

    async def test_author_without_args(self) -> None:
        await self.command("author")
        body = self.send.text_body
        self.assertIn("用法", body)
        self.assertIn("/jm author", body)

    async def test_actor_search(self) -> None:
        await self.command("actor 白洲アロナ")
        body = self.send.text_body
        self.assertIn("按登场人物", body)
        self.assertIn("下一页：/jm actor 白洲アロナ 2", body)

    async def test_actor_aliases(self) -> None:
        for alias in ("角色 白洲アロナ", "character 白洲アロナ", "登场人物 白洲アロナ"):
            with self.subTest(alias=alias):
                self.send.texts.clear()
                await self.command(alias)
                self.assertIn("按登场人物", self.send.text_body)

    async def test_actor_without_args(self) -> None:
        await self.command("actor")
        body = self.send.text_body
        self.assertIn("用法", body)
        self.assertIn("/jm actor", body)

    async def test_search_uses_site_mode(self) -> None:
        await self.command("search 测试")
        self.assertIn("按关键词", self.send.text_body)

    async def test_all_search_results_are_chat_records(self) -> None:
        """回归：五种搜索的结果都必须打包成一条合并转发，不能逐条刷屏。"""

        self.tag_totals(巨乳=40)
        try:
            cases = [
                ("search 测试", "关键词"),
                ("series 测试", "作品系列"),
                ("author 测试", "作者"),
                ("actor 测试", "登场人物"),
                ("tag 巨乳", "标签"),
            ]
            for args, label in cases:
                with self.subTest(mode=label):
                    self.send.texts.clear()
                    self.send.forwards.clear()
                    await self.command(args)

                    # 恰好一条聊天记录
                    self.assertEqual(len(self.send.forwards), 1, label)
                    _stream_id, nodes = self.send.forwards[0]
                    kinds = {node["segments"][0]["type"] for node in nodes}
                    self.assertTrue(kinds <= {"text", "image"}, label)
                    # 标题节点一定在（封面是可选装饰）
                    self.assertIn("text", kinds, label)
                    self.assertGreater(len(nodes), 1, label)

                    # 结果本身不在普通文本里，普通文本只有那句「正在搜索」
                    plain = "\n".join(text for _, text in self.send.texts)
                    self.assertNotIn("JM50", plain, label)
                    self.assertNotIn("JM40", plain, label)
                    self.assertIn("正在按", plain, label)
        finally:
            self.tag_totals()

    async def test_search_record_includes_hints(self) -> None:
        """翻页提示要跟着结果一起进聊天记录。"""

        await self.command("search 测试")
        body = self.send.forward_text
        self.assertIn("下一页：/jm search 测试 2", body)
        self.assertIn("发送 /jm 车号 下载", body)

    async def test_search_record_falls_back_to_text(self) -> None:
        """合并转发被平台拒绝时退回普通文本，不能什么都不发。"""

        self.send.fail_forward = True
        await self.command("search 测试")

        self.assertEqual(self.send.forwards, [])
        self.assertIn("JM500001", self.send.text_body)

    async def test_search_shows_full_title(self) -> None:
        """回归：标题不能截断，否则群里根本看不出是哪一本。"""

        long_title = (
            "[欶瀾漢化組] [ちぃずオレ] ダイビング部ご奉仕漫畫【前編】 "
            "(ブルーアーカイブ) [中國翻譯] 這是一個刻意很長很長的標題用來驗證不截斷"
        )
        fake_jmcomic.FakePage.titles = [long_title]
        fake_jmcomic.FakePage.HITS = 1
        try:
            await self.command("search 测试")
        finally:
            fake_jmcomic.FakePage.titles = None
            fake_jmcomic.FakePage.HITS = 25

        self.assertIn(long_title, self.send.forward_text)

    async def test_search_attaches_covers(self) -> None:
        """每条结果的封面与标题必须在**同一条消息**里（一个节点两个段）。"""

        await self.command("search 测试")
        _stream_id, nodes = self.send.forwards[0]

        with_cover = [node for node in nodes if len(node["segments"]) == 2]
        # 10 条结果 → 10 个「图 + 标题」节点
        self.assertEqual(len(with_cover), 10)
        for node in with_cover:
            self.assertEqual(
                [seg["type"] for seg in node["segments"]], ["image", "text"]
            )
        # 没有任何「只有一个 image 段」的独立封面节点
        self.assertFalse(
            [node for node in nodes if [s["type"] for s in node["segments"]] == ["image"]]
        )

    async def test_covers_can_be_disabled(self) -> None:
        config = self.plugin.get_default_config()
        config["limits"]["cooldown_seconds"] = 0
        config["search"]["cover"] = False
        self.plugin.set_plugin_config(config)

        await self.command("search 测试")
        _stream_id, nodes = self.send.forwards[0]
        kinds = {node["segments"][0]["type"] for node in nodes}
        self.assertEqual(kinds, {"text"})

    async def test_cover_count_caps_images(self) -> None:
        config = self.plugin.get_default_config()
        config["limits"]["cooldown_seconds"] = 0
        config["search"]["cover_count"] = 3
        self.plugin.set_plugin_config(config)

        await self.command("search 测试")
        _stream_id, nodes = self.send.forwards[0]
        image_segments = [
            seg for node in nodes for seg in node["segments"] if seg["type"] == "image"
        ]
        self.assertEqual(len(image_segments), 3)
        # 标题仍然 10 条（一个节点一条）
        title_nodes = [n for n in nodes if n["segments"][-1]["type"] == "text"]
        self.assertGreaterEqual(len(title_nodes), 10)

    async def test_cover_failure_still_sends_list(self) -> None:
        """个别封面下载失败只是少几张图，列表照发。"""

        fake_jmcomic.FakeClient.COVER_FAIL = {"500001", "500002"}
        try:
            await self.command("search 测试")
        finally:
            fake_jmcomic.FakeClient.COVER_FAIL = set()

        _stream_id, nodes = self.send.forwards[0]
        image_segments = [
            seg for node in nodes for seg in node["segments"] if seg["type"] == "image"
        ]
        self.assertEqual(len(image_segments), 8)
        self.assertIn("JM500001", self.send.forward_text)

    async def test_tag_search_attaches_covers(self) -> None:
        self.tag_totals(巨乳=25)
        try:
            await self.command("tag 巨乳")
        finally:
            self.tag_totals()

        _stream_id, nodes = self.send.forwards[0]
        image_segments = [
            seg for node in nodes for seg in node["segments"] if seg["type"] == "image"
        ]
        self.assertEqual(len(image_segments), 10)

    async def test_search_no_result_is_also_a_record(self) -> None:
        fake_jmcomic.FakePage.HITS = 0
        try:
            await self.command("search 不存在")
        finally:
            fake_jmcomic.FakePage.HITS = 25

        self.assertEqual(len(self.send.forwards), 1)
        self.assertIn("没有搜到", self.send.forward_text)


class FilterTests(PluginTestCase):
    """AI 作品过滤。"""

    async def test_filter_disabled_by_default(self) -> None:
        await self.command("search 测试")
        self.assertNotIn("已过滤", self.send.text_body)

    async def test_search_filter_hides_ai(self) -> None:
        fake_jmcomic.FakePage.HITS = 4
        try:
            self.plugin.set_plugin_config(
                self._config_with(
                    filter={"enabled": True, "scope": "search", "keep_in_search": False},
                    search={"display": 10},
                )
            )
            # 前两条是 AI 作品，后两条正常
            fake_jmcomic.FakePage.titles = ["AI上色本", "[AI]作品", "正常本1", "正常本2"]
            await self.command("search 测试")
            body = self.send.text_body
            self.assertIn("已过滤 2 条 AI 作品", body)
            self.assertNotIn("AI上色本", body)
            self.assertNotIn("[AI]作品", body)
            self.assertIn("正常本1", body)
        finally:
            fake_jmcomic.FakePage.HITS = 25
            fake_jmcomic.FakePage.titles = None

    async def test_search_keep_in_search_marks_only(self) -> None:
        self.plugin.set_plugin_config(
            self._config_with(
                filter={"enabled": True, "scope": "search", "keep_in_search": True},
                search={"display": 10},
            )
        )
        await self.command("search 测试")
        self.assertNotIn("已过滤", self.send.text_body)

    async def test_download_blocked_for_ai(self) -> None:
        fake_jmcomic.FakeAlbum.DEFAULT_TAGS = ["AI上色"]
        try:
            self.plugin.set_plugin_config(
                self._config_with(filter={"enabled": True, "scope": "all"})
            )
            await self.command("350234")
            await wait_jobs(self.plugin)
            self.assertIn("是 AI 作品", self.send.text_body)
            # 被拦截，不应产生下载任务
            self.assertFalse(self.plugin._jobs)
        finally:
            fake_jmcomic.FakeAlbum.DEFAULT_TAGS = ["测试", "全彩"]

    async def test_operator_bypass(self) -> None:
        fake_jmcomic.FakeAlbum.DEFAULT_TAGS = ["AI上色"]
        try:
            self.plugin.set_plugin_config(
                self._config_with(
                    filter={"enabled": True, "scope": "all", "operator_bypass": True}
                )
            )
            await self.plugin.handle_jm(
                text="/jm 350234",
                stream_id="s1",
                group_id="g1",
                user_id="u1",
                platform="qq",
                is_local_operator=True,
                matched_groups={"args": "350234"},
            )
            await wait_jobs(self.plugin)
            self.assertNotIn("是 AI 作品", self.send.text_body)
            self.assertTrue(self.send.forwards)
        finally:
            fake_jmcomic.FakeAlbum.DEFAULT_TAGS = ["测试", "全彩"]

    async def test_info_marks_ai(self) -> None:
        fake_jmcomic.FakeAlbum.DEFAULT_TAGS = ["AI上色"]
        try:
            self.plugin.set_plugin_config(
                self._config_with(filter={"enabled": True, "scope": "search"})
            )
            await self.command("info 350234")
            self.assertIn("这是 AI 作品", self.send.text_body)
        finally:
            fake_jmcomic.FakeAlbum.DEFAULT_TAGS = ["测试", "全彩"]

    async def test_info_blocked_when_scope_all(self) -> None:
        fake_jmcomic.FakeAlbum.DEFAULT_TAGS = ["AI上色"]
        try:
            self.plugin.set_plugin_config(
                self._config_with(filter={"enabled": True, "scope": "all"})
            )
            await self.command("info 350234")
            body = self.send.text_body
            self.assertIn("是 AI 作品", body)
            self.assertNotIn("类型：整本", body)
        finally:
            fake_jmcomic.FakeAlbum.DEFAULT_TAGS = ["测试", "全彩"]

    def _config_with(self, **sections: dict[str, Any]) -> dict[str, Any]:
        config = self.plugin.get_default_config()
        config["limits"]["cooldown_seconds"] = 0
        for section, values in sections.items():
            config[section].update(values)
        return config


class DownloadTests(PluginTestCase):
    """完整下载 -> 发送链路。"""

    async def test_download_album_forward(self) -> None:
        await self.command("350234")
        await wait_jobs(self.plugin)

        self.assertTrue(self.send.forwards, "应该用合并转发发送图片")
        images = self.images_sent()
        self.assertEqual(len(images), 6)
        self.assertEqual(images[0], IMAGE_BYTES)
        self.assertIn("✅ JM350234", self.send.text_body)
        self.assertIn("发送 6 张", self.send.text_body)

    async def test_download_photo(self) -> None:
        await self.command("p123456")
        await wait_jobs(self.plugin)

        self.assertEqual(len(self.images_sent()), 3)
        job = next(iter(self.plugin._jobs.values()))
        self.assertEqual(job.ref.kind, "photo")
        self.assertEqual(job.ref.jm_id, "123456")

    async def test_plate_command(self) -> None:
        await self.plugin.handle_plate(
            stream_id="s1",
            group_id="g1",
            user_id="u1",
            platform="qq",
            matched_groups={"plate_id": "654321"},
        )
        await wait_jobs(self.plugin)
        self.assertIn("JM654321", self.send.text_body)

    async def test_image_mode(self) -> None:
        self.plugin.set_plugin_config(self._config_with(send={"mode": "image"}))
        await self.plugin.on_config_update("self", {}, "1")
        await self.command("350234")
        await wait_jobs(self.plugin)

        self.assertFalse(self.send.forwards)
        self.assertEqual(len(self.send.images), 6)

    async def test_all_images_in_one_forward(self) -> None:
        """默认配置下所有图片应合并成一条聊天记录，不再按 8 张切分。"""

        await self.command("350234")
        await wait_jobs(self.plugin)

        self.assertEqual(len(self.send.forwards), 1)
        self.assertEqual(sum(len(nodes) for _, nodes in self.send.forwards), 6)

    async def test_forward_retries_before_giving_up(self) -> None:
        """转发没回执时先重试，且不逐张补发（避免图片跑到聊天记录外）。"""

        self.send.fail_forward = True
        await self.command("350234")
        await wait_jobs(self.plugin)

        # forward_retry=2 → 共 3 次尝试
        self.assertEqual(self.send.forward_attempts, 3)
        self.assertEqual(self.send.images, [])
        self.assertIn("未返回回执", self.send.text_body)

    async def test_forward_fallback_disabled_by_default(self) -> None:
        """默认 forward_fallback=none：即使失败也不产生散图。"""

        self.send.fail_forward = True
        await self.command("350234")
        await wait_jobs(self.plugin)

        self.assertEqual(len(self.send.images), 0)

    async def test_forward_fallback_to_images_when_enabled(self) -> None:
        """显式打开兜底时才逐张补发。"""

        self.plugin.set_plugin_config(
            self._config_with(send={"forward_fallback": "image"})
        )
        self.send.fail_forward = True
        await self.command("350234")
        await wait_jobs(self.plugin)

        self.assertEqual(len(self.send.images), 6)

    async def test_partial_failure_still_sends(self) -> None:
        fake_jmcomic.PARTIAL = True
        await self.command("350234")
        await wait_jobs(self.plugin)

        self.assertTrue(self.send.forwards)
        self.assertIn("部分", self.send.text_body)

    async def test_max_images_caps_count(self) -> None:
        """显式设置上限时按上限截断。"""

        self.plugin.set_plugin_config(self._config_with(send={"max_images": 2}))
        await self.command("350234")
        await wait_jobs(self.plugin)

        self.assertEqual(len(self.images_sent()), 2)

    async def test_unlimited_sends_all_in_one_record(self) -> None:
        """默认 max_images=0：整部作品全部图片进一条聊天记录。"""

        await self.command("350234")
        await wait_jobs(self.plugin)

        self.assertEqual(len(self.send.forwards), 1)
        self.assertEqual(sum(len(nodes) for _, nodes in self.send.forwards), 6)
        self.assertEqual(self.send.images, [])
        self.assertIn("发送 6 张", self.send.text_body)

    async def test_delete_after_send(self) -> None:
        self.plugin.set_plugin_config(self._config_with(download={"delete_after_send": True}))
        await self.command("350234")
        await wait_jobs(self.plugin)

        job = next(iter(self.plugin._jobs.values()))
        self.assertTrue(job.terminal)
        self.assertFalse(Path(job.directory).exists())

    async def test_none_mode_keeps_files(self) -> None:
        self.plugin.set_plugin_config(
            self._config_with(send={"mode": "none"}, download={"delete_after_send": True})
        )
        await self.command("350234")
        await wait_jobs(self.plugin)

        job = next(iter(self.plugin._jobs.values()))
        self.assertTrue(Path(job.directory).exists())
        self.assertIn("仅下载", self.send.text_body)

    async def test_status_after_download(self) -> None:
        await self.command("350234")
        await wait_jobs(self.plugin)
        await self.command("status")

        self.assertIn("完成", self.send.text_body)
        self.assertIn("JM350234", self.send.text_body)

    async def test_cancel_running_job(self) -> None:
        fake_jmcomic.DELAY = 0.2
        await self.command("350234")
        await asyncio.sleep(0.05)
        await self.plugin._cancel_command("all", "s1", "u1", "g1")
        job = next(iter(self.plugin._jobs.values()))
        self.assertTrue(job.terminal)
        self.assertEqual(job.state, "cancelled")
        await wait_jobs(self.plugin)
        self.assertIn("已取消", self.send.text_body)
        # 取消后配额应退回，不能重复扣
        self.assertEqual(self.plugin._quota.remaining("g:g1"), self.plugin.config.limits.daily_per_chat)

    def _config_with(self, **sections: dict[str, Any]) -> dict[str, Any]:
        config = self.plugin.get_default_config()
        config["limits"]["cooldown_seconds"] = 0
        for section, values in sections.items():
            config[section].update(values)
        return config


class ConfigSchemaTests(unittest.TestCase):
    """配置 Schema 必须带中文 label / hint / description，否则 WebUI 里就是裸字段名。"""

    def test_every_field_has_label_and_hint(self) -> None:
        import plugin as plugin_module

        p = plugin_module.create_plugin()
        schema = p.get_webui_config_schema(plugin_id="com.deepseek.jmcomic")
        sections = schema.get("sections", {})

        self.assertTrue(sections, "Schema 不应为空")
        checked = 0
        for sec_name, sec in sections.items():
            fields = sec.get("fields") or sec.get("properties") or {}
            self.assertTrue(fields, f"配置节 {sec_name} 没有字段")
            for fname, meta in fields.items():
                with self.subTest(field=f"{sec_name}.{fname}"):
                    label = str(meta.get("label") or "").strip()
                    self.assertTrue(label, "缺少 label")
                    self.assertNotEqual(label, fname, "label 不能退化成字段名")
                    hint = str(meta.get("hint") or "").strip()
                    self.assertTrue(hint, "缺少 hint")
                    desc = str(meta.get("description") or "").strip()
                    self.assertTrue(desc, "缺少 description")
                    checked += 1

        # 覆盖全部配置节（tools 为审核后新增的 LLM 工具开关）
        self.assertEqual(
            set(sections),
            {"plugin", "download", "send", "limits", "search", "tools", "filter"},
        )
        self.assertGreaterEqual(checked, 27)

    def test_numeric_fields_have_step(self) -> None:
        import plugin as plugin_module

        p = plugin_module.create_plugin()
        schema = p.get_webui_config_schema(plugin_id="com.deepseek.jmcomic")
        sections = schema.get("sections", {})

        for sec_name, sec in sections.items():
            fields = sec.get("fields") or sec.get("properties") or {}
            for fname, meta in fields.items():
                if meta.get("type") not in {"number", "integer", "float"}:
                    continue
                with self.subTest(field=f"{sec_name}.{fname}"):
                    self.assertIsNotNone(meta.get("step"), "数字字段应有 step")

    def test_literal_fields_have_choices(self) -> None:
        import plugin as plugin_module

        p = plugin_module.create_plugin()
        schema = p.get_webui_config_schema(plugin_id="com.deepseek.jmcomic")
        sections = schema.get("sections", {})

        for fname in ("mode", "forward_fallback", "client_impl", "scope"):
            for sec in sections.values():
                fields = sec.get("fields") or sec.get("properties") or {}
                if fname in fields:
                    with self.subTest(field=fname):
                        self.assertTrue(fields[fname].get("choices"), "枚举字段应有 choices")


class LimitTests(PluginTestCase):
    """停用、白名单、冷却、配额。"""

    async def test_disabled(self) -> None:
        self.plugin.set_plugin_config(self._config_with(plugin={"enabled": False}))
        await self.command("350234")
        self.assertIn("已停用", self.send.text_body)

    async def test_allowed_chats(self) -> None:
        self.plugin.set_plugin_config(self._config_with(limits={"allowed_chats": "999"}))
        await self.command("350234")
        self.assertIn("没有使用", self.send.text_body)

    async def test_allowed_chats_match(self) -> None:
        self.plugin.set_plugin_config(self._config_with(limits={"allowed_chats": "g1"}))
        await self.command("350234")
        await wait_jobs(self.plugin)
        self.assertIn("✅", self.send.text_body)

    async def test_blocked_chat_rejects_download(self) -> None:
        """黑名单命中：下载被拒绝，且不建立任务。"""

        self.plugin.set_plugin_config(self._config_with(limits={"blocked_chats": "g1"}))
        await self.command("350234")
        await wait_jobs(self.plugin)
        self.assertIn("已被拉黑", self.send.text_body)
        self.assertFalse(self.plugin._jobs)

    async def test_blocked_by_user_id(self) -> None:
        """黑名单里填 QQ 号也要生效（不只是群号）。"""

        self.plugin.set_plugin_config(self._config_with(limits={"blocked_chats": "u1"}))
        await self.command("350234")
        await wait_jobs(self.plugin)
        self.assertIn("已被拉黑", self.send.text_body)

    async def test_blocked_rejects_all_subcommands(self) -> None:
        """黑名单对 info / search / status 同样生效。"""

        self.plugin.set_plugin_config(self._config_with(limits={"blocked_chats": "g1"}))
        for args in ("info 350234", "search 测试", "status", "help"):
            with self.subTest(args=args):
                self.send.texts.clear()
                await self.command(args)
                self.assertIn("已被拉黑", self.send.text_body)

    async def test_blocked_overrides_whitelist(self) -> None:
        """黑名单优先级高于白名单：两边都填，仍然拒绝。"""

        self.plugin.set_plugin_config(
            self._config_with(limits={"allowed_chats": "g1", "blocked_chats": "g1"})
        )
        await self.command("350234")
        await wait_jobs(self.plugin)
        self.assertIn("已被拉黑", self.send.text_body)

    async def test_blocked_other_chat_unaffected(self) -> None:
        """黑名单只影响命中的会话，其他会话照常可用。"""

        self.plugin.set_plugin_config(self._config_with(limits={"blocked_chats": "other-group"}))
        await self.command("350234")
        await wait_jobs(self.plugin)
        self.assertIn("✅", self.send.text_body)

    async def test_blocked_tool_search_rejected(self) -> None:
        """LLM 工具同样受黑名单约束。"""

        self.plugin.set_plugin_config(self._config_with(limits={"blocked_chats": "g1"}))
        result = await self.plugin.tool_search("测试", 1, group_id="g1", user_id="u1")
        self.assertFalse(result["success"])
        self.assertIn("已被拉黑", result["content"])

    async def test_whitelist_message_differs_from_blacklist(self) -> None:
        """未命中白名单与命中黑名单的提示要能区分开。"""

        self.plugin.set_plugin_config(self._config_with(limits={"allowed_chats": "999"}))
        await self.command("350234")
        self.assertIn("不在白名单", self.send.text_body)
        self.assertNotIn("已被拉黑", self.send.text_body)

    async def test_cooldown(self) -> None:
        self.plugin.set_plugin_config(self._config_with(limits={"cooldown_seconds": 999}))
        await self.command("350234")
        await wait_jobs(self.plugin)
        await self.command("654321")
        self.assertIn("太快", self.send.text_body)

    async def test_daily_quota(self) -> None:
        self.plugin.set_plugin_config(self._config_with(limits={"daily_per_chat": 1}))
        self.plugin._quota.limit = 1
        await self.command("350234")
        await wait_jobs(self.plugin)
        await self.command("654321")
        self.assertIn("次数用完", self.send.text_body)

    async def test_duplicate_job(self) -> None:
        fake_jmcomic.DELAY = 0.2
        await self.command("350234")
        await self.command("350234")
        self.assertIn("已经在任务", self.send.text_body)
        await wait_jobs(self.plugin)

    def _config_with(self, **sections: dict[str, Any]) -> dict[str, Any]:
        config = self.plugin.get_default_config()
        config["limits"]["cooldown_seconds"] = 0
        for section, values in sections.items():
            config[section].update(values)
        return config


class ToolTests(PluginTestCase):
    """给 LLM 的工具。"""

    async def test_search_tool(self) -> None:
        result = await self.plugin.tool_search("测试", 1)
        self.assertTrue(result["success"])
        self.assertIn("JM500001", result["content"])

    async def test_search_tool_paging(self) -> None:
        """工具调用翻页同样要生效。"""

        page1 = await self.plugin.tool_search("测试", 1)
        page2 = await self.plugin.tool_search("测试", 2)
        self.assertIn("JM500001", page1["content"])
        self.assertIn("JM500011", page2["content"])

    async def test_search_tool_empty(self) -> None:
        result = await self.plugin.tool_search("")
        self.assertFalse(result["success"])

    async def test_download_tool_disabled_by_default(self) -> None:
        """审核要求：AI 下载工具默认关闭，不得由模型自行触发。"""

        result = await self.plugin.tool_download(
            "350234", "album", stream_id="s1", user_id="u1", group_id="g1"
        )
        self.assertFalse(result["success"])
        self.assertIn("默认关闭", result["content"])
        # 不应产生任何下载任务
        self.assertFalse(self.plugin._jobs)

    async def test_download_tool_when_enabled(self) -> None:
        """管理员显式开启后，工具才允许入队。"""

        config = self.plugin.get_default_config()
        config["limits"]["cooldown_seconds"] = 0
        config["tools"]["allow_download"] = True
        self.plugin.set_plugin_config(config)

        result = await self.plugin.tool_download(
            "350234", "album", stream_id="s1", user_id="u1", group_id="g1"
        )
        self.assertTrue(result["success"])
        self.assertIn("任务已创建", result["content"])
        await wait_jobs(self.plugin)
        self.assertTrue(self.send.forwards)

    async def test_download_tool_bad_id(self) -> None:
        config = self.plugin.get_default_config()
        config["limits"]["cooldown_seconds"] = 0
        config["tools"]["allow_download"] = True
        self.plugin.set_plugin_config(config)

        result = await self.plugin.tool_download("abc", "album", stream_id="s1")
        self.assertFalse(result["success"])
        self.assertIn("纯数字", result["content"])


class SecurityHardeningTests(PluginTestCase):
    """审核提出的工程加固项。"""

    def test_base_dir_rejects_absolute_path(self) -> None:
        """审核意见：base_dir 曾接受任意绝对路径，配合发送后删除会污染外部目录。"""

        config = self.plugin.get_default_config()
        config["limits"]["cooldown_seconds"] = 0
        config["download"]["base_dir"] = "C:/Windows/Temp/jm-should-not-use"
        self.plugin.set_plugin_config(config)

        root = self.plugin._resolve_download_root()
        data_dir = Path(self.plugin.ctx.paths.data_dir).resolve()
        self.assertTrue(root.is_relative_to(data_dir), f"{root} 不应越出 {data_dir}")
        self.assertEqual(root, data_dir / "downloads")

    def test_base_dir_rejects_parent_escape(self) -> None:
        config = self.plugin.get_default_config()
        config["limits"]["cooldown_seconds"] = 0
        config["download"]["base_dir"] = "../../etc"
        self.plugin.set_plugin_config(config)

        root = self.plugin._resolve_download_root()
        data_dir = Path(self.plugin.ctx.paths.data_dir).resolve()
        self.assertTrue(root.is_relative_to(data_dir))

    def test_base_dir_accepts_relative(self) -> None:
        config = self.plugin.get_default_config()
        config["limits"]["cooldown_seconds"] = 0
        config["download"]["base_dir"] = "comics/sub"
        self.plugin.set_plugin_config(config)

        root = self.plugin._resolve_download_root()
        data_dir = Path(self.plugin.ctx.paths.data_dir).resolve()
        self.assertEqual(root, (data_dir / "comics" / "sub").resolve())

    def test_base_dir_empty_uses_default(self) -> None:
        root = self.plugin._resolve_download_root()
        data_dir = Path(self.plugin.ctx.paths.data_dir).resolve()
        self.assertEqual(root, data_dir / "downloads")

    async def test_cover_cache_is_trimmed(self) -> None:
        """审核意见：封面缓存只增不清会吃磁盘，需按上限淘汰。"""

        from jm_service import JmService

        covers = Path(self.plugin.ctx.paths.data_dir) / "covers"
        covers.mkdir(parents=True, exist_ok=True)
        # 造 12 个文件，上限压到 10
        for index in range(12):
            (covers / f"90000{index}_3x4.jpg").write_bytes(b"x")

        removed = JmService._trim_cover_cache(covers, limit=10)
        remaining = list(covers.glob("*.jpg"))
        self.assertGreater(removed, 0)
        self.assertLessEqual(len(remaining), 10)

    async def test_cover_cache_noop_below_limit(self) -> None:
        from jm_service import JmService

        covers = Path(self.plugin.ctx.paths.data_dir) / "covers2"
        covers.mkdir(parents=True, exist_ok=True)
        for index in range(3):
            (covers / f"91000{index}_3x4.jpg").write_bytes(b"x")

        removed = JmService._trim_cover_cache(covers, limit=10)
        self.assertEqual(removed, 0)
        self.assertEqual(len(list(covers.glob("*.jpg"))), 3)

    async def test_download_tool_off_by_default_in_config(self) -> None:
        """默认配置里 AI 下载就应该是关的。"""

        defaults = self.plugin.get_default_config()
        self.assertFalse(defaults["tools"]["allow_download"])


class MissingJmcomicTests(unittest.IsolatedAsyncioTestCase):
    """没装 jmcomic 时也要能加载并给出人话提示。"""

    def test_plugin_imports_without_jmcomic(self) -> None:
        saved = sys.modules.pop("jmcomic", None)
        try:
            instance = JmComicPlugin()
            self.assertIsNotNone(instance.get_default_config())
        finally:
            if saved is not None:
                sys.modules["jmcomic"] = saved

    async def test_command_reports_missing_dependency(self) -> None:
        base = make_temp_dir("nodep-")
        plugin = build_plugin(base)
        try:
            with patch.object(
                jm_service_module.importlib,
                "import_module",
                side_effect=ImportError("模拟 jmcomic 缺失"),
            ):
                await plugin.on_load()
            await plugin.handle_jm(
                text="/jm 350234",
                stream_id="s1",
                group_id="g1",
                user_id="u1",
                platform="qq",
                matched_groups={"args": "350234"},
            )
            self.assertIn("jmcomic 不可用", plugin.ctx.send.text_body)
        finally:
            await plugin.on_unload()
            remove_temp_dir(base)


if __name__ == "__main__":
    unittest.main()



"""纯逻辑层单测：车号解析、配额、任务状态、文件排序。"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from jm_core import (  # noqa: E402
    STATE_CANCELLED,
    STATE_DONE,
    STATE_FAILED,
    STATE_RUNNING,
    Job,
    JmRef,
    QuotaBook,
    chunked,
    collect_images,
    detect_ai_reason,
    human_duration,
    human_size,
    match_tags,
    natural_key,
    normalize_tag,
    parse_jm_ref,
    parse_jm_refs,
    parse_tag_query,
    short,
)


class ParseRefTests(unittest.TestCase):
    """车号解析。"""

    def test_plain_digits(self) -> None:
        ref = parse_jm_ref("看看 350234 这本")
        self.assertIsNotNone(ref)
        assert ref is not None
        self.assertEqual(ref.jm_id, "350234")
        self.assertEqual(ref.kind, "album")

    def test_prefixed_forms(self) -> None:
        for text in ("JM123456", "jm-123456", "jm_123456", "禁漫 123456", "车号：123456"):
            with self.subTest(text=text):
                ref = parse_jm_ref(text)
                assert ref is not None
                self.assertEqual(ref.jm_id, "123456")

    def test_urls(self) -> None:
        album = parse_jm_ref("https://18comic.vip/album/350234/")
        assert album is not None
        self.assertEqual((album.jm_id, album.kind), ("350234", "album"))

        photo = parse_jm_ref("https://18comic.vip/photo/123456")
        assert photo is not None
        self.assertEqual((photo.jm_id, photo.kind), ("123456", "photo"))

    def test_photo_tag(self) -> None:
        ref = parse_jm_ref("p123456 单章")
        assert ref is not None
        self.assertEqual((ref.jm_id, ref.kind), ("123456", "photo"))

        ref = parse_jm_ref("photo-654321")
        assert ref is not None
        self.assertEqual(ref.kind, "photo")

    def test_multiple_and_dedupe(self) -> None:
        refs = parse_jm_refs("JM350234 和 350234，还有 1234567")
        self.assertEqual([item.jm_id for item in refs], ["350234", "1234567"])

    def test_ignores_year_and_long_numbers(self) -> None:
        self.assertEqual(parse_jm_refs("2024 年 1234567890"), [])
        self.assertEqual(parse_jm_refs("QQ 123456789"), [])

    def test_limit(self) -> None:
        refs = parse_jm_refs("12345 23456 34567", limit=2)
        self.assertEqual(len(refs), 2)

    def test_label(self) -> None:
        self.assertEqual(JmRef("123456").label, "JM123456")
        self.assertIn("单章", JmRef("123456", kind="photo").label)


class HelperTests(unittest.TestCase):
    """小工具。"""

    def test_chunked(self) -> None:
        self.assertEqual(list(chunked([1, 2, 3, 4, 5], 2)), [[1, 2], [3, 4], [5]])
        self.assertEqual(list(chunked([], 3)), [])

    def test_short(self) -> None:
        self.assertEqual(short("  a \n b  "), "a b")
        self.assertTrue(short("x" * 100, 10).endswith("…"))

    def test_human_size(self) -> None:
        self.assertEqual(human_size(0), "0B")
        self.assertEqual(human_size(1024), "1.0KB")

    def test_human_duration(self) -> None:
        self.assertEqual(human_duration(42), "42 秒")
        self.assertEqual(human_duration(125), "2 分 5 秒")

    def test_natural_key(self) -> None:
        names = ["10.jpg", "2.jpg", "1.jpg"]
        self.assertEqual(sorted(names, key=natural_key), ["1.jpg", "2.jpg", "10.jpg"])


class QuotaTests(unittest.TestCase):
    """每日配额。"""

    def test_consume_and_refund(self) -> None:
        quota = QuotaBook(limit=2)
        self.assertTrue(quota.consume("g:1"))
        self.assertTrue(quota.consume("g:1"))
        self.assertFalse(quota.consume("g:1"))
        self.assertEqual(quota.remaining("g:1"), 0)
        quota.refund("g:1")
        self.assertEqual(quota.remaining("g:1"), 1)

    def test_unlimited(self) -> None:
        quota = QuotaBook(limit=0)
        for _ in range(100):
            self.assertTrue(quota.consume("g:1"))

    def test_roll_on_new_day(self) -> None:
        quota = QuotaBook(limit=1, day="2000-01-01", used={"g:1": 1})
        self.assertEqual(quota.remaining("g:1"), 1)

    def test_serialization(self) -> None:
        quota = QuotaBook(limit=3)
        quota.consume("g:1")
        restored = QuotaBook.from_dict(quota.to_dict(), limit=3)
        self.assertEqual(restored.remaining("g:1"), 2)

    def test_bad_data(self) -> None:
        restored = QuotaBook.from_dict({"day": "x", "used": {"g:1": "bad", "g:2": -1}}, limit=2)
        self.assertEqual(restored.used, {})


class JobTests(unittest.TestCase):
    """任务状态机。"""

    def _job(self) -> Job:
        return Job(ref=JmRef("350234"), stream_id="s1", user_id="u1", group_id="g1")

    def test_lifecycle(self) -> None:
        job = self._job()
        self.assertFalse(job.terminal)
        job.mark_running()
        self.assertEqual(job.state, STATE_RUNNING)
        self.assertGreater(job.started_at, 0)

        job.done_images = 3
        job.sent_images = 3
        job.mark_done()
        self.assertEqual(job.state, STATE_DONE)
        self.assertTrue(job.terminal)
        self.assertIn("完成", job.to_line())

    def test_failed_and_cancelled(self) -> None:
        job = self._job()
        job.mark_failed("网络错误")
        self.assertEqual(job.state, STATE_FAILED)
        self.assertIn("网络错误", job.to_line())

        job = self._job()
        job.mark_cancelled("用户取消")
        self.assertEqual(job.state, STATE_CANCELLED)

    def test_display_title(self) -> None:
        job = self._job()
        job.title = "很长很长的标题" * 5
        self.assertTrue(job.display_title.startswith("JM350234《"))


class AiDetectTests(unittest.TestCase):
    """AI 作品识别。"""

    def test_detects_ai_tag(self) -> None:
        # 真实禁漫 tag 里见过 AI上色
        reason = detect_ai_reason(tags=["多毛", "熟女", "AI上色", "中文"])
        self.assertEqual(reason, "标签「AI上色」")

    def test_detects_plain_ai_tag(self) -> None:
        self.assertEqual(detect_ai_reason(tags=["AI"]), "标签「AI」")

    def test_detects_ai_generated_tag(self) -> None:
        self.assertIn("AI生成", detect_ai_reason(tags=["AI生成"]))

    def test_detects_from_title(self) -> None:
        self.assertEqual(detect_ai_reason(title="某作品 [AI] 重制"), "标题含 AI 标记")

    def test_detects_from_description(self) -> None:
        self.assertEqual(detect_ai_reason(description="本作由 AI 绘制"), "描述含 AI 标记")

    def test_no_false_positive(self) -> None:
        self.assertEqual(detect_ai_reason(tags=["熟女", "巨乳", "中文"], title="普通作品"), "")

    def test_ignores_raid_and_aid(self) -> None:
        self.assertEqual(detect_ai_reason(tags=["Raid"]), "")
        self.assertEqual(detect_ai_reason(tags=["aid"]), "")

    def test_empty_input(self) -> None:
        self.assertEqual(detect_ai_reason(), "")


class TagMatchingTests(unittest.TestCase):
    """标签解析与匹配。"""

    def test_parse_single(self) -> None:
        self.assertEqual(parse_tag_query("巨乳"), ["巨乳"])

    def test_parse_multiple_separators(self) -> None:
        for raw in ("巨乳、NTR", "巨乳,NTR", "巨乳，NTR", "巨乳 NTR"):
            with self.subTest(raw=raw):
                self.assertEqual(parse_tag_query(raw), ["巨乳", "ntr"])

    def test_parse_empty(self) -> None:
        self.assertEqual(parse_tag_query("   "), [])
        self.assertEqual(parse_tag_query("、、"), [])

    def test_normalize_fullwidth(self) -> None:
        # 全角字母应归一到半角
        self.assertEqual(normalize_tag("ＡＩ"), normalize_tag("AI"))

    def test_match_all_required(self) -> None:
        ok, matched = match_tags(["巨乳", "ntr"], ["巨乳", "NTR", "中文"])
        self.assertTrue(ok)
        self.assertEqual(matched, ("巨乳", "ntr"))

    def test_match_missing_one(self) -> None:
        ok, matched = match_tags(["巨乳", "ntr"], ["巨乳", "中文"])
        self.assertFalse(ok)
        self.assertEqual(matched, ("巨乳",))

    def test_match_case_insensitive(self) -> None:
        ok, _ = match_tags(["ntr"], ["NTR"])
        self.assertTrue(ok)

    def test_match_substring(self) -> None:
        # 「汉化」应命中「中文汉化」
        ok, _ = match_tags(["汉化"], ["中文汉化"])
        self.assertTrue(ok)

    def test_match_no_reverse_substring(self) -> None:
        # 反过来不成立：要求「中文汉化」不该命中只有「中文」的作品
        ok, _ = match_tags(["中文汉化"], ["中文"])
        self.assertFalse(ok)

    def test_match_empty_wanted(self) -> None:
        ok, matched = match_tags([], ["巨乳"])
        self.assertFalse(ok)
        self.assertEqual(matched, ())


class CollectImagesTests(unittest.TestCase):
    """文件收集与排序。"""

    def test_collect_sorted(self) -> None:
        from tmp_utils import make_temp_dir, remove_temp_dir

        tmp = make_temp_dir("core-")
        try:
            root = tmp
            chapter2 = root / "第2话"
            chapter1 = root / "第1话"
            chapter2.mkdir()
            chapter1.mkdir()
            (chapter2 / "00001.jpg").write_bytes(b"x")
            (chapter1 / "00010.jpg").write_bytes(b"x")
            (chapter1 / "00002.webp").write_bytes(b"x")
            (chapter1 / "note.txt").write_text("skip", encoding="utf-8")

            files = collect_images(root)
            self.assertEqual(
                [Path(item).name for item in files],
                ["00002.webp", "00010.jpg", "00001.jpg"],
            )
        finally:
            remove_temp_dir(tmp)

    def test_missing_dir(self) -> None:
        self.assertEqual(collect_images(Path("definitely-not-here")), [])


if __name__ == "__main__":
    unittest.main()

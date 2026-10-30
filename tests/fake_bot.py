"""测试用的假 MaiBot 上下文，以及构造插件的辅助函数。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import asyncio
import logging
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
TESTS_DIR = Path(__file__).resolve().parent
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))

from maibot_sdk.context import PluginPaths  # noqa: E402

import plugin as plugin_module  # noqa: E402


class FakeSend:
    """记录所有发送调用。"""

    def __init__(self) -> None:
        self.texts: list[tuple[str, str]] = []
        self.images: list[tuple[str, str]] = []
        self.forwards: list[tuple[str, list[dict[str, Any]]]] = []
        self.fail_forward = False
        self.fail_image = False
        #: 记录 forward 的实际调用次数，用于验证重试
        self.forward_attempts = 0

    async def text(self, text: str, stream_id: str, **kwargs: Any) -> bool:
        del kwargs
        self.texts.append((stream_id, text))
        return True

    async def image(self, image_data: str, stream_id: str, **kwargs: Any) -> bool:
        del kwargs
        if self.fail_image:
            return False
        self.images.append((stream_id, image_data))
        return True

    async def forward(self, messages: list[dict[str, Any]], stream_id: str, **kwargs: Any) -> bool:
        del kwargs
        self.forward_attempts += 1
        if self.fail_forward:
            return False
        self.forwards.append((stream_id, messages))
        return True

    # -- 断言辅助 ---------------------------------------------------------
    @property
    def forward_text(self) -> str:
        """合并转发里的文本（搜索结果就是走这条路的）。"""

        lines: list[str] = []
        for _stream_id, nodes in self.forwards:
            for node in nodes:
                for segment in node.get("segments") or ():
                    if segment.get("type") == "text":
                        lines.append(str(segment.get("content") or ""))
        return "\n".join(line for line in lines if line)

    @property
    def text_body(self) -> str:
        """普通文本 + 合并转发里的文本，方便断言「用户能看到什么」。"""

        parts = [text for _, text in self.texts]
        forwarded = self.forward_text
        if forwarded:
            parts.append(forwarded)
        return "\n".join(parts)

    def image_count(self) -> int:
        """实际发出去的图片张数（只数 image 段，文本聊天记录不算）。"""

        count = len(self.images)
        for _stream_id, nodes in self.forwards:
            for node in nodes:
                for segment in node.get("segments") or ():
                    if segment.get("type") == "image":
                        count += 1
        return count


class FakeCtx:
    """只带插件会用到的字段。"""

    def __init__(self, base_dir: Path) -> None:
        self.plugin_id = plugin_module.PLUGIN_ID
        self.paths = PluginPaths(
            data_dir=base_dir / "data",
            runtime_dir=base_dir / "temp",
        )
        self.send = FakeSend()
        self.logger = logging.getLogger("test.jmcomic")


def _deep_merge(target: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(target.get(key), dict):
            _deep_merge(target[key], value)
        else:
            target[key] = value
    return target


def build_plugin(base_dir: Path, patch: dict[str, Any] | None = None) -> plugin_module.JmComicPlugin:
    """创建插件实例，默认关掉冷却方便测试。"""

    instance = plugin_module.create_plugin()
    config = instance.get_default_config()
    config["limits"]["cooldown_seconds"] = 0
    if patch:
        _deep_merge(config, patch)
    instance.set_plugin_config(config)
    instance._set_context(FakeCtx(base_dir))
    return instance


async def wait_jobs(instance: plugin_module.JmComicPlugin, timeout: float = 10.0) -> None:
    """等待插件内所有后台任务结束。"""

    deadline = asyncio.get_running_loop().time() + timeout
    while instance._active_jobs():
        if asyncio.get_running_loop().time() > deadline:
            raise TimeoutError("等待下载任务超时")
        await asyncio.sleep(0.01)

    pending = [task for task in instance._tasks if not task.done()]
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)

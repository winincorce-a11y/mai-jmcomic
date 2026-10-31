"""MaiBot 插件：JMComic 下载器。

把 ``JMComic-Crawler-Python``（jmcomic）的「按车号下载本子」能力接到
MaiBot 的群聊里：

- ``/jm 350234`` 下载整本，按下发的配置用合并转发或逐张发送；
- ``/jm p123456`` 只下载一个章节；
- ``/jm info 350234`` / ``/jm search 关键词`` 查详情、搜索；
- ``/jm status`` / ``/jm cancel`` 管理后台任务；
- 直接发送 ``JM123456`` 也能触发下载。

插件只通过 ``self.ctx`` 使用 MaiBot 能力，不导入 ``src.*``；jmcomic 的
同步下载全部丢进线程池，命令本身立即返回，避免卡住 Runner 的事件循环。
"""

from __future__ import annotations

import asyncio
import base64
import json
import re
import shutil
import time
from contextlib import suppress
from pathlib import Path
from typing import Any, Literal

from maibot_sdk import Command, CONFIG_RELOAD_SCOPE_SELF, Field, HomeCard, MaiBotPlugin, PluginConfigBase, Tool
from maibot_sdk.types import ToolParameterInfo, ToolParamType

try:  # 作为插件包被 Runner 加载
    from .jm_core import (
        STATE_DONE,
        Job,
        QuotaBook,
        JmRef,
        count_images,
        detect_ai_reason,
        format_job_line,
        human_duration,
        is_relative_to,
        parse_jm_ref,
        parse_tag_query,
        short,
    )
    from .jm_service import (
        DownloadSettings,
        JmCancelled,
        JmError,
        JmService,
        JmUnavailable,
        SearchResult,
        TagSearchResult,
        describe_media,
    )
except ImportError:  # 单测 / 手动 import 时按顶层模块处理
    from jm_core import (  # type: ignore[no-redef]
        STATE_DONE,
        Job,
        QuotaBook,
        JmRef,
        count_images,
        detect_ai_reason,
        format_job_line,
        human_duration,
        is_relative_to,
        parse_jm_ref,
        parse_tag_query,
        short,
    )
    from jm_service import (  # type: ignore[no-redef]
        DownloadSettings,
        JmCancelled,
        JmError,
        JmService,
        JmUnavailable,
        SearchResult,
        TagSearchResult,
        describe_media,
    )

__all__ = [
    "DownloadSection",
    "JmComicConfig",
    "JmComicPlugin",
    "LimitSection",
    "PluginSection",
    "SearchSection",
    "SendSection",
    "create_plugin",
]

PLUGIN_ID = "github.winincorce-a11y.mai-jmcomic"

#: 主命令：``/jm``、``/jmcomic``、``/禁漫``，后面可跟任意参数。
_COMMAND_PATTERN = r"(?i)^\s*[/／]\s*(?:jmcomic|jm|禁漫)(?:\s+(?P<args>.*))?$"

#: 直接甩车号：``JM123456`` / ``禁漫-123456``。
_PLATE_PATTERN = r"(?i)^\s*(?:jmcomic|jm|禁漫|车号|車號)\s*[-_:#：]?\s*(?P<plate_id>\d{4,8})\s*$"

_HELP_TEXT = (
    "📚 JMComic 下载\n"
    "· /jm 350234 — 下载整本并发送\n"
    "· /jm p123456 — 只下载单章\n"
    "· /jm info 350234 — 查看详情\n"
    "· /jm search 关键词 [页码] — 站内搜索\n"
    "· /jm series 作品 [页码] — 按作品系列搜索（如 蔚蓝档案）\n"
    "· /jm author 作者 [页码] — 按作者/画师搜索\n"
    "· /jm actor 角色 [页码] — 按登场人物搜索\n"
    "· /jm tag 标签1、标签2 [页码] — 按标签搜索\n"
    "· /jm status — 查看任务\n"
    "· /jm cancel 任务号|all — 取消任务\n"
    "也可以直接发送 JM123456 触发下载。"
)

_SEARCH_WORDS = frozenset({"search", "s", "搜", "搜索", "查", "找"})
_SERIES_WORDS = frozenset({"series", "work", "works", "系列", "作品", "原作"})
_AUTHOR_WORDS = frozenset({"author", "artists", "artist", "作者", "画师", "畫師", "绘师"})
_ACTOR_WORDS = frozenset({"actor", "character", "char", "登场人物", "人物", "角色", "登場人物"})

#: 各搜索模式的展示文案与对应命令。key 与 :meth:`JmService.search` 的 ``mode`` 一致。
_SEARCH_MODES: dict[str, tuple[str, str]] = {
    "site": ("search", "关键词"),
    "work": ("series", "作品系列"),
    "author": ("author", "作者"),
    "actor": ("actor", "登场人物"),
    "tag": ("tag", "标签"),
}
_TAG_WORDS = frozenset({"tag", "t", "标签", "標籤", "tagsearch"})
_INFO_WORDS = frozenset({"info", "i", "详情", "信息"})
_STATUS_WORDS = frozenset({"status", "st", "状态", "任务", "队列"})
_CANCEL_WORDS = frozenset({"cancel", "c", "取消", "停止"})
_DOWNLOAD_WORDS = frozenset({"download", "dl", "下载", "get"})
_HELP_WORDS = frozenset({"help", "h", "?", "？", "帮助", "-h", "--help"})

_IMAGE_SUFFIXES = frozenset({".jpg", ".jpeg", ".png", ".webp", ".gif"})
_MAX_JOBS_KEPT = 60
_PROGRESS_INTERVAL = 1.5

#: 单条合并转发允许的 base64 总量。Runner 的 RPC 单帧上限是 16MB
#: （见 src/plugin_runtime/transport/base.py 的 MAX_FRAME_SIZE），这里按 12MB
#: 控制以预留 msgpack 包装与字段开销；实测 50 张 JPG 约 7.6MB 可一条放下。
_FRAME_BUDGET = 12 * 1024 * 1024

#: 单张图片原始字节上限；再大 base64 后可能顶到 RPC 帧上限。
_MAX_IMAGE_BYTES = 8 * 1024 * 1024

#: AI 判定缓存的条目上限，防止无限增长。
_AI_CACHE_MAX = 500

#: 标签搜索单次等待上限（秒）。候选越多越慢，实际预算按候选数动态算，
#: 这里只是兜底天花板。
_TAG_MAX_WAIT = 900

#: 所有搜索结果（站内/系列/作者/人物/标签）合并转发时，聊天记录里显示的发送者名字。
_SEARCH_RECORD_NAME = "JM 搜索"

#: 下载搜索结果封面的总等待上限（秒）。封面是锦上添花，到点就只发文字。
_COVER_TIMEOUT = 60.0

#: 重编码后单张图片的字节上限。重编码只会变小，超过它说明原文件本身
#: 过大，重编码没有意义——调用方（``_encode_image``）已先行拦截。
_REENCODE_MAX_INPUT_BYTES = 8 * 1024 * 1024

#: 重编码认得动的格式。动图（GIF）与动图 WebP 重编码会丢动画，直接跳过。
_REENCODE_STATIC_FORMATS = frozenset({"JPEG", "PNG", "WEBP"})


def _reencode_image(data: bytes, path: str, quality: int) -> bytes | None:
    """对图片做轻量重编码，返回新字节；无法/无需重编码时返回 ``None``。

    目的是改变图片指纹以绕开 QQ 群聊的内容风控（原图指纹被拦截时
    表现为消息无回执）。流程：

    1. GIF 等动图直接跳过（重编码会丢动画帧）；
    2. JPEG 原图：直接以 ``quality`` 重存，不缩放，尺寸不变；
    3. 其他静态格式（PNG/WebP/未知）：转成 JPEG 再重存——漫画图转
       JPEG 画质损失可忽略，且体积通常更小。

    任何异常都回退原始字节（调用方拿到 ``None`` 即发原图），
    保证「发得出」优先于「换指纹」。
    """

    del path  # 仅用于将来日志定位；此处不打印避免逐张刷屏
    try:
        from io import BytesIO

        from PIL import Image

        with Image.open(BytesIO(data)) as im:
            fmt = (im.format or "").upper()
            if fmt not in _REENCODE_STATIC_FORMATS:
                return None
            im = im.convert("RGB")
            buf = BytesIO()
            im.save(buf, "JPEG", quality=max(50, min(95, quality)))
            out = buf.getvalue()
        # 极端兜底：重编码反而膨胀超过 1.5 倍时仍采用——指纹变了才是目的，
        # 体积只是次要指标；只有超过单张上限才放弃。
        if len(out) > _REENCODE_MAX_INPUT_BYTES:
            return None
        return out
    except Exception:  # noqa: BLE001 - 重编码失败必须回退原图
        return None


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------


class PluginSection(PluginConfigBase):
    """插件基础配置。"""

    __ui_label__ = "插件"
    __ui_icon__ = "package"
    __ui_order__ = 0
    __ui_i18n__ = {
        "zh-CN": {
            "title": "插件",
            "description": "总开关与配置版本。",
        }
    }

    enabled: bool = Field(
        default=True,
        description="是否启用 JM 下载",
        json_schema_extra={
            "label": "启用插件",
            "hint": "关闭后所有 /jm 命令都只回复「已停用」",
        },
    )
    config_version: str = Field(
        default="1.0.0",
        description="配置版本",
        json_schema_extra={
            "label": "配置版本",
            "hint": "改配置结构时由插件维护，一般不用手动改",
            "disabled": True,
        },
    )


class DownloadSection(PluginConfigBase):
    """jmcomic 下载参数。"""

    __ui_label__ = "下载"
    __ui_icon__ = "download"
    __ui_order__ = 1

    base_dir: str = Field(
        default="",
        description="下载根目录，只接受插件数据目录内的相对路径；留空使用 downloads",
        json_schema_extra={
            "label": "下载根目录",
            "placeholder": "downloads/comics",
            "hint": (
                "只允许相对路径，且不得跳出插件数据目录（填绝对路径或 .. 会被忽略并回落到默认值）。"
                "留空则存到 data/plugins/<插件ID>/downloads"
            ),
        },
    )
    dir_rule: str = Field(
        default="Bd_Pname",
        description="jmcomic 目录规则，Bd=根目录，Pname=章节名",
        json_schema_extra={
            "label": "目录规则",
            "hint": "保持默认即可；改错会导致所有图片堆在同一层",
        },
    )
    threads: int = Field(
        default=8,
        ge=1,
        le=32,
        description="单章图片下载并发数",
        json_schema_extra={
            "label": "下载并发数",
            "hint": "并发越高越快，但太大容易被站点限流；建议 4~16",
            "step": 1,
        },
    )
    image_suffix: str = Field(
        default="",
        description="强制图片后缀（如 .jpg）；留空保留原格式",
        json_schema_extra={
            "label": "图片后缀",
            "placeholder": ".jpg",
            "hint": "站点图片多为 webp；填 .jpg 可让部分 QQ 适配器更稳",
        },
    )
    client_impl: Literal["api", "html"] = Field(
        default="api",
        description="jmcomic 客户端实现",
        json_schema_extra={
            "label": "客户端实现",
            "hint": "api 更快更省流量；html 在 api 失效时作备选",
        },
    )
    domains: str = Field(
        default="",
        description="自定义域名，多个用逗号分隔；留空使用 jmcomic 内置域名池",
        json_schema_extra={
            "label": "自定义域名",
            "placeholder": "18comic.vip, jmcomic.me",
            "hint": "只有内置域名全部失效时才需要填",
        },
    )
    proxy: str = Field(
        default="",
        description="代理地址，例如 http://127.0.0.1:7890；留空跟随系统代理",
        json_schema_extra={
            "label": "代理地址",
            "placeholder": "http://127.0.0.1:7890",
            "hint": "站点打不开时优先在这里挂代理；留空用系统代理",
        },
    )
    decode: bool = Field(
        default=True,
        description="是否解码图片；关闭后直接保存原始文件",
        json_schema_extra={
            "label": "解码图片",
            "hint": "关闭后保存的是站点原始（可能加密/拼接）文件，不建议",
        },
    )
    retry_times: int = Field(
        default=3,
        ge=0,
        le=10,
        description="请求重试次数",
        json_schema_extra={
            "label": "请求重试次数",
            "hint": "单张图片下载失败后的重试次数；网络差可适当加大",
            "step": 1,
        },
    )
    delete_after_send: bool = Field(
        default=False,
        description="发送成功后删除本地图片",
        json_schema_extra={
            "label": "发送后删除",
            "hint": "开启可省磁盘，但重复下载同一车号会重新拉取",
        },
    )


class SendSection(PluginConfigBase):
    """下载完成后怎么发到群里。"""

    __ui_label__ = "发送"
    __ui_icon__ = "send"
    __ui_order__ = 2

    mode: Literal["forward", "image", "none"] = Field(
        default="forward",
        description="forward=合并转发（推荐），image=逐张发送，none=只下载不发送",
        json_schema_extra={
            "label": "发送方式",
            "hint": "forward=打包成一条聊天记录（推荐）；image=逐张发；none=只下载不发",
        },
    )
    max_images: int = Field(
        default=0,
        ge=0,
        le=500,
        description="每次最多发送多少张；0=不限制，把整部作品一口气发完",
        json_schema_extra={
            "label": "单次发送上限",
            "hint": "0=不限制，一部漫画全部发完；想防止刷屏可填 20 之类的数",
            "step": 1,
        },
    )
    forward_batch: int = Field(
        default=0,
        ge=0,
        le=500,
        description="单条聊天记录最多放多少张；0=不限制，尽量塞进同一条，只有超过体积上限才拆",
        json_schema_extra={
            "label": "单条记录张数",
            "hint": "0=不限制，全部图片合并成一条聊天记录；体积超限才会自动拆",
            "step": 1,
        },
    )
    forward_retry: int = Field(
        default=2,
        ge=0,
        le=5,
        description="合并转发失败后的重试次数；适配器偶发未回执时很有效",
        json_schema_extra={
            "label": "合并转发重试",
            "hint": "QQ 常返回「未确认回执」但消息其实已送达，重试可提高成功率",
            "step": 1,
        },
    )
    forward_fallback: Literal["none", "image"] = Field(
        default="none",
        description="重试仍失败时的兜底：none=不补发（图片不会跑到聊天记录外面），image=逐张补发",
        json_schema_extra={
            "label": "失败兜底",
            "hint": "none=不补发，避免图片跑到聊天记录外面；image=逐张重发（可能重复）",
        },
    )
    image_interval_ms: int = Field(
        default=300,
        ge=0,
        le=5000,
        description="逐张发送的间隔（毫秒）",
        json_schema_extra={
            "label": "逐张发送间隔",
            "hint": "仅在发送方式为 image 或兜底补发时生效；太小容易触发平台限流",
            "step": 100,
        },
    )
    max_image_mb: float = Field(
        default=8.0,
        ge=0.1,
        le=50.0,
        description="单张图片大小上限（MB）",
        json_schema_extra={
            "label": "单张大小上限",
            "hint": "超过此大小的图片会跳过不发送，避免顶到 RPC 体积上限",
            "step": 0.5,
        },
    )
    reencode: bool = Field(
        default=True,
        description="发送前对图片轻量重编码（换图片指纹）。QQ 群聊会按图片指纹拦截已知的来源图片（表现为「无回执」静默吞图），重编码可绕开；视觉上几乎无差别",
        json_schema_extra={
            "label": "图片重编码",
            "hint": "群聊吞图（提示无回执）时开启；换掉图片指纹可绕开 QQ 群风控，画质几乎无损",
        },
    )
    reencode_quality: int = Field(
        default=88,
        ge=50,
        le=95,
        description="重编码的 JPEG 质量（1-95）；越高越清晰、体积越大",
        json_schema_extra={
            "label": "重编码质量",
            "hint": "默认 88，视觉无损；风控仍吞图时可降到 80 试试",
            "step": 2,
        },
    )
    group_fallback: Literal["image", "none"] = Field(
        default="image",
        description="群聊合并转发被 QQ 云端审核拦截时的兜底：image=改为逐张直发重编码图（实测可过），none=不补发",
        json_schema_extra={
            "label": "群聊兜底",
            "hint": "QQ 对群聊合并转发做云端内容审核（漫画图必拦），拦截后自动改为逐张直发；逐张直发只查指纹、重编码后可过",
        },
    )


class LimitSection(PluginConfigBase):
    """频率与配额限制。"""

    __ui_label__ = "限制"
    __ui_icon__ = "shield"
    __ui_order__ = 3

    daily_per_chat: int = Field(
        default=5,
        ge=0,
        le=200,
        description="每个群/私聊每天最多下载几次，0 表示不限制",
        json_schema_extra={
            "label": "每日下载配额",
            "hint": "按群/私聊计数，每天 0 点重置；0=不限制",
            "step": 1,
        },
    )
    max_concurrent: int = Field(
        default=1,
        ge=1,
        le=5,
        description="同时执行的任务数",
        json_schema_extra={
            "label": "同时下载数",
            "hint": "并发下载多个车号；过大容易把站点或机器带宽打满",
            "step": 1,
        },
    )
    max_pending: int = Field(
        default=10,
        ge=1,
        le=100,
        description="排队任务上限",
        json_schema_extra={
            "label": "排队上限",
            "hint": "等待中的任务超过此数量就拒绝新请求",
            "step": 1,
        },
    )
    cooldown_seconds: int = Field(
        default=10,
        ge=0,
        le=3600,
        description="同一用户两次下载的最短间隔（秒）",
        json_schema_extra={
            "label": "用户冷却（秒）",
            "hint": "防止单人连续刷屏；0=不冷却",
            "step": 5,
        },
    )
    allowed_chats: str = Field(
        default="",
        description="只允许这些群号/QQ号使用，逗号分隔；留空表示不限制",
        json_schema_extra={
            "label": "会话白名单",
            "placeholder": "123456789, 987654321",
            "hint": "填群号或 QQ 号，多个用逗号分隔；留空表示所有会话都能用",
        },
    )
    blocked_chats: str = Field(
        default="",
        description="禁止使用本插件的群号/QQ号，逗号分隔；优先级高于白名单",
        json_schema_extra={
            "label": "会话黑名单",
            "placeholder": "111111111, 222222222",
            "hint": "填群号或 QQ 号，多个用逗号分隔；命中即拒绝、白名单也救不回来。留空表示不拉黑",
        },
    )
    private_only: bool = Field(
        default=False,
        description="开启后只在私聊中响应，群聊一律拒绝。群聊合并转发会被 QQ 云端审核拦截、逐张直发又有刷屏和账号风险，可开此项只保留私聊功能",
        json_schema_extra={
            "label": "仅私聊生效",
            "hint": "开启后群聊里所有命令都不响应（含搜索/下载/标签）；私聊不受影响",
        },
    )


class SearchSection(PluginConfigBase):
    """搜索展示。"""

    __ui_label__ = "搜索"
    __ui_icon__ = "search"
    __ui_order__ = 4

    display: int = Field(
        default=10,
        ge=1,
        le=30,
        description="每次搜索展示多少条结果（同时是 /jm tag 的每页条数）",
        json_schema_extra={
            "label": "每页展示条数",
            "hint": "搜索与标签搜索共用的翻页步长；/jm search 关键词 页码、/jm tag 标签 页码",
            "step": 1,
        },
    )
    tag_candidates: int = Field(
        default=200,
        ge=1,
        le=600,
        description="多标签搜索时最多核对多少条候选作品（单标签用服务端搜索，不受影响）",
        json_schema_extra={
            "label": "多标签核对上限",
            "hint": "单标签走服务端标签搜索、几秒出结果；多标签必须逐条核对详情，这个值决定最多查多少条",
            "step": 20,
        },
    )
    cover: bool = Field(
        default=True,
        description="搜索结果是否附带封面图（禁漫列表缩略图，每张约 40~100KB）",
        json_schema_extra={
            "label": "显示封面",
            "hint": "开着更直观，但每次搜索要多下 10 张缩略图；关掉就只发文字，更快",
        },
    )
    cover_count: int = Field(
        default=10,
        ge=0,
        le=30,
        description="每次搜索最多给多少条结果配封面",
        json_schema_extra={
            "label": "封面张数上限",
            "hint": "与每页条数一致即可；设成 0 等同于关闭封面",
            "step": 1,
        },
    )


class ToolSection(PluginConfigBase):
    """给 LLM 用的工具开关。"""

    __ui_label__ = "LLM 工具"
    __ui_icon__ = "bot-message-square"
    __ui_order__ = 6

    allow_download: bool = Field(
        default=False,
        description="是否允许 LLM 工具 jmcomic_download 自行创建下载任务",
        json_schema_extra={
            "label": "允许 AI 触发下载",
            "hint": (
                "默认关闭。开启后模型可以不经用户确认就把图片发进群；"
                "只读工具（搜索/详情）始终可用，不受此项影响"
            ),
        },
    )


class FilterSection(PluginConfigBase):
    """AI 作品过滤。"""

    __ui_label__ = "AI 过滤"
    __ui_icon__ = "bot"
    __ui_order__ = 5

    enabled: bool = Field(
        default=False,
        description="是否过滤 AI 作品（禁漫 tag 含 AI上色 / AI / AI生成 等）",
        json_schema_extra={
            "label": "启用 AI 过滤",
            "hint": "按禁漫自带的 tag 与标题标记识别；关闭后所有作品都可下载",
        },
    )
    scope: Literal["search", "search_download", "all"] = Field(
        default="all",
        description="过滤范围：search=仅搜索，search_download=搜索+下载，all=搜索/下载/详情都过滤",
        json_schema_extra={
            "label": "过滤范围",
            "hint": "search=只过滤搜索结果；search_download=搜索+下载；all=连 /jm info 也过滤",
        },
    )
    keep_in_search: bool = Field(
        default=False,
        description="搜索结果里是否仍展示 AI 作品（仅标注不隐藏）",
        json_schema_extra={
            "label": "搜索仍展示",
            "hint": "true=搜索结果里保留 AI 作品（只是下载时仍会拦）；false=直接从列表隐藏",
        },
    )
    operator_bypass: bool = Field(
        default=True,
        description="管理员是否可跳过过滤强制下载",
        json_schema_extra={
            "label": "管理员可绕过",
            "hint": "开启后本地操作员（管理员）发车号仍可下载 AI 作品",
        },
    )


class JmComicConfig(PluginConfigBase):
    """JMComic 插件完整配置。"""

    plugin: PluginSection = Field(default_factory=PluginSection)
    download: DownloadSection = Field(default_factory=DownloadSection)
    send: SendSection = Field(default_factory=SendSection)
    limits: LimitSection = Field(default_factory=LimitSection)
    search: SearchSection = Field(default_factory=SearchSection)
    tools: ToolSection = Field(default_factory=ToolSection)
    filter: FilterSection = Field(default_factory=FilterSection)


# ---------------------------------------------------------------------------
# 插件主体
# ---------------------------------------------------------------------------


class JmComicPlugin(MaiBotPlugin):
    """按车号下载 JM 本子，并把图片发回聊天流。"""

    config_model = JmComicConfig

    def __init__(self) -> None:
        super().__init__()
        self._service = JmService()
        self._jobs: dict[str, Job] = {}
        self._tasks: set[asyncio.Task[Any]] = set()
        self._semaphore: asyncio.Semaphore | None = None
        self._quota = QuotaBook()
        self._quota_path: Path | None = None
        self._save_lock: asyncio.Lock | None = None
        self._last_start: dict[str, float] = {}
        self._root = Path(".")
        self._runtime = Path(".")
        self._jm_version = ""
        #: 车号 -> AI 判定原因（"" 表示不是 AI 作品），避免重复查详情
        self._ai_reason_cache: dict[str, str] = {}
        #: 车号 -> 已确认的标题，供汇报与 AI 判定复用
        self._title_cache: dict[str, str] = {}

    # ===== 生命周期 =====

    async def on_load(self) -> None:
        """初始化目录、配额和 jmcomic。"""

        config = self.config
        self._root = self._resolve_download_root()
        self._runtime = Path(self.ctx.paths.runtime_dir)
        self._quota_path = Path(self.ctx.paths.data_dir) / "quota.json"
        for directory in (self._root, self._runtime, self._quota_path.parent):
            directory.mkdir(parents=True, exist_ok=True)

        self._quota = QuotaBook.from_dict(
            self._read_json(self._quota_path),
            limit=config.limits.daily_per_chat,
        )
        self._semaphore = asyncio.Semaphore(max(1, config.limits.max_concurrent))

        available, detail = await asyncio.to_thread(self._service.probe)
        self._jm_version = detail if available else ""
        if available:
            self.ctx.logger.info("JMComic 插件已加载，jmcomic %s，下载目录 %s", detail, self._root)
        else:
            self.ctx.logger.warning("JMComic 插件已加载，但 jmcomic 不可用：%s", detail)

    async def on_unload(self) -> None:
        """取消所有后台任务并保存配额。"""

        for job in list(self._jobs.values()):
            if not job.terminal and job.control is not None:
                with suppress(Exception):
                    job.control.cancel("插件卸载")
                job.mark_cancelled("插件卸载")

        for task in list(self._tasks):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)

        self._write_quota_sync()
        self.ctx.logger.info("JMComic 插件已卸载")

    async def on_config_update(self, scope: str, config_data: dict[str, Any], version: str) -> None:
        """热更新目录、配额与并发上限。"""

        del config_data
        if scope != CONFIG_RELOAD_SCOPE_SELF:
            return

        config = self.config
        self._quota.limit = config.limits.daily_per_chat
        self._root = self._resolve_download_root()
        self._quota_path = Path(self.ctx.paths.data_dir) / "quota.json"
        for directory in (self._root, self._quota_path.parent):
            directory.mkdir(parents=True, exist_ok=True)

        # 有任务在跑时不打断，等空闲后自然生效
        if not self._active_jobs():
            self._semaphore = asyncio.Semaphore(max(1, config.limits.max_concurrent))
        self.ctx.logger.info("JMComic 配置已更新: version=%s", version)

    # ===== 命令 =====

    @Command(
        "jmcomic",
        description="JMComic 下载、搜索与任务管理",
        pattern=_COMMAND_PATTERN,
        timeout_ms=1200000,
    )
    async def handle_jm(
        self,
        text: str = "",
        stream_id: str = "",
        group_id: str = "",
        user_id: str = "",
        platform: str = "",
        is_local_operator: bool = False,
        matched_groups: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> tuple[bool, None, bool]:
        """``/jm ...`` 主入口。"""

        del kwargs
        args = str((matched_groups or {}).get("args") or "").strip()
        if not args:
            args = self._strip_command(text)
        await self._dispatch(args, stream_id, group_id, user_id, platform, is_local_operator)
        return True, None, True

    @Command("jmcomic_plate", description="直接发送 JM 车号触发下载", pattern=_PLATE_PATTERN)
    async def handle_plate(
        self,
        stream_id: str = "",
        group_id: str = "",
        user_id: str = "",
        platform: str = "",
        is_local_operator: bool = False,
        matched_groups: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> tuple[bool, None, bool]:
        """``JM123456`` 这种裸车号。"""

        del kwargs
        plate_id = str((matched_groups or {}).get("plate_id") or "").strip()
        if plate_id.isdigit():
            ref = JmRef(jm_id=plate_id, kind="album", raw=f"JM{plate_id}")
            await self._download(
                ref, stream_id, group_id, user_id, platform, operator=is_local_operator
            )
        return True, None, True

    # ===== 工具（给 LLM 调用）=====

    @Tool(
        "jmcomic_search",
        description=(
            "搜索禁漫（JMComic）作品，返回编号与标题。mode 选搜索维度："
            "site=站内全字段；work=作品系列/原作（如 蔚蓝档案 这类企划名，结果更多更准）；"
            "author=作者/画师；actor=登场人物/角色；tag=精确标签名。"
            "当用户想找本子、但只给了关键词/作品名/作者/角色而没有给车号时使用。"
        ),
        parameters=[
            ToolParameterInfo(name="keyword", param_type=ToolParamType.STRING, description="搜索关键词", required=True),
            ToolParameterInfo(name="page", param_type=ToolParamType.INTEGER, description="页码，从 1 开始", required=False),
            ToolParameterInfo(
                name="mode",
                param_type=ToolParamType.STRING,
                description="site=站内全字段（默认），work=作品系列/原作，author=作者，actor=登场人物，tag=精确标签",
                required=False,
            ),
        ],
    )
    async def tool_search(
        self,
        keyword: str = "",
        page: int = 1,
        mode: str = "site",
        user_id: str = "",
        group_id: str = "",
        **kwargs: Any,
    ) -> dict[str, Any]:
        """搜索并返回文本结果，不发送图片。"""

        del kwargs
        if not self.config.plugin.enabled:
            return {"success": False, "content": "JM 插件已停用"}
        reject = self._chat_reject_reason(group_id, user_id)
        if reject:
            return {"success": False, "content": reject.replace("⛔ ", "")}
        text = str(keyword or "").strip()
        if not text:
            return {"success": False, "content": "关键词不能为空"}
        try:
            chosen = str(mode or "").strip().lower()
            if chosen not in _SEARCH_MODES:
                chosen = "site"
            result = await self._search(text, page, chosen)
        except asyncio.TimeoutError:
            return {"success": False, "content": "搜索超时，请稍后再试"}
        except JmError as exc:
            return {"success": False, "content": str(exc)}
        return {"success": True, "content": self._format_search(result)}

    @Tool(
        "jmcomic_download",
        description=(
            "按车号下载禁漫（JMComic）本子，并把图片发送到当前聊天。"
            "**默认被管理员关闭**，关闭时调用只会返回提示；"
            "需要用户明确要求下载并给出数字车号才可调用。"
            "参数 jm_id 是纯数字编号，kind 为 album（整本）或 photo（单章）。"
        ),
        parameters=[
            ToolParameterInfo(name="jm_id", param_type=ToolParamType.STRING, description="纯数字车号", required=True),
            ToolParameterInfo(
                name="kind",
                param_type=ToolParamType.STRING,
                description="album=整本，photo=单章",
                required=False,
            ),
        ],
    )
    async def tool_download(
        self,
        jm_id: str = "",
        kind: str = "album",
        stream_id: str = "",
        group_id: str = "",
        user_id: str = "",
        platform: str = "",
        is_local_operator: bool = False,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """把下载任务丢进队列，立即返回任务号。"""

        del kwargs
        # 审核意见：把触发权交给规划器风险太大，默认关闭，需管理员显式开启。
        if not self.config.tools.allow_download:
            return {
                "success": False,
                "content": (
                    "本插件的 AI 下载工具默认关闭。"
                    "请让用户直接发送 /jm <车号> 自行触发，或由管理员在插件配置里开启「允许 AI 触发下载」。"
                ),
            }
        clean_id = re.sub(r"(?i)^(?:jmcomic|jm|禁漫|车号|車號)[-_:#：]?\s*", "", str(jm_id or "").strip())
        if not clean_id.isdigit() or not 4 <= len(clean_id) <= 8:
            return {"success": False, "content": "jm_id 必须是 4~8 位纯数字车号"}
        ref = JmRef(jm_id=clean_id, kind="photo" if str(kind).lower() == "photo" else "album", raw=clean_id)
        task_id = await self._download(
            ref,
            stream_id,
            group_id,
            user_id,
            platform,
            quiet=True,
            operator=bool(is_local_operator),
        )
        if not task_id:
            return {"success": False, "content": "任务没有创建成功，请确认插件已启用、配额未用完"}
        return {"success": True, "content": f"下载任务已创建：{task_id}"}

    @HomeCard(
        "jmcomic_home",
        title="JMComic 下载",
        description="按车号下载本子并发送到聊天，支持搜索与任务管理。",
        content=[
            {"type": "markdown", "content": "发送 `/jm 350234` 下载整本，`/jm p123456` 只下载单章。"},
            {"type": "list", "items": ["/jm info 350234：查看详情", "/jm search 关键词：搜索", "/jm status：任务状态"]},
        ],
        width="medium",
        order=200,
    )
    async def home_card(self) -> None:
        """声明 WebUI 首页卡片。"""

        return None

    # ===== 命令分发 =====

    async def _dispatch(
        self,
        args: str,
        stream_id: str,
        group_id: str,
        user_id: str,
        platform: str,
        operator: bool = False,
    ) -> None:
        """解析 ``/jm`` 后面的参数。"""

        config = self.config
        if not config.plugin.enabled:
            await self._say(stream_id, "JM 下载已停用，请联系管理员在插件配置里开启。")
            return
        reject = self._chat_reject_reason(group_id, user_id)
        if reject:
            await self._say(stream_id, reject)
            return

        raw = str(args or "").strip()
        if not raw:
            await self._say(stream_id, _HELP_TEXT)
            return

        head, _, rest = raw.partition(" ")
        head = head.strip().lower()
        rest = rest.strip()

        if head in _HELP_WORDS:
            await self._say(stream_id, _HELP_TEXT)
            return
        if head in _TAG_WORDS:
            if not rest:
                await self._say(stream_id, "用法：/jm tag 标签1[、标签2 …] [页码]")
                return
            await self._tag_command(rest, stream_id)
            return
        if head in _SERIES_WORDS:
            if not rest:
                await self._say(stream_id, "用法：/jm series 作品 [页码]，例如 /jm series 蔚蓝档案")
                return
            await self._search_command(rest, stream_id, mode="work")
            return
        if head in _AUTHOR_WORDS:
            if not rest:
                await self._say(stream_id, "用法：/jm author 作者 [页码]，例如 /jm author ぽんこつ")
                return
            await self._search_command(rest, stream_id, mode="author")
            return
        if head in _ACTOR_WORDS:
            if not rest:
                await self._say(stream_id, "用法：/jm actor 角色 [页码]，例如 /jm actor 白洲アロナ")
                return
            await self._search_command(rest, stream_id, mode="actor")
            return
        if head in _SEARCH_WORDS:
            if not rest:
                await self._say(stream_id, "用法：/jm search 关键词 [页码]")
                return
            await self._search_command(rest, stream_id, mode="site")
            return
        if head in _INFO_WORDS:
            await self._info_command(rest, stream_id)
            return
        if head in _STATUS_WORDS:
            await self._status_command(stream_id)
            return
        if head in _CANCEL_WORDS:
            await self._cancel_command(rest, stream_id, user_id, group_id, operator)
            return
        if head in _DOWNLOAD_WORDS:
            raw = rest
            if not raw:
                await self._say(stream_id, "用法：/jm 350234 或 /jm p123456")
                return

        ref = parse_jm_ref(raw)
        if ref is not None:
            await self._download(ref, stream_id, group_id, user_id, platform, operator=operator)
            return

        # 没给车号就当成搜索关键词，省得用户记两条命令
        await self._search_command(raw, stream_id)

    # ===== 下载 =====

    async def _download(
        self,
        ref: JmRef,
        stream_id: str,
        group_id: str,
        user_id: str,
        platform: str,
        *,
        quiet: bool = False,
        operator: bool = False,
    ) -> str | None:
        """校验配额后创建下载任务，返回任务号；被拒绝时返回 ``None``。"""

        config = self.config
        if not config.plugin.enabled:
            if not quiet:
                await self._say(stream_id, "JM 下载已停用，请联系管理员在插件配置里开启。")
            return None

        if not self._jm_version:
            if not quiet:
                await self._say(
                    stream_id,
                    "❌ jmcomic 不可用，请先在 MaiBot 的运行环境安装 jmcomic 再重载插件。",
                )
            return None

        if self._filter_covers("download"):
            reason = self._cached_ai_reason(ref.jm_id)
            if reason is None:
                # 详情里才有 tag，需要先查一次；查询失败不拦截，交给下载阶段报错
                try:
                    reason = await asyncio.wait_for(
                        asyncio.to_thread(self._lookup_ai_reason, ref),
                        timeout=30,
                    )
                except Exception:  # noqa: BLE001 - 查询失败时放行，避免误拦
                    self.ctx.logger.warning("AI 过滤查询详情失败：JM%s", ref.jm_id, exc_info=True)
                    reason = ""
                else:
                    self._remember_ai_reason(ref.jm_id, reason)
            if reason:
                blocked = await self._ai_block_message(ref, reason, operator)
                if blocked:
                    if not quiet:
                        await self._say(stream_id, blocked)
                    return None

        reject = self._chat_reject_reason(group_id, user_id)
        if reject:
            if not quiet:
                await self._say(stream_id, reject)
            return None

        if not stream_id:
            if not quiet:
                self.ctx.logger.warning("JM 下载缺少 stream_id，无法回传结果")
            return None

        duplicate = next(
            (
                job
                for job in self._jobs.values()
                if not job.terminal and job.stream_id == stream_id and job.ref.jm_id == ref.jm_id
            ),
            None,
        )
        if duplicate is not None:
            if not quiet:
                await self._say(stream_id, f"⏳ JM{ref.jm_id} 已经在任务 {duplicate.task_id} 里处理了。")
            return None

        if len(self._active_jobs()) >= config.limits.max_pending:
            if not quiet:
                await self._say(stream_id, "⏳ 排队任务太多了，等前面的下载结束再试。")
            return None

        cooldown = int(config.limits.cooldown_seconds or 0)
        if cooldown > 0:
            now = time.monotonic()
            last = self._last_start.get(user_id or stream_id, 0.0)
            if now - last < cooldown:
                wait = int(cooldown - (now - last)) + 1
                if not quiet:
                    await self._say(stream_id, f"⏱ 操作太快了，{wait} 秒后再试。")
                return None

        quota_key = self._quota_key(group_id, user_id)
        if not self._quota.consume(quota_key):
            if not quiet:
                await self._say(
                    stream_id,
                    f"📛 今天这个会话的下载次数用完了（上限 {config.limits.daily_per_chat} 次）。",
                )
            return None

        self._last_start[user_id or stream_id] = time.monotonic()
        self._prune_cooldowns()

        job = Job(
            ref=ref,
            stream_id=stream_id,
            user_id=user_id,
            group_id=group_id,
            platform=platform,
            directory=str(self._root / f"JM{ref.jm_id}"),
        )
        self._jobs[job.task_id] = job
        self._prune_jobs()

        if not quiet:
            await self._say(
                stream_id,
                f"⏳ 收到 {ref.label}，任务 {job.task_id} 已排队。\n下载完成后会自动发送图片。",
            )

        task = asyncio.create_task(self._run_job(job), name=f"jm-download-{job.task_id}")
        job.task = task
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        self.ctx.logger.info("创建下载任务 %s：%s", job.task_id, ref.label)
        return job.task_id

    async def _run_job(self, job: Job) -> None:
        """后台任务：下载 -> 发送 -> 汇报。"""

        try:
            semaphore = self._semaphore or asyncio.Semaphore(1)
            async with semaphore:
                job.mark_running()
                job.control = await asyncio.to_thread(self._service.new_control)
                monitor = asyncio.create_task(self._monitor_progress(job))
                try:
                    outcome = await asyncio.to_thread(
                        self._service.download,
                        job.ref,
                        job.directory,
                        self._settings(),
                        control=job.control,
                    )
                finally:
                    monitor.cancel()
                    with suppress(asyncio.CancelledError):
                        await monitor

                job.title = outcome.info.title
                job.author = outcome.info.author
                job.page_count = outcome.info.page_count
                job.chapter_count = outcome.info.chapter_count
                job.files = list(outcome.files)
                job.warning = outcome.warning
                job.done_images = len(outcome.files)
                job.total_images = max(job.total_images, len(outcome.files))

                if not outcome.files:
                    raise JmError("下载结束但没有找到图片文件")

                job.mark_sending()
                sent = await self._deliver(job)
                job.sent_images = sent
                job.mark_done()
                await self._say(job.stream_id, self._final_text(job))

        except asyncio.CancelledError:
            if not job.terminal:
                job.mark_cancelled("任务被中止")
                self._refund_quota(job)
                await self._safe_say(job.stream_id, f"🚫 {job.display_title} 已取消（任务 {job.task_id}）。")
            raise
        except JmCancelled as exc:
            # 用户取消时 _cancel_command 已经标记过终态并退过配额，这里不能重复处理
            if not job.terminal:
                job.mark_cancelled(str(exc))
                self._refund_quota(job)
                await self._safe_say(job.stream_id, f"🚫 {job.display_title} 已取消：{short(exc, 120)}")
        except JmUnavailable as exc:
            if self._fail_job(job, str(exc)):
                await self._safe_say(job.stream_id, f"❌ jmcomic 不可用：{short(exc, 200)}")
        except JmError as exc:
            if self._fail_job(job, str(exc)):
                await self._safe_say(job.stream_id, f"❌ {job.display_title} 失败：{short(exc, 200)}")
        except Exception as exc:  # noqa: BLE001 - 后台任务不能把异常抛给事件循环
            reason = f"{type(exc).__name__}: {exc}"
            if self._fail_job(job, reason):
                self.ctx.logger.exception("JM 下载任务 %s 异常", job.task_id)
                await self._safe_say(job.stream_id, f"❌ {job.display_title} 失败：{short(exc, 180)}")
        finally:
            if job.state == STATE_DONE and job.sent_images > 0 and self.config.download.delete_after_send:
                await self._cleanup(job)
            await self._persist_quota()

    async def _monitor_progress(self, job: Job) -> None:
        """定期数一下落盘图片，作为进度展示。"""

        while not job.terminal:
            try:
                count = await asyncio.to_thread(count_images, job.directory)
            except Exception:  # noqa: BLE001 - 进度统计失败不影响下载
                count = job.done_images
            if count > job.done_images:
                job.done_images = count
            await asyncio.sleep(_PROGRESS_INTERVAL)

    # ===== 发送 =====

    async def _deliver(self, job: Job) -> int:
        """按配置把下载好的图片发到聊天流，返回成功张数。"""

        config = self.config
        if config.send.mode == "none":
            return 0

        selected, skipped = await asyncio.to_thread(self._select_files, job.files)
        if not selected:
            raise JmError("没有符合条件的图片（可能都超过单张大小上限）")

        if config.send.mode == "forward":
            sent, unconfirmed = await self._send_all_in_one_record(job, selected)
        else:
            sent = await self._send_one_by_one(selected, job.stream_id)
            unconfirmed = 0

        job.sent_images = sent
        job.unconfirmed_images = unconfirmed
        if skipped:
            self.ctx.logger.info("%s 有 %s 张图片因超过大小上限被跳过", job.display_title, skipped)
        if sent == 0 and unconfirmed == 0:
            raise JmError("图片一张都没发出去，可能是平台限流、图片过大或适配器不支持")
        return sent

    async def _send_all_in_one_record(self, job: Job, files: list[str]) -> tuple[int, int]:
        """把全部图片放进**同一条聊天记录**发出，失败按配置兜底。

        用户诉求：所有图片必须塞进同一条聊天记录，不拆批、不逐张。
        即使 RPC 帧预算被迫拆分（图特别多时），也只拆成最少批数，
        且每批都重试到成功或达到上限。

        实测（2026-10）：QQ 对**群聊合并转发**走云端内容审核（识别图片
        内容本身，漫画图必拦，换指纹/加边框/转格式均无效），被拦时协议
        返回 ``result=0`` 但 ``sequence=null``（无回执）；**逐张直发**只查
        图片指纹库，重编码即可通过。因此群聊（``job.group_id`` 非空）在
        合并转发确认失败后，按 ``send.group_fallback`` 配置降级为逐张
        直发重编码图，避免无意义硬撞风控累积账号风险。

        返回 ``(确认发出张数, 未确认张数)``。未确认指平台没回执但可能实际
        已进聊天记录的情况。
        """

        batches = self._plan_forward_batches_minimal(files)
        total_sent = 0
        total_unconfirmed = 0

        for index, batch in enumerate(batches):
            nodes: list[dict[str, Any]] = []
            for path in batch:
                encoded = await self._encode_image(path)
                if not encoded:
                    continue
                nodes.append(
                    {
                        "user_id": "0",
                        "nickname": f"JM{job.ref.jm_id}",
                        "segments": [{"type": "image", "content": encoded}],
                    }
                )
            if not nodes:
                continue

            sent, missed = await self._send_record_with_retry(job, nodes, index, len(batches))
            total_sent += sent
            total_unconfirmed += missed
            if len(batches) > 1:
                await asyncio.sleep(1.0)

        # 群聊兜底：合并转发被云端审核拦截时，改逐张直发（重编码图可过）
        if (
            total_sent == 0
            and total_unconfirmed > 0
            and job.group_id
            and self.config.send.group_fallback == "image"
        ):
            self.ctx.logger.info(
                "群聊合并转发被 QQ 云端审核拦截（%s 张未确认），改为逐张直发重编码图",
                total_unconfirmed,
            )
            await self._say(
                job.stream_id,
                f"⚠️ 群聊合并转发被 QQ 风控拦截，改为逐张发送《{job.display_title}》"
                f"（共 {total_unconfirmed} 张，间隔稍长请耐心等待）",
            )
            sent = await self._send_one_by_one(files, job.stream_id)
            if sent > 0:
                total_sent = sent
                total_unconfirmed = 0

        return total_sent, total_unconfirmed

    async def _send_record_with_retry(
        self, job: Job, nodes: list[dict[str, Any]], index: int, total_batches: int
    ) -> tuple[int, int]:
        """重试发送一条合并转发，直到成功或达到重试上限。

        与旧逻辑的区别：**永不逐张补发**。逐张会破坏「所有图片在一条聊天
        记录里」的形式，宁可全部未确认也不拆散。重试间隔从 1.5s 拉长到 5s，
        给 QQ 风控留冷却时间。
        """

        total = len(nodes)
        label = (
            job.display_title
            if total_batches == 1
            else f"{job.display_title}（第 {index + 1}/{total_batches} 条）"
        )
        # 重试上限回到配置驱动（默认 2）。实测群聊合并转发被云端内容审核
        # 拦截时重试必然再次失败，多试只会累积账号风险（严重时被踢下线）。
        attempts = max(1, int(self.config.send.forward_retry) + 1)
        for attempt in range(1, attempts + 1):
            try:
                ok = await self.ctx.send.forward(nodes, job.stream_id)
            except Exception:  # noqa: BLE001 - 适配器异常也算一次失败
                self.ctx.logger.warning(
                    "合并转发异常（第 %s/%s 次）%s", attempt, attempts, label, exc_info=True
                )
                ok = False
            if ok:
                return total, 0
            self.ctx.logger.warning(
                "合并转发未确认（第 %s/%s 次）：%s，%s 张", attempt, attempts, label, total
            )
            if attempt < attempts:
                await asyncio.sleep(5.0)

        self.ctx.logger.warning(
            "合并转发 %s 次尝试后仍未确认：%s，%s 张（按用户要求不逐张补发，保持聊天记录形式）",
            attempts,
            label,
            total,
        )
        return 0, total

    async def _send_one_by_one(self, files: list[str], stream_id: str) -> int:
        """逐张发送；失败继续下一张。"""

        interval = max(0, int(self.config.send.image_interval_ms)) / 1000.0
        sent = 0
        for path in files:
            encoded = await self._encode_image(path)
            if not encoded:
                continue
            try:
                if await self.ctx.send.image(encoded, stream_id):
                    sent += 1
                else:
                    self.ctx.logger.warning("发送图片失败: %s", path)
            except Exception:  # noqa: BLE001 - 单张失败不终止整个任务
                self.ctx.logger.exception("发送图片异常: %s", path)
            if interval:
                await asyncio.sleep(interval)
        return sent

    async def _encode_image(self, path: str) -> str:
        """读文件并编码 base64；读取失败返回空串。

        开启 ``send.reencode`` 时先做轻量重编码：QQ 群聊风控按图片指纹
        （文件 md5）拦截已知来源的图片——实测原图被静默吞掉（协议返回
        ``result=0`` 但 ``sequence=null``，即无回执），同一张图用 Pillow
        重新编码改变指纹后即可正常发出。动图（GIF）与重编码失败时回退
        原始字节，保证「发得出」优先于「换指纹」。
        """

        try:
            data = await asyncio.to_thread(Path(path).read_bytes)
        except OSError as exc:
            self.ctx.logger.warning("读取图片失败 %s: %s", path, exc)
            return ""
        if not data:
            return ""
        if len(data) > _MAX_IMAGE_BYTES:
            self.ctx.logger.warning("图片过大，跳过发送: %s (%s bytes)", path, len(data))
            return ""
        if self.config.send.reencode:
            encoded = await asyncio.to_thread(_reencode_image, data, path, int(self.config.send.reencode_quality))
            if encoded is not None:
                data = encoded
        return base64.b64encode(data).decode("ascii")

    def _plan_forward_batches_minimal(self, files: list[str]) -> list[list[str]]:
        """把图片切成**最少批数**的合并转发。

        用户要求所有图片进同一条聊天记录，所以这里忽略 ``forward_batch``
        张数限制，只受 RPC 帧预算约束（超预算会被 Runner 拒收，必须拆）。
        正常长度的漫画（一两百张以内）都会是 1 批。
        """

        batches: list[list[str]] = []
        current: list[str] = []
        current_size = 0

        for path in files:
            try:
                raw_size = Path(path).stat().st_size
            except OSError:
                continue
            encoded_size = (raw_size + 2) // 3 * 4
            if current and current_size + encoded_size > _FRAME_BUDGET:
                batches.append(current)
                current = []
                current_size = 0
            current.append(path)
            current_size += encoded_size

        if current:
            batches.append(current)

        if len(batches) > 1:
            self.ctx.logger.info(
                "共 %s 张，超过单条 RPC 帧预算（%s 字节），最少拆成 %s 条聊天记录",
                len(files),
                _FRAME_BUDGET,
                len(batches),
            )
        return batches

    def _select_files(self, files: list[str]) -> tuple[list[str], int]:
        """按张数上限和单张大小筛选；``max_images=0`` 表示不限制。"""

        max_bytes = min(int(float(self.config.send.max_image_mb) * 1024 * 1024), _MAX_IMAGE_BYTES)
        raw_limit = max(0, int(self.config.send.max_images))
        limit = raw_limit or len(files)
        selected: list[str] = []
        skipped = 0
        for path in files:
            if len(selected) >= limit:
                break
            try:
                size = Path(path).stat().st_size
            except OSError:
                skipped += 1
                continue
            if size <= 0:
                skipped += 1
                continue
            if max_bytes > 0 and size > max_bytes:
                skipped += 1
                continue
            selected.append(path)
        return selected, skipped

    def _final_text(self, job: Job) -> str:
        """任务成功的汇报文本。"""

        lines = [f"✅ {job.display_title}"]
        meta: list[str] = []
        if job.author:
            meta.append(f"作者：{short(job.author, 20)}")
        if job.page_count:
            meta.append(f"{job.page_count} 页")
        if job.chapter_count > 1:
            meta.append(f"{job.chapter_count} 章")
        if meta:
            lines.append(" ｜ ".join(meta))

        mode_text = {"forward": "合并转发", "image": "逐张发送", "none": "仅下载"}.get(
            self.config.send.mode, self.config.send.mode
        )
        delivered = job.sent_images + job.unconfirmed_images
        lines.append(f"下载 {job.done_images} 张，发送 {delivered} 张（{mode_text}）")
        if job.unconfirmed_images:
            lines.append(
                f"ℹ️ 其中 {job.unconfirmed_images} 张平台未返回回执，通常已在聊天记录里，未重复补发"
            )
        if job.warning:
            lines.append(f"⚠️ {short(job.warning, 120)}")
        lines.append(f"耗时 {human_duration(job.duration)}，任务 {job.task_id}")
        if self.config.send.mode == "none":
            lines.append(f"目录：{job.directory}")
        return "\n".join(lines)

    async def _cleanup(self, job: Job) -> None:
        """删除任务目录，且只允许删下载根目录内的内容。"""

        directory = Path(job.directory)
        if not job.directory or directory == self._root or not is_relative_to(directory, self._root):
            return
        try:
            await asyncio.to_thread(shutil.rmtree, directory, True)
        except Exception:  # noqa: BLE001
            self.ctx.logger.warning("清理目录失败: %s", directory, exc_info=True)

    # ===== 查询 =====

    async def _info_command(self, raw: str, stream_id: str) -> None:
        """``/jm info <车号>``。"""

        ref = parse_jm_ref(raw)
        if ref is None:
            await self._say(stream_id, "用法：/jm info 350234")
            return
        await self._say(stream_id, f"🔍 正在查询 {ref.label} …")
        try:
            info = await asyncio.wait_for(
                asyncio.to_thread(self._service.fetch, ref, self._runtime, self._settings()),
                timeout=45,
            )
        except asyncio.TimeoutError:
            await self._say(stream_id, "⌛ 查询超时了，可能是网络或代理的问题。")
            return
        except JmError as exc:
            await self._say(stream_id, f"❌ 查询失败：{short(exc, 200)}")
            return

        ai_reason = detect_ai_reason(
            tags=info.tags, title=info.title, description=getattr(info, "description", "")
        )
        text = describe_media(info) + f"\n下载：/jm {ref.jm_id}"
        if ai_reason:
            # ``scope=all`` 时连详情也过滤，只标注不展示正文
            if self._filter_covers("detail"):
                await self._say(stream_id, f"🤖 {ref.label} 是 AI 作品（{ai_reason}），已按配置过滤。")
                return
            text = f"🤖 这是 AI 作品（{ai_reason}）\n" + text
        await self._say(stream_id, text)

    # ===== AI 过滤 =====

    def _filter_covers(self, area: str) -> bool:
        """判断 AI 过滤是否覆盖指定场景。

        ``area`` 取 ``search`` / ``download`` / ``detail``。
        """

        section = self.config.filter
        if not section.enabled:
            return False
        if section.scope == "all":
            return True
        if section.scope == "search":
            return area == "search"
        # search_download：覆盖搜索与下载，不含详情
        return area in {"search", "download"}

    def _cached_ai_reason(self, jm_id: str) -> str | None:
        """取缓存里的 AI 判定；没查过返回 ``None``。"""

        return self._ai_reason_cache.get(jm_id)

    def _remember_ai_reason(self, jm_id: str, reason: str) -> None:
        """缓存 AI 判定，避免同一车号反复查详情。"""

        if len(self._ai_reason_cache) >= _AI_CACHE_MAX:
            self._ai_reason_cache.clear()
        self._ai_reason_cache[jm_id] = reason

    def _lookup_ai_reason(self, ref: JmRef) -> str:
        """查详情判断是否 AI 作品（阻塞方法，需放线程里跑）。"""

        info = self._service.fetch(ref, self._runtime, self._settings())
        reason = detect_ai_reason(
            tags=info.tags, title=info.title, description=getattr(info, "description", "")
        )
        if info.title:
            ref_info = self._title_cache.get(ref.jm_id)
            if ref_info != info.title:
                self._title_cache[ref.jm_id] = info.title
        return reason

    async def _ai_block_message(self, ref: JmRef, reason: str, operator: bool) -> str | None:
        """返回拦截提示；允许放行时返回 ``None``。"""

        if reason and self.config.filter.operator_bypass and operator:
            return None
        return (
            f"🤖 {ref.label} 是 AI 作品（{reason}），已按配置过滤。\n"
            "如需下载，请让管理员在插件配置里关闭过滤。"
        )

    def _filter_search_hits(self, result: SearchResult) -> tuple[SearchResult, int]:
        """按配置过滤搜索结果，返回 ``(新结果, 被过滤条数)``。

        搜索接口不返回 tag，所以这里只能用标题判断；详情里的 tag 判断留给
        下载/详情路径。``keep_in_search`` 为真时只标注、不删除。
        """

        if not self._filter_covers("search"):
            return result, 0

        kept: list[Any] = []
        dropped = 0
        for hit in result.hits:
            reason = detect_ai_reason(title=hit.title)
            if reason and not self.config.filter.keep_in_search:
                dropped += 1
                continue
            kept.append(hit)

        if not dropped:
            return result, 0

        return (
            SearchResult(
                keyword=result.keyword,
                page=result.page,
                page_count=result.page_count,
                total=max(0, result.total - dropped),
                hits=tuple(kept),
                offset=result.offset,
            ),
            dropped,
        )

    async def _search_command(self, keyword: str, stream_id: str, mode: str = "site") -> None:
        """``/jm search 关键词 [页码]``（mode=site）与 ``/jm series 关键词 [页码]``（mode=work）。"""

        command, label = _SEARCH_MODES.get(mode, _SEARCH_MODES["site"])
        usage = f"/jm {command} 关键词 [页码]"
        text = str(keyword or "").strip()
        if not text:
            await self._say(stream_id, f"用法：{usage}")
            return

        page = 1
        match = re.search(r"\s+(\d{1,3})\s*$", text)
        if match:
            page = max(1, int(match.group(1)))
            text = text[: match.start()].strip()

        await self._say(stream_id, f"🔍 正在按{label}搜索「{short(text, 30)}」第 {page} 页…")
        try:
            result = await asyncio.wait_for(
                asyncio.to_thread(
                    self._service.search,
                    text,
                    page,
                    self._runtime,
                    self._settings(),
                    page_size=int(self.config.search.display),
                    mode=mode,
                ),
                timeout=60,
            )
        except asyncio.TimeoutError:
            await self._say(stream_id, "⌛ 搜索超时了，稍后再试。")
            return
        except JmError as exc:
            await self._say(stream_id, f"❌ 搜索失败：{short(exc, 200)}")
            return

        filtered, dropped = self._filter_search_hits(result)
        lines, pairs = self._search_result_lines(filtered, dropped)
        covers = await self._fetch_covers(pairs)
        await self._send_result_record(stream_id, lines, "搜索结果", covers)

    async def _search(self, keyword: str, page: int, mode: str = "site") -> SearchResult:
        """工具调用用的搜索，不做回复。"""

        return await asyncio.wait_for(
            asyncio.to_thread(
                self._service.search,
                keyword,
                page,
                self._runtime,
                self._settings(),
                page_size=int(self.config.search.display),
                mode=mode,
            ),
            timeout=60,
        )

    def _search_result_lines(
        self, result: SearchResult, dropped: int = 0
    ) -> tuple[list[str], list[tuple[str, str]]]:
        """把搜索结果排成聊天记录内容。

        返回 ``(行列表, [(车号, 标题行), ...])``：行列表用于发送，第二个值
        用于事后按车号下封面、再把封面与标题行对上。
        """

        command, label = _SEARCH_MODES.get(result.mode, _SEARCH_MODES["site"])
        total_text = f"{result.total}{'+' if result.capped else ''}"
        page_count = max(1, result.page_count)

        if not result.hits:
            if dropped:
                return (
                    [
                        f"🔎「{short(result.keyword, 24)}」本页 {dropped} 条结果已按 AI 过滤隐藏，"
                        "换一页或换个关键词试试。"
                    ],
                    [],
                )
            if result.total:
                return (
                    [
                        f"🔎 按{label}「{short(result.keyword, 24)}」共 {total_text} 条结果，"
                        f"没有第 {result.page} 页（共 {page_count} 页）。",
                        f"回到第 1 页：/jm {command} {result.keyword} 1",
                    ],
                    [],
                )
            return ([f"🔎 没有搜到「{short(result.keyword, 30)}」相关的作品。"], [])

        header = (
            f"🔎 按{label}「{short(result.keyword, 24)}」第 {result.page}/{page_count} 页，"
            f"共 {total_text} 条"
            + (f"（本页 {result.offset}-{result.offset + len(result.hits) - 1}）" if result.offset else "")
        )
        if dropped:
            header += f"，已过滤 {dropped} 条 AI 作品"
        lines = [header]
        pairs: list[tuple[str, str]] = []
        for hit in result.hits:
            tag_text = ""
            if hit.tags:
                tag_text = " [" + " ".join(short(tag, 8) for tag in hit.tags[:3]) + "]"
            # 标题不截断：搜索结果就是要看清到底是哪一本
            line = f"JM{hit.jm_id} {hit.title}{tag_text}"
            lines.append(line)
            pairs.append((hit.jm_id, line))

        hints: list[str] = ["发送 /jm 车号 下载"]
        if result.page < page_count:
            hints.append(f"下一页：/jm {command} {result.keyword} {result.page + 1}")
        if result.page > 1:
            hints.append(f"上一页：/jm {command} {result.keyword} {result.page - 1}")
        lines.append("；".join(hints))
        return lines, pairs

    def _format_search(self, result: SearchResult, dropped: int = 0) -> str:
        """把搜索结果排成一段文本（给 LLM 工具用，不进聊天记录）。"""

        lines, _pairs = self._search_result_lines(result, dropped)
        return "\n".join(lines)

    async def _tag_command(self, raw: str, stream_id: str) -> None:
        """``/jm tag 标签1[、标签2 …] [页码]``：按标签搜索作品。

        结果是「逐条查详情核对标签」得来，一次要一两分钟，所以命中列表由
        :class:`JmService` 缓存，翻页基本是瞬时的。
        """

        text = str(raw or "").strip()
        page = 1
        match = re.search(r"\s+(\d{1,3})\s*$", text)
        if match:
            page = max(1, int(match.group(1)))
            text = text[: match.start()].strip()

        tags = parse_tag_query(text)
        if not tags:
            await self._say(stream_id, "用法：/jm tag 标签1[、标签2 …] [页码]，多个标签用「、」分隔")
            return

        shown = "、".join(short(tag, 12) for tag in tags)
        candidates = max(1, int(self.config.search.tag_candidates))
        if len(tags) == 1:
            # 单标签走服务端标签搜索，一次请求一页
            await self._say(stream_id, f"🏷 正在按标签搜索「{shown}」第 {page} 页…")
            timeout = 180
        else:
            # 多标签服务端做不到 AND，只能挑最稀有的标签当候选流逐条核对，
            # 按页增量：凑够这一页就停
            await self._say(
                stream_id,
                f"🏷 正在按标签搜索「{shown}」第 {page} 页，多标签要逐条核对详情（约半分钟）…",
            )
            timeout = min(_TAG_MAX_WAIT, 90 + int(candidates * 1.5))
        try:
            result = await asyncio.wait_for(
                asyncio.to_thread(
                    self._service.search_by_tags,
                    tags,
                    self._runtime,
                    self._settings(),
                    max_candidates=candidates,
                    page=page,
                    page_size=max(1, int(self.config.search.display)),
                    # 只有开了 AI 过滤才需要 tag：列表接口不返回 tag，
                    # 补齐要多查一轮详情，会明显拖慢单标签翻页
                    with_tags=bool(self.config.filter.enabled),
                ),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            await self._say(
                stream_id,
                f"⌛ 标签搜索超过 {int(timeout)} 秒还没跑完。"
                "把「多标签核对上限」调小、少给几个标签，或稍后再试。",
            )
            return
        except JmError as exc:
            await self._say(stream_id, f"❌ 标签搜索失败：{short(exc, 200)}")
            return

        await self._send_tag_result(stream_id, result)

    async def _send_tag_result(self, stream_id: str, result: TagSearchResult) -> None:
        """把标签搜索结果作为**一条合并转发**发出去，避免刷屏。"""

        lines, pairs = self._tag_result_lines(result)
        covers = await self._fetch_covers(pairs)
        await self._send_result_record(stream_id, lines, "标签搜索结果", covers)

    async def _fetch_covers(self, pairs: list[tuple[str, str]]) -> list[tuple[str, str]]:
        """给一页搜索结果配封面，返回 ``[(标题行, 本地图片路径)]``。

        ``pairs`` 是 ``[(jm_id, 标题行)]``。封面是锦上添花：开关关掉、超时、
        单张失败都只是少几张图，列表照常发。图片缓存在插件数据目录的
        ``covers/`` 下，同一车号不会反复下载。

        封面 URL 由车号直接拼出（``/media/albums/<id>_3x4.jpg``），不需要先
        查详情，所以代价只是 N 张小图。
        """

        if not pairs or not self.config.search.cover:
            return []

        limit = max(0, int(self.config.search.cover_count))
        if not limit:
            return []
        wanted = [(str(jm_id), line) for jm_id, line in pairs if jm_id and line][:limit]
        if not wanted:
            return []

        try:
            mapping = await asyncio.wait_for(
                asyncio.to_thread(
                    self._service.fetch_covers,
                    [jm_id for jm_id, _line in wanted],
                    self._covers_dir(),
                    self._settings(),
                ),
                timeout=_COVER_TIMEOUT,
            )
        except asyncio.TimeoutError:
            self.ctx.logger.warning("封面下载超时，本次只发文字")
            return []
        except Exception:  # noqa: BLE001 - 封面失败绝不能影响搜索
            self.ctx.logger.warning("封面下载失败，本次只发文字", exc_info=True)
            return []

        return [(line, mapping[jm_id]) for jm_id, line in wanted if jm_id in mapping]

    def _covers_dir(self) -> Path:
        """封面缓存目录（插件数据目录下）。"""

        base = getattr(self.ctx.paths, "data_dir", None) or self._runtime
        return Path(base) / "covers"

    def _filter_tag_hits(self, hits: Any) -> tuple[list[Any], int]:
        """对标签搜索的一页结果做 AI 过滤，返回 ``(保留, 过滤条数)``。

        和关键词搜索不同，这里标签已经在详情里拿到了，判定比只看标题更准。
        """

        if not self._filter_covers("search"):
            return list(hits), 0

        kept: list[Any] = []
        dropped = 0
        for hit in hits:
            reason = detect_ai_reason(tags=hit.tags, title=hit.title)
            if reason and not self.config.filter.keep_in_search:
                dropped += 1
                continue
            kept.append(hit)

        return kept, dropped

    def _tag_result_lines(self, result: TagSearchResult) -> tuple[list[str], list[tuple[str, str]]]:
        """把标签搜索结果排成聊天记录内容。

        单标签走服务端搜索，页数、总数都是确定的；多标签是逐条核对出来的，
        「共多少部 / 共几页」只有核对完才确定，没核对完就如实说「已命中 N 部」。

        返回 ``(行列表, [(车号, 标题行), ...])``，后者用于配封面。
        """

        shown = "、".join(short(tag, 12) for tag in result.tags)
        page_count = max(1, result.page_count)
        server = result.mode == "server"

        if not result.total:
            lines = [f"🏷 没有找到{'同时' if not server else ''}带有「{shown}」的作品。"]
            if not server and result.candidates:
                lines.append(f"已核对 {result.checked}/{result.candidates} 条候选，{result.failed} 条查询失败。")
            if not server:
                lines.append("提示：多标签是「同时满足」，标签越少越容易命中；也可以调大「多标签核对上限」。")
            return lines, []

        if not result.hits:
            return (
                [
                    f"🏷「{shown}」共命中 {result.total}{'+' if result.capped else ''} 部，"
                    f"没有第 {result.page} 页（共 {page_count} 页）。",
                    f"回到第 1 页：/jm tag {shown} 1",
                ],
                [],
            )

        kept, dropped = self._filter_tag_hits(result.hits)
        span = f"第 {result.offset}-{result.offset + len(result.hits) - 1} 部"
        total_text = f"{result.total}{'+' if result.capped else ''}"
        if server or result.exhausted:
            lines = [f"🏷 标签「{shown}」第 {result.page}/{page_count} 页｜共命中 {total_text} 部（{span}）"]
        else:
            lines = [
                f"🏷 标签「{shown}」第 {result.page} 页"
                f"｜已命中 {result.total} 部（{span}），后面还有候选没核对"
            ]
        if not server and result.candidates:
            note = f"候选 {result.candidates} 条，已核对 {result.checked} 条"
            if result.failed:
                note += f"，{result.failed} 条查询失败"
            if result.cached:
                note += "，接着上次进度"
            lines.append(note)
        if dropped:
            lines.append(f"已按 AI 过滤隐藏本页 {dropped} 部")

        pairs: list[tuple[str, str]] = []
        for hit in kept:
            tag_text = ""
            if hit.tags:
                tag_text = " [" + " ".join(short(tag, 8) for tag in hit.tags[:4]) + "]"
            # 标题不截断，方便直接看清是哪一本
            line = f"JM{hit.jm_id} {hit.title}{tag_text}"
            lines.append(line)
            pairs.append((hit.jm_id, line))

        if not kept:
            lines.append("本页结果都被 AI 过滤隐藏了，换一页看看。")

        hints = ["发送 /jm 车号 下载"]
        if result.page > 1:
            hints.append(f"上一页：/jm tag {shown} {result.page - 1}")
        if not result.exhausted or result.page < page_count:
            hints.append(f"下一页：/jm tag {shown} {result.page + 1}")
        lines.append("；".join(hints))
        return lines, pairs

    async def _status_command(self, stream_id: str) -> None:
        """``/jm status``：只展示当前会话的任务。"""

        jobs = [job for job in self._jobs.values() if job.stream_id == stream_id]
        active = [job for job in jobs if not job.terminal]
        recent = sorted((job for job in jobs if job.terminal), key=lambda item: item.finished_at, reverse=True)[:3]

        if not active and not recent:
            await self._say(stream_id, "当前没有 JM 下载任务。")
            return

        lines = ["📋 JM 任务" + (f"（jmcomic {self._jm_version}）" if self._jm_version else "")]
        if active:
            lines.extend(format_job_line(job) for job in active)
        else:
            lines.append("· 没有进行中的任务")
        if recent:
            lines.append("最近：")
            lines.extend(format_job_line(job) for job in recent)
        await self._say(stream_id, "\n".join(lines))

    async def _cancel_command(
        self,
        raw: str,
        stream_id: str,
        user_id: str,
        group_id: str,
        operator: bool = False,
    ) -> None:
        """``/jm cancel <任务号|all>``，普通成员只能取消自己的任务。"""

        del group_id
        target = str(raw or "").strip().lower()
        if not target:
            await self._say(stream_id, "用法：/jm cancel 任务号 或 /jm cancel all")
            return

        active = [
            job
            for job in self._jobs.values()
            if not job.terminal
            and job.stream_id == stream_id
            and (operator or job.user_id == user_id)
        ]
        if target in {"all", "全部", "*"}:
            targets = active
        else:
            targets = [job for job in active if job.task_id.startswith(target)]

        if not targets:
            await self._say(stream_id, "没有找到可取消的任务。")
            return

        for job in targets:
            if job.control is not None:
                with suppress(Exception):
                    job.control.cancel("用户取消")
            job.mark_cancelled("用户取消")
            if job.task is not None:
                job.task.cancel()
            self._quota.refund(self._quota_key(job.group_id, job.user_id))

        await self._say(stream_id, "🚫 已取消：" + "、".join(job.task_id for job in targets))
        await self._persist_quota()

    # ===== 小工具 =====

    def _settings(self) -> DownloadSettings:
        """把插件配置转成 jmcomic 参数。"""

        config = self.config.download
        domains = tuple(
            item.strip()
            for item in re.split(r"[,，\s]+", str(config.domains or ""))
            if item.strip()
        )
        return DownloadSettings(
            dir_rule=str(config.dir_rule or "Bd_Pname"),
            threads=max(1, int(config.threads)),
            image_suffix=str(config.image_suffix or ""),
            client_impl=str(config.client_impl or "api"),
            domains=domains,
            proxy=str(config.proxy or ""),
            decode=bool(config.decode),
            retry_times=max(0, int(config.retry_times)),
        )

    def _resolve_download_root(self) -> Path:
        """解析下载根目录，**只允许落在插件数据目录内**。

        审核意见：``base_dir`` 以前接受任意绝对路径，配合
        ``delete_after_send=true`` 会在该目录下先建再删 ``JM<车号>``。
        虽然 ``_cleanup`` 有 ``is_relative_to`` 兜底、删不出下载根目录，
        但运维把 base_dir 指到共享目录时仍会污染那里。

        现在规则：
        - 绝对路径、盘符路径、含 ``..`` 的路径一律忽略，回落到默认目录；
        - 所有结果都会被解析成本地路径并校验是否仍在数据目录内。
        """

        data_root = Path(self.ctx.paths.data_dir).resolve()
        default_root = data_root / "downloads"

        raw = str(self.config.download.base_dir or "").strip()
        if not raw:
            return default_root

        candidate = Path(raw).expanduser()
        # 绝对路径 / 盘符 / UNC 一律拒绝
        if candidate.is_absolute() or candidate.drive or candidate.root:
            self.ctx.logger.warning(
                "base_dir 只接受相对路径，已忽略绝对路径：%s", raw
            )
            return default_root

        resolved = (data_root / candidate).resolve()
        if not is_relative_to(resolved, data_root) or resolved == data_root:
            self.ctx.logger.warning(
                "base_dir 越出插件数据目录，已忽略：%s", raw
            )
            return default_root

        return resolved

    @staticmethod
    def _parse_chat_ids(raw: Any) -> set[str]:
        """把逗号/空格分隔的群号或 QQ 号解析成集合。"""

        return {item.strip() for item in re.split(r"[,，\s]+", str(raw or "")) if item.strip()}

    def _chat_blocked(self, group_id: str, user_id: str) -> bool:
        """是否被黑名单拦截（优先级高于白名单）。"""

        blocked = self._parse_chat_ids(self.config.limits.blocked_chats)
        return bool(blocked & {group_id, user_id})

    def _chat_allowed(self, group_id: str, user_id: str) -> bool:
        """按配置白名单判断当前会话能不能用。"""

        allowed = self._parse_chat_ids(self.config.limits.allowed_chats)
        if not allowed:
            return True
        return bool(allowed & {group_id, user_id})

    def _chat_reject_reason(self, group_id: str, user_id: str) -> str:
        """返回拒绝原因；允许使用时返回空串。

        黑名单优先级最高：命中即拒绝，白名单也无法豁免；
        ``private_only`` 开启时群聊一律拒绝。
        """

        if self.config.limits.private_only and group_id:
            return "🔒 本插件已设置为仅私聊生效，请在私聊中使用。"
        if self._chat_blocked(group_id, user_id):
            return "⛔ 当前会话已被拉黑，无法使用 JM 功能。"
        if not self._chat_allowed(group_id, user_id):
            return "⛔ 当前会话不在白名单内，没有使用 JM 的功能权限。"
        return ""

    @staticmethod
    def _quota_key(group_id: str, user_id: str) -> str:
        """配额按群（私聊按人）计。"""

        return f"g:{group_id}" if group_id else f"u:{user_id}"

    def _refund_quota(self, job: Job) -> None:
        """退回一次配额。"""

        self._quota.refund(self._quota_key(job.group_id, job.user_id))

    def _fail_job(self, job: Job, reason: str) -> bool:
        """把任务标记为失败并退配额；已经终态（如被用户取消）时返回 ``False``。"""

        if job.terminal:
            return False
        job.mark_failed(reason)
        self._refund_quota(job)
        return True

    def _active_jobs(self) -> list[Job]:
        """未结束的任务。"""

        return [job for job in self._jobs.values() if not job.terminal]

    def _prune_jobs(self) -> None:
        """只保留最近的若干条任务记录，避免常驻内存无限涨。"""

        if len(self._jobs) <= _MAX_JOBS_KEPT:
            return
        finished = sorted(
            (job for job in self._jobs.values() if job.terminal),
            key=lambda item: item.finished_at,
        )
        for job in finished[: max(0, len(self._jobs) - _MAX_JOBS_KEPT)]:
            self._jobs.pop(job.task_id, None)

    def _prune_cooldowns(self) -> None:
        """清掉一天前的冷却记录。"""

        if len(self._last_start) < 500:
            return
        cutoff = time.monotonic() - 86400
        self._last_start = {key: value for key, value in self._last_start.items() if value >= cutoff}

    @staticmethod
    def _strip_command(text: str) -> str:
        """从原始文本里剥掉命令前缀，兼容没匹配到命名组的情况。"""

        match = re.match(r"(?i)^\s*[/／]\s*(?:jmcomic|jm|禁漫)\s*(.*)$", str(text or ""), re.S)
        return match.group(1).strip() if match else str(text or "").strip()

    async def _say(self, stream_id: str, text: str) -> bool:
        """发一条文本；失败只记日志。"""

        if not stream_id or not text:
            return False
        try:
            sent = bool(await self.ctx.send.text(text, stream_id))
        except Exception:  # noqa: BLE001 - 发送失败不应影响任务状态
            self.ctx.logger.exception("发送消息失败 stream=%s", stream_id)
            return False
        if not sent:
            # 常见原因：被出站 Hook 拦截，或适配器没有确认回执
            self.ctx.logger.warning("文本消息未送达: %s", short(text, 60))
        return sent

    async def _safe_say(self, stream_id: str, text: str) -> None:
        """``_say`` 的无返回值版本。"""

        await self._say(stream_id, text)

    async def _send_result_record(
        self,
        stream_id: str,
        lines: list[str],
        what: str,
        covers: list[tuple[str, str]] | None = None,
    ) -> None:
        """把一页搜索结果打包成**一条合并转发**发出去，避免刷屏。

        所有搜索（站内 / 作品系列 / 作者 / 登场人物 / 标签）都走这条路：
        一页 10 条如果一条条发，群里几秒钟就被刷满了；聊天记录只占一个气泡，
        点开就是完整列表。

        ``covers`` 是 ``[(标题行, 本地封面路径), ...]``：有封面的条目发成
        「图 + 标题」两个节点，没有的就只发文本。封面失败不影响列表本身。

        平台拒绝合并转发时退回一条普通文本，保证用户至少能看到结果。
        """

        nodes = await self._record_nodes(lines, covers)
        if not nodes:
            return
        if await self._send_record_nodes(stream_id, nodes):
            return
        self.ctx.logger.warning("%s合并转发失败，退回普通文本", what)
        await self._say(stream_id, "\n".join(lines))

    async def _record_nodes(
        self, lines: list[str], covers: list[tuple[str, str]] | None = None
    ) -> list[dict[str, Any]]:
        """把文本行 + 封面组装成合并转发的节点列表。

        一条结果就是**一个节点**：``segments`` 里先放封面图、再放标题文本，
        合并转发里点开看到的是「图 + 标题」挨在一起的一小条，而不是拆成
        两条消息。读图失败（文件没了 / 太大）时该节点只留标题，不影响整条记录。
        """

        cover_map = {str(line): path for line, path in (covers or ())}

        nodes: list[dict[str, Any]] = []
        for line in lines:
            if not line:
                continue
            segments: list[dict[str, Any]] = []
            path = cover_map.get(str(line), "")
            if path:
                encoded = await self._encode_image(path)
                if encoded:
                    segments.append({"type": "image", "content": encoded})
            segments.append({"type": "text", "content": line})
            nodes.append(
                {
                    "user_id": "0",
                    "nickname": _SEARCH_RECORD_NAME,
                    "segments": segments,
                }
            )
        return nodes

    async def _send_record_nodes(self, stream_id: str, nodes: list[dict[str, Any]]) -> bool:
        """发送组装好的合并转发节点，按 ``forward_retry`` 重试。"""

        if not stream_id or not nodes:
            return False

        attempts = max(1, int(self.config.send.forward_retry) + 1)
        for attempt in range(1, attempts + 1):
            try:
                if await self.ctx.send.forward(nodes, stream_id):
                    return True
            except Exception:  # noqa: BLE001 - 适配器异常同样按失败重试
                self.ctx.logger.warning(
                    "合并转发异常（第 %s/%s 次）", attempt, attempts, exc_info=True
                )
            if attempt < attempts:
                await asyncio.sleep(1.0)

        return False

    async def _send_text_record(self, stream_id: str, lines: list[str], nickname: str) -> bool:
        """把若干行纯文本打包成一条合并转发（无封面时用）。"""

        del nickname
        return await self._send_record_nodes(stream_id, await self._record_nodes(lines))

    # -- 配额持久化 ---------------------------------------------------------

    @staticmethod
    def _read_json(path: Path) -> Any:
        """读 JSON；文件不存在或损坏时返回 ``None``。"""

        try:
            with path.open("r", encoding="utf-8") as handle:
                return json.load(handle)
        except (OSError, ValueError):
            return None

    @staticmethod
    def _write_json(path: Path, data: Any) -> None:
        """原子写 JSON。"""

        temp = path.with_suffix(path.suffix + ".tmp")
        path.parent.mkdir(parents=True, exist_ok=True)
        with temp.open("w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
        temp.replace(path)

    async def _persist_quota(self) -> None:
        """异步保存配额。"""

        if self._quota_path is None:
            return
        if self._save_lock is None:
            self._save_lock = asyncio.Lock()
        data = self._quota.to_dict()
        async with self._save_lock:
            try:
                await asyncio.to_thread(self._write_json, self._quota_path, data)
            except Exception:  # noqa: BLE001
                self.ctx.logger.warning("保存配额失败: %s", self._quota_path, exc_info=True)

    def _write_quota_sync(self) -> None:
        """卸载时的同步保存。"""

        if self._quota_path is None:
            return
        try:
            self._write_json(self._quota_path, self._quota.to_dict())
        except Exception:  # noqa: BLE001
            pass


def create_plugin() -> JmComicPlugin:
    """MaiBot 插件工厂。"""

    return JmComicPlugin()




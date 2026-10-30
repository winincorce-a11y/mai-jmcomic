# 投稿到 MaiBot 插件仓库

本文件记录把 MaiBot 插件 **JMComic 下载**（`com.deepseek.jmcomic`）投稿到
官方插件市场需要做的事。市场索引仓库是
[`Mai-with-u/plugin-repo`](https://github.com/Mai-with-u/plugin-repo)，
主程序通过它读取 `plugin_details.json` / `plugin_versions.json`。

---

## 一、投稿前必须满足的条件（已逐条核对）

| 条件 | 要求 | 本插件状态 |
| --- | --- | --- |
| Manifest 版本 | `manifest_version` 必须为 `2` | ✅ `2` |
| 插件 ID | 字母/数字/下划线，用 `.` 或 `-` 分隔，至少两段 | ✅ `com.deepseek.jmcomic` |
| 版本号 | **严格三段式** `X.Y.Z` | ✅ `1.5.1` |
| 必填字段 | `id` `version` `name` `description` `author` `license` `urls` `host_application` `sdk` `capabilities` `i18n` | ✅ 全部齐备 |
| `author` | 必须是 `{name, url}`，`url` 必须是 `http(s)://` | ✅ |
| `urls.repository` | 必填，必须是 `http(s)://` | ✅ |
| `host_application` / `sdk` | 必须是 `{min_version, max_version}`，且 `min <= max` | ✅ |
| 入口文件 | 插件目录内必须有 `plugin.py` 和 `_manifest.json` | ✅ |
| 依赖声明 | `type=python_package` 的 `name` / `version_spec` 必须可解析 | ✅ `jmcomic >=2.7,<3` |
| 图标 | `display.icon.type` 只能是 `lucide` / `emoji` / `local` | ✅ `lucide` |
| 更新日志 | `changelog` 必须是 `.md` 相对路径或 http(s) URL | ✅ `CHANGELOG.md` |

> **注意：`keywords` 与 `categories` 不能写进 `_manifest.json`。**
> 主程序的 Manifest 模型是 `extra="forbid"`，写了会直接校验失败
> （实测报「存在未声明字段: categories / keywords」）。市场条目里看到的这两个
> 字段来自 `plugin-repo` 的索引文件，不是插件清单。本插件的市场元数据由
> 索引仓库维护，见下节。

## 二、投稿步骤

### 1. 建一个插件仓库（已完成）

市场按 **Git 仓库 → Release → 资产** 的方式拉取插件，所以必须有一个独立仓库，
且**不能用 MaiBot 主仓、也不能用 jmcomic 上游仓**作为 `urls.repository`。

当前清单里填的是占位地址，投稿前请替换成你自己的仓库：

```text
https://github.com/deepseek-ai/mai-jmcomic
```

**仓库根目录就是插件目录**，结构应为：

```text
mai-jmcomic/
├── _manifest.json
├── plugin.py
├── jm_service.py
├── jm_core.py
├── README.md
├── CHANGELOG.md
├── LICENSE
├── requirements.txt
├── .gitignore
└── tests/
```

即：把本目录内容整体推到仓库根，**不要再套一层 `mai-jmcomic/` 目录**，
否则 Host 找不到 `_manifest.json`。

### 2. 打 Release 并上传资产（已完成）

- Tag 用 `v1.5.1` 形式（与清单 `version` 对应）；
- Release 里附一个 **zip**，解压后能直接看到 `_manifest.json` 与 `plugin.py`；
- 市场会读 Release 里的 `_manifest.json` 来展示版本信息。

### 3. 向 `Mai-with-u/plugin-repo` 提交 PR

在索引仓库中添加/更新该插件的条目（`id` 用清单里的 `com.deepseek.jmcomic`），
至少包含：

```json
{
  "id": "com.deepseek.jmcomic",
  "manifest": { "...": "与 _manifest.json 一致" },
  "repository_url": "https://github.com/deepseek-ai/mai-jmcomic",
  "keywords": ["jmcomic", "禁漫", "本子", "同人", "下载", "漫画"],
  "categories": ["media", "tool"]
}
```

`keywords` / `categories` 写在**索引条目**里，这样市场搜索能找到它。

---

## 三、给审核者的说明（可直接用）

> **插件名**：JMComic 下载（`com.deepseek.jmcomic`）
>
> **功能**：把 `jmcomic` 库接进 MaiBot。群里发车号即可后台下载整本/单章，
> 用合并转发（一条聊天记录）发回；支持五种搜索（站内 / 作品系列 / 作者 /
> 登场人物 / 标签），结果同样以一条聊天记录发出并附带封面。
>
> **能力声明**：只用了 `send.text`、`send.image`、`send.forward`，
> 不读取聊天记录、不调用 LLM、不访问数据库。
>
> **网络行为**：只访问禁漫域名（由 `jmcomic` 库负责域名发现与回退），
> 支持在配置里指定代理与自定义域名。
>
> **依赖**：`jmcomic >=2.7,<3`（`_manifest.json` 已声明，Host 可自动安装）。
>
> **配置**：全部字段都有中文 `label` / `hint`，WebUI 可直接编辑。
>
> **限流**：每日配额、用户冷却、并发/排队上限、会话黑白名单。
>
> **测试**：172 个不联网单测（自带假 `jmcomic` 模块），`python -m unittest
> discover -s tests` 全部通过。
>
> **免责**：插件不内置、不再分发任何内容，下载行为与内容合法性由部署者负责。

---

## 四、投稿前自检清单

- [ ] 把 `_manifest.json` 里的 `urls` 换成真实仓库地址
- [ ] 确认仓库根目录直接是插件目录（不是子目录）
- [ ] 打 `v1.5.1` tag 并上传 zip 资产
- [ ] 确认 `pip install -r requirements.txt` 能装好 `jmcomic`
- [ ] 在干净的 MaiBot 实例里装一遍，确认能加载、`/jm` 能用
- [ ] 确认没有提交 `config.toml`（含个人配置）、`__pycache__`、测试临时目录
- [ ] 向 `plugin-repo` 提 PR，补齐 `keywords` / `categories`

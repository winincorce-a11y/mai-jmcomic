# 投稿到 MaiBot 插件仓库

插件 **JMComic 下载**（`github.winincorce-a11y.mai-jmcomic`）已投稿：

- Issue：<https://github.com/Mai-with-u/plugin-repo/issues/1186>
- 状态：`validated`（自动校验通过），等待维护者 `/approve`

市场索引仓库是 [`Mai-with-u/plugin-repo`](https://github.com/Mai-with-u/plugin-repo)，
投稿走 **Issue 表单**（不是 PR）。

---

## 一、官方要求与当前状态（逐条核对）

| 条件 | 要求 | 状态 |
| --- | --- | --- |
| Manifest 版本 | `manifest_version` 必须为 `2` | ✅ `2` |
| 插件 ID | 字母/数字/下划线，`.` 或 `-` 分隔 | ✅ `github.winincorce-a11y.mai-jmcomic` |
| 版本号 | **严格三段式** `X.Y.Z` | ✅ `1.5.3` |
| 必填字段 | `id` `version` `name` `description` `author` `license` `urls` `host_application` `sdk` `capabilities` `i18n` | ✅ 全部齐备 |
| `author` | `{name, url}`，`url` 必须 `http(s)://` | ✅ |
| `urls.repository` | 公开 GitHub HTTPS，无 `.git` 后缀 | ✅ |
| 版本前两位 | 不能是 `99` / `999` | ✅ `1.3.0`–`1.3.99` |
| 根目录文件 | `_manifest.json`、`plugin.py`、`LICENSE`、`README.md` | ✅ |
| 根目录不能有 `config.toml` | 必须移出版本控制 | ✅ 已 `.gitignore` |
| LICENSE 与 `license` 一致 | MIT | ✅ |
| 已发布 Release | 非草稿、非预发布 | ✅ `v1.5.3` |
| 禁止字段 | `categories` / `keywords` / `repository_url` / `homepage_url` | ✅ 均未出现 |

> **注意**：`keywords` 与 `categories` 不能写进 `_manifest.json`。Host 的
> Manifest 模型是 `extra="forbid"`，写了会直接校验失败。市场条目里的这两个
> 字段由索引仓库维护，不由插件清单提供。

## 二、审核反馈与处理

Issue #1186 的两次 AI 审核结论一致：**工程实现没问题，卡点是内容合规**。

### 已修复的工程项

| # | 审核意见 | 处理 |
| --- | --- | --- |
| 1 | `download.base_dir` 接受任意绝对路径，配合 `delete_after_send=true` 会在该目录建目录再删掉 | 只接受**插件数据目录内的相对路径**；绝对路径/盘符/`..` 越界一律忽略并回落默认目录（`_resolve_download_root`） |
| 2 | LLM 工具 `jmcomic_download` 把触发权交给规划器，无约束 | 新增 `[tools].allow_download`，**默认 `false`**；关闭时调用只返回提示。只读工具不受影响 |
| 3 | 封面缓存 `covers/` 只增不清 | 新增 `_trim_cover_cache()`：上限 500 个文件，超出按 mtime 删到 80% |
| 4 | README 头部版本仍是 1.1.0，与 manifest 不符 | 已改为 `1.5.3`，与本文件、manifest 三处一致 |

### 未解决（无法通过改代码解决）

| 问题 | 说明 |
| --- | --- |
| **内容合规** | 抓取源是禁漫天堂（18comic.vip），R-18 + 盗版汉化，落在「拒绝色情和违反国家法律法规的插件」一条。这是插件的**主干功能**，加开关也绕不开，需维护者定夺 |

审核原话：

> 内容合规这条先给出明确说法，工程问题修好后可以再触发 `recheck` 复审。

所以在维护者对内容属性给出结论前，继续迭代代码意义有限。

## 三、复审方式

工程修复发布后，在 Issue #1186 里单独发一条评论：

```text
/recheck
```

`/recheck` 会重新校验 manifest 并触发新一轮 AI 审核（每 Issue 上限 8 次，
已用 2 次）。**这个命令只有 Issue 作者和维护者能用**——需要投稿账号本人操作。

## 四、发布新版本的流程

1. 改 `_manifest.json` 的 `version`（三段式），确保与本次提交一致
2. 提交代码，打 tag（`v1.5.3` 或 `1.5.3`），发布 GitHub Release
3. 已收录版本的 Tag / commit **不可修改**；修复只能发新版本号

## 五、自检清单

- [x] `urls` 指向真实仓库 `https://github.com/winincorce-a11y/mai-jmcomic`
- [x] 仓库根目录直接是插件目录（没有再套一层）
- [x] Release 已发布（`v1.5.3`）并带 zip 资产
- [x] 根目录无 `config.toml`、无 `__pycache__`、无测试临时目录
- [x] `base_dir` 已限制在插件数据目录内
- [x] AI 下载工具默认关闭
- [x] 封面缓存有淘汰策略
- [ ] **内容合规结论**（待维护者定夺，无法自行完成）

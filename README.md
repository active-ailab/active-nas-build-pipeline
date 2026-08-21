<<<<<<< HEAD
# NAS 固件发布自动化流水线

> 华米 Zepp 智能手表固件：从 CI 构建到飞书文档交付的一站式 DevOps 自动化系统。
> 配置驱动 · 分层解耦 · 一键发布

## 项目定位

把「触发 Jenkins 构建 → 下载产物 → 解包/重组 → 上传 NAS → 生成共享链接 → 生成飞书版本文档 → 通知」这条发版链路，做成一套**可复用、可观测、可扩展**的自动化系统。适用于 MHS003 / MHS003S / NXP595（Apollo4）系列芯片平台的固件版本发布。

## 核心特性

- **配置驱动**：新增一个设备只需添加一份 `gqf_*.json` 配置文件，无需改任何代码。
- **三入口一致**：CLI 命令行 / Web 管理后台 / 飞书 Bot 自然语言，共用同一套核心引擎。
- **DSL 解耦**：文件处理流程（解压、挑选、拷贝）由 JSON DSL 描述，与业务逻辑分离。
- **飞书深度集成**：Wiki 模板复制 + `{{REL_*}}` 占位符替换 + 文件名超链接化 + 权限分发 + 消息通知。
- **凭证分级**：环境变量 > 配置文件 > 占位符，敏感信息多层保护。

## 核心链路

```
触发 Jenkins 构建 ──▶ 下载产物 ──▶ DSL 准备(解包/重组) ──▶ NAS 上传(WebDAV)
                                                              │
        ┌─────────────────────────────────────────────────────┘
        ▼
生成 DSM 共享链接 ──▶ 生成版本文档 ──▶ 飞书 Wiki 复制 + 占位符替换 ──▶ 通知
        └──▶ Changelog 差分对比（异步，链接回填）
```

## 技术栈

| 层 | 技术 |
|---|---|
| 语言 | Python 3.9+ |
| Web 后台 | Flask + 原生 HTML/CSS/JS + SSE 实时推送 |
| 飞书 | 官方 Lark CLI（`lark_cli_adapter.py` 封装） |
| 外部系统 | Jenkins、NAS（WebDAV + Synology DSM）、飞书 Open API、DeepSeek LLM |

## 快速开始

### 环境要求

- Python 3（建议 3.9+）
- Windows PowerShell
- `curl.exe` 可用（Windows 10/11 自带）
- 网络可达：Jenkins、NAS、飞书

### 安装

```powershell
# 1. 安装 Web 后台依赖（仅 web_release_server.py 需要）
pip install -r requirements_web.txt

# 2. 初始化飞书 CLI（一次性）
python lark_release.py setup

# 3. 飞书授权（各域名按需）
lark-cli auth login --domain wiki docs im drive base
```

### 运行发布

```powershell
# 完整发布流水线
python lark_release.py run --config gqf_windermere.json

# 仅触发 Jenkins 构建
python lark_release.py trigger --config gqf_windermere.json

# 仅生成飞书文档（跳过构建）
python lark_release.py doc --config gqf_windermere.json

# 启动 Web 管理后台
python web_release_server.py
```

## 目录结构

```
NAS - 优化版 开发版/
├── 核心脚本
│   ├── lark_release.py          # CLI 统一入口
│   ├── web_release_server.py    # Web 管理后台 + 飞书 Bot
│   ├── jenkins_trigger_build.py # Jenkins 构建触发与监控
│   ├── release_pipeline_run.py  # 主流程编排引擎
│   ├── lark_cli_adapter.py      # 飞书 CLI 封装
│   ├── nas_webdav_upload.py     # NAS WebDAV 上传
│   ├── config_loader.py         # 配置继承加载
│   └── credential_resolver.py   # 凭证解析（env > config）
├── 配置文件
│   ├── common.json              # 公共基础配置（Jenkins/NAS/飞书/LLM）
│   ├── gqf_*.json               # 各设备项目配置（base_config 继承）
│   ├── placeholders.default.json# DSL 占位符 + 处理流程定义
│   └── placeholders.schema.json # DSL JSON Schema
├── Web 前端
│   ├── templates/               # HTML 模板
│   └── static/                  # CSS/JS
└── 文档（见下方索引）
```

## 支持的设备

NXP595 平台：stuttgart · toulouse · toulouseh

MHS003 平台：pike · windermere · cologne · geneva · milan · milan_32 · milan_64m · pamir · pamir_32 · pamir_64m · rome · rome_64m

MHS003S 平台：oslo

> 每个设备对应一个 `gqf_<设备名>.json`。新增设备只需添加配置文件，系统自动识别。

## 配置说明

两级分层 JSON 架构：

- **`common.json`** —— 所有项目共享的公共配置（Jenkins / NAS / 飞书 / LLM / Webhook）。
- **`gqf_<项目>.json`** —— 设备独立配置（设备名、版本号、Tag、分支等），通过 `base_config` 继承 common.json。

凭证通过 `credential_resolver.py` 统一解析，支持 `${ENV:VARNAME}` 占位符，优先读环境变量，回退到配置值。

## 文档索引

| 文档 | 内容 |
|---|---|
| [help.md](help.md) | 使用说明（详细命令与环境） |
| [固件发布自动化流水线 — 功能全览.md](固件发布自动化流水线%20—%20功能全览.md) | 十大功能模块总览 |
| [链路方案设计.md](链路方案设计.md) | 端到端链路架构与 API 设计 |
| [CHANGELOG_INTEGRATION_GUIDE.md](CHANGELOG_INTEGRATION_GUIDE.md) | Changelog 差分集成指南 |
| [NAS_LINK_DISPLAY_GUIDE.md](NAS_LINK_DISPLAY_GUIDE.md) | NAS 链接在飞书文档的展示方案 |
| [FEISHU_TEMPLATE_EXAMPLES.md](FEISHU_TEMPLATE_EXAMPLES.md) | 飞书模板占位符示例 |

## 注意事项

- 敏感凭证（Jenkins/NAS 密码、飞书 secret、API key）会随配置文件提交，请确认仓库访问权限可控。
- 飞书 user token 为个人凭证、会过期，团队成员需各自执行 `lark-cli auth login` 授权。
- 运行时产物（`work/`、`output/`、`*.bak.*` 等）已通过 `.gitignore` 排除，不入库。
=======
# active-nas-build-pipeline

## 项目简介

该仓库名称表明其目标可能与 NAS 构建流水线有关，但以当前 `main` 分支的实际内容为准，仓库中**只有一个 GitHub Actions 飞书通知工作流**，尚未包含任何 NAS 构建、上传、分发、调度、配置、脚本或 README 文档。

因此，当前仓库更准确的定位是：**一个尚未放入业务实现的占位仓库**。

## 当前已存在的功能

- 在向 `main` 或 `master` 分支推送时触发通知。
- 支持在 GitHub Actions 页面手动触发通知。
- 复用 `active-ailab/skills-manifest/.github/workflows/reusable-feishu-notify.yml@main` 发送飞书变更消息。

## 适用场景

当前版本只能用于仓库变更通知，不能直接承担 NAS 构建流水线任务。若后续要承载真实流水线，应在本仓库补充：

- 构建入口脚本
- NAS 上传/下载逻辑
- 配置文件与环境变量说明
- 使用文档与验证步骤

## 目录结构

```text
active-nas-build-pipeline/
└── .github/
    └── workflows/
        └── feishu-notify.yml
```

## 使用方法

当前没有可执行的 NAS pipeline 命令。现阶段仅可：

- 推送代码到 `main` / `master`，触发飞书通知；
- 在 GitHub Actions 页面手动运行 `Feishu Notify`。

## 注意事项

- 不应根据仓库名推断这里已经有 NAS 构建逻辑；当前代码并没有。
- 若后续补充真正的流水线实现，应同步更新本 README，避免误导使用者。
>>>>>>> fbd7fc1fb1a2cf3a898f1d0afb86e1cb2d340141

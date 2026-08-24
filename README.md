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

### 凭证配置（运行前必读）

本项目所有真实凭证（Jenkins / NAS 密码、飞书 App Secret、DeepSeek API Key、飞书 Webhook）**已从 `*.json` 配置中移除**，统一通过环境变量注入，不会随代码提交。

首次运行前，必须先在项目根目录创建 `.env` 文件：

```powershell
# 1. 复制模板生成 .env（在项目根目录执行）
copy .env.example .env

# 2. 用编辑器打开 .env，把每项 CHANGE_ME 替换成真实凭证
```

`.env` 里需要填写的变量：

| 变量 | 用途 |
|---|---|
| `JENKINS_PASSWORD` | Jenkins 登录密码 |
| `NAS_WEBDAV_PASSWORD` | NAS WebDAV 密码 |
| `NAS_DSM_PASSWORD` | NAS DSM 密码 |
| `FEISHU_APP_SECRET` | 飞书应用 App Secret |
| `FEISHU_WEBHOOK_URL` | 飞书 Flow 通知 webhook 地址 |
| `DEEPSEEK_API_KEY` | DeepSeek API 密钥 |

`.env` 放在项目根目录（与 `lark_release.py` 同级），启动任一脚本时会自动读取并注入环境变量。

> **重要说明：**
> - `.env` 已被 `.gitignore` 忽略，**不会**进入 git 仓库，切勿把真实凭证提交进去。
> - 没有 `.env` 时运行会因缺少凭证报错（如 `Missing credential` / `LarkCliError`）。
> - `.env` 通过私密渠道（如飞书私聊）分发给需要的同事；NAS 账号仅部分同事有权限，无权限者需向负责人索取 `.env`。

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

凭证通过 `credential_resolver.py` 统一解析，支持 `${ENV:VARNAME}` 占位符，优先读环境变量，回退到配置值。真实凭证存放于 `.env`（见上方「凭证配置」）。

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

- 敏感凭证（Jenkins/NAS 密码、飞书 secret、API key）已外置到 `.env`，**不再随代码提交**，见「凭证配置」。
- 飞书 user token 为个人凭证、会过期，团队成员需各自执行 `lark-cli auth login` 授权。
- 运行时产物（`work/`、`output/`、`*.bak.*` 等）已通过 `.gitignore` 排除，不入库。

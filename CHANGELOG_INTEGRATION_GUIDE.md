# 差分 Changelog 自动集成指南

本仓库已集成“差分 Changelog 自动触发与飞书链接提取”功能。该功能允许在发版流程中自动触发 Jenkins 上的差分任务，并将生成的飞书报告链接自动回填到最终的发版文档中。

## 1. 功能概述

- **异步触发与复用**：在触发 Release/Debug 构建后，系统会提前异步触发 `CMP-JIRA-GIT` 差分任务，并把队列解析出的 build 信息写入 `output/changelog_job.json`，后续流程会优先复用该构建，避免二次触发。
- **智能等待**：在需要时同步轮询 Jenkins 任务状态，直到构建完成（或按配置限时等待/放行）。
- **链接提取**：自动解析 Jenkins 控制台日志，提取形如 `差分报告飞书链接: https://zepp.feishu.cn/...` 的链接。
- **智能超链接 (Hyperlink)**：脚本不再只是简单替换 URL 文本。在飞书 Docx/Wiki 替换时，它会自动将 `{{REL_XXX}}` 转换为一个**真实的超链接**，其点击文本是**实际文件名**，链接地址是 **NAS 分享地址**。
- **文件名占位符**：如果你仍需单独引用文件名，可以使用 `{{REL_XXX_NAME}}`。

## 2. 配置说明 (以 cologne.json 为例)

为了方便维护，推荐将核心参数统一定义在顶部的 `vars` 模块中。

### 2.1 变量定义 (vars)
在文件顶部定义常用的版本和 Tag：
```json
"vars": {
  "prev_version_name": "3.7.0.5",
  "prev_version_code": "202602141532",
  "tag": "cologne_rc5.2",
  "release_version_name": "3.102.0",
  "fct_version_name": "3.102.0",
  "debug_version_name": "3.102.1"
}
```

### 2.2 Changelog 模块
配置触发任务地址、参数映射与等待策略：
```json
"changelog": {
  "enabled": true,
  "jenkins_job_url": "https://jenkins.huami.com/job/DownStream/job/CMP-JIRA-GIT",
  "block_until_ready": true,
  "block_timeout_sec": 0,
  "post_wait_sec": 180,
  "auto_trigger_after_builds": true,
  "params": {
    "PRODUCTION": "${release.project}",
    "BEFORE_VERSION_NAME": "${vars.prev_version_name}",
    "BEFORE_VERSION_CODE": "${vars.prev_version_code}",
    "AFTER_VERSION_NAME": "${release.version}",
    "AFTER_MANIFEST_TAG": "${vars.tag}",
    "DIFF_COMPARE_SKIP_JIRA": "YES"
  }
}
```

字段说明：
- `block_until_ready`：在飞书文档复制完成后，是否阻塞等待差分链接就绪；true 表示阻塞等待，false 表示非阻塞，转为“事后补链”。
- `block_timeout_sec`：阻塞模式下的超时秒数；0 表示不设上限（谨慎使用）。
- `post_wait_sec`：非阻塞模式下，在最终完成前额外等待的最大秒数用于尝试补链（默认 120～180 推荐）。
- `auto_trigger_after_builds`：是否在触发主线构建后即异步触发差分任务（默认 true）。

参数展开：
- 支持在参数值内使用 `${...}` 占位符，按“点路径”从主配置中展开，支持在字符串中出现多处 `${...}`。

### 2.3 执行时序与状态复用
- 触发阶段：`jenkins_trigger_build.py` 在触发 Release/Debug 后异步触发差分任务，并在解析到 build 编号后把状态写入 `output/changelog_job.json`，形如：
  ```json
  { "job_url": ".../CMP-JIRA-GIT/", "build_number": 1234, "build_url": ".../1234/" }
  ```
- 文档阶段：`release_pipeline_run.py` 在构建飞书文档时先尝试复用该 build，直接从其 consoleText 抽取链接，避免重复触发。
- 补链策略：按 `block_until_ready / block_timeout_sec / post_wait_sec` 决定是阻塞等待，还是非阻塞完成后在窗口期内尝试补链。

## 3. 使用方法

### 3.1 飞书模板准备
现在你只需直接使用链接占位符，脚本会自动将其渲染为“带名称的超链接”。

| 功能 | 占位符 | 渲染效果 (飞书) |
| :--- | :--- | :--- |
| **总包** | `{{REL_RELEASE_FULL_ARCHIVE}}` | [archive_cologne_...tgz](https://...) |
| **OTA 包** | `{{REL_RELEASE_OTA_CLOUD_ARCHIVE}}` | [archive_OTA_...tgz](https://...) |
| **其他** | `{{REL_XXX}}` | [filename](https://...) |

**注意**：你不需要在模板中写 `[name](link)`，直接写 `{{REL_RELEASE_FULL_ARCHIVE}}` 即可，脚本会自动处理转换逻辑。

### 3.2 运行流水线
正常运行发版编排脚本即可。脚本会在准备飞书文档时自动执行差分逻辑：
```powershell
python .\release_pipeline_run.py --config .\cologne.json
```

### 3.3 调试模式
如果只想测试差分触发和链接提取，而不产生实际文档，可以使用 `--dry-run`：
```powershell
python .\release_pipeline_run.py --config .\cologne.json --dry-run --skip-download --skip-prepare --skip-upload --skip-share --skip-doc
```
脚本会打印 `Feishu placeholder mapping generated`，你可以在生成的 JSON 文件或控制台日志中查看 `{{REL_CHANGELOG_LINK}}` 是否正确。

### 3.4 阻塞与补链策略建议
- 快速出文档：`block_until_ready=false` + `post_wait_sec=120~180`。文档先出，尽量在窗口期内补链。
- 严格齐活：`block_until_ready=true`，必要时设置 `block_timeout_sec` 防止长时间占用。
- 复用已触发任务：确保未删除 `output/changelog_job.json`，可减少等待时间。

## 4. 关键代码参考

- **触发与解析逻辑**：详见 `_trigger_changelog_job_and_get_feishu_link`（提取 Feishu 链接，自带轮询与控制台解析）。参见 [release_pipeline_run.py](file:///c:/Users/hmnjcs/Desktop/NAS%20-%20%E4%BC%98%E5%8C%96%E7%89%88/release_pipeline_run.py#L2989-L3174)。
- **配置处理与复用**：详见 `_build_changelog_link`（参数展开、复用 `output/changelog_job.json` 状态、择机触发/拉取）。参见 [release_pipeline_run.py](file:///c:/Users/hmnjcs/Desktop/NAS%20-%20%E4%BC%98%E5%8C%96%E7%89%88/release_pipeline_run.py#L3176-L3284)。
- **异步预触发**：差分任务的异步触发和状态落盘逻辑。参见 [jenkins_trigger_build.py](file:///c:/Users/hmnjcs/Desktop/NAS%20-%20%E4%BC%98%E5%8C%96%E7%89%88/jenkins_trigger_build.py#L661-L724) 与调用位置 [jenkins_trigger_build.py](file:///c:/Users/hmnjcs/Desktop/NAS%20-%20%E4%BC%98%E5%8C%96%E7%89%88/jenkins_trigger_build.py#L1042-L1078)。

## 5. 常见问题排查 (Troubleshooting)

### 5.1 Jenkins 403 Forbidden
- **原因**：通常是 CSRF 保护或 Session 丢失。
- **解决**：脚本已内置 `CookieJar` 管理会话。如果依然报错，请在 `cologne.json` 的 `jenkins.auth.password` 中使用 **Jenkins API Token** 替代普通密码。

### 5.2 飞书 OAuth 报错 20014
- **原因**：自建应用 (Internal App) 在换取 User Token 时未携带有效 Tenant Token。
- **解决**：脚本已针对 `cli_` 开头的应用优化了流程，会自动先获取 `tenant_access_token` 进行授权。

### 5.3 找不到飞书链接
- **原因**：Jenkins 日志中没有匹配到正则模式。
- **解决**：确保 Jenkins 任务控制台打印了 `差分报告飞书链接: https://zepp.feishu.cn/...` 这一行。

### 5.4 长时间等待未出结果
- 检查 `output/changelog_job.json` 是否存在并包含最近一次的 build 编号；如果已存在可在浏览器直接打开 `build_url` 查看控制台。
- 若开启了 `block_until_ready=true` 且未设置 `block_timeout_sec`，建议设定超时以避免无限等待。

### 5.5 文档已生成但链接为空
- 非阻塞策略下属于正常现象，系统会在 `post_wait_sec` 窗口内尝试补链。
- 若窗口期仍未补上，请核对 Jenkins 控制台是否确实输出了目标行；必要时手动把链接写入飞书文档对应位置或重新运行文档替换步骤（仅替换 `{{REL_CHANGELOG_LINK}}`）。

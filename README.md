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

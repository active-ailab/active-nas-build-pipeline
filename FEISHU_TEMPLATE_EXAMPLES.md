# 飞书模板改进示例

## 飞书文档中的占位符使用示例

### 示例 1：简洁版本（推荐日常使用）

```
## 版本信息
设备: {{REL_DEVICE_NAME}}
阶段: {{REL_STAGE}}
版本: {{REL_VERSION}}

## CI/CD 信息
编译链接: {{REL_CI_BUILD_URL}}

## 下载资源

### Release 包

**完整档案** ({{REL_RELEASE_FULL_ARCHIVE_FILENAME}})
{{REL_RELEASE_FULL_ARCHIVE}}

**OTA 云端包** ({{REL_RELEASE_OTA_CLOUD_ARCHIVE_FILENAME}})
{{REL_RELEASE_OTA_CLOUD_ARCHIVE}}

**OTA 签名包** ({{REL_RELEASE_OTA_SIGN_ZIP_FILENAME}})
{{REL_RELEASE_OTA_SIGN_ZIP}}

### 调试包 (Monkey)

**完整档案** ({{REL_MONKEY_FULL_ARCHIVE_FILENAME}})
{{REL_MONKEY_FULL_ARCHIVE}}

### 工具和文档

**Factory 软件** ({{REL_RELEASE_FACTORY_TOOL_ZIP_FILENAME}})
{{REL_RELEASE_FACTORY_TOOL_ZIP}}

**校验文件** ({{REL_MD5SUM_FILE_FILENAME}})
{{REL_MD5SUM_FILE}}

**编译日志** ({{REL_BUILD_LOG_FILE_FILENAME}})
{{REL_BUILD_LOG_FILE}}

## Manifest 信息

**Bootloader**: {{REL_BOOTLOADER_MANIFEST_FILENAME}}
{{REL_BOOTLOADER_MANIFEST_INFO}}

**Recovery**: {{REL_RECOVERY_MANIFEST_FILENAME}}
{{REL_RECOVERY_MANIFEST_INFO}}

## 标签信息
- APP 标签: {{REL_APP_TAG}}
- Bootloader 标签: {{REL_BOOT_TAG}}
- Recovery 标签: {{REL_RECOVERY_TAG}}
- FCT 标签: {{REL_FCT_TAG}}
```

### 示例 2：链接优先版本（强调下载）

```
## {{REL_DEVICE_NAME}} {{REL_STAGE}} {{REL_VERSION}} - 下载中心

| 文件 | 下载链接 |
|-----|--------|
| {{REL_RELEASE_FULL_ARCHIVE_FILENAME}} | {{REL_RELEASE_FULL_ARCHIVE}} |
| {{REL_RELEASE_OTA_CLOUD_ARCHIVE_FILENAME}} | {{REL_RELEASE_OTA_CLOUD_ARCHIVE}} |
| {{REL_RELEASE_OTA_SIGN_ZIP_FILENAME}} | {{REL_RELEASE_OTA_SIGN_ZIP}} |
| {{REL_MONKEY_FULL_ARCHIVE_FILENAME}} | {{REL_MONKEY_FULL_ARCHIVE}} |
| {{REL_RELEASE_FACTORY_TOOL_ZIP_FILENAME}} | {{REL_RELEASE_FACTORY_TOOL_ZIP}} |
| {{REL_MD5SUM_FILE_FILENAME}} | {{REL_MD5SUM_FILE}} |
| {{REL_BUILD_LOG_FILE_FILENAME}} | {{REL_BUILD_LOG_FILE}} |
```

### 示例 3：超链接版本（飞书原生，最美观）

在飞书模板中，对每个文件名占位符做如下处理：

1. **步骤**：
   - 在模板中输入：`{{REL_RELEASE_OTA_SIGN_ZIP_FILENAME}}`
   - 选中这段文本
   - 点击菜单 → 样式 → 超链接（或 Ctrl+K）
   - URL 栏输入：`{{REL_RELEASE_OTA_SIGN_ZIP}}`
   - 保存

2. **结果**：
   - 模板替换后，用户看到的是可点击的链接
   - 链接显示文本是文件名（如 `watch@mhs003_ota_sign.zip`）
   - 链接指向 NAS 上的实际文件

3. **示例模板效果**（飞书中实际显示）：
```
下载 OTA 签名包

[watch@mhs003_ota_sign.zip]  ← 这是一个可点击的链接
```

## 模板修改检查清单

- [ ] 识别所有需要改进的链接占位符
- [ ] 为每个链接占位符添加文件名占位符（如 `{{XXXXX_FILENAME}}` + `{{XXXXX}}`）
- [ ] 修改飞书模板，使用上述某个示例格式
- [ ] 在 `placeholders.default.json` 中确认对应的占位符定义已存在
- [ ] 运行一次 `--dry-run` 验证占位符生成
- [ ] 查看 `output/` 中的占位符映射文件，确认文件名被正确提取
- [ ] 执行完整的发布流程，验证飞书文档中的显示效果

## 常见模板误区

#### ❌ 不好的做法
```
OTA 签名:
https://10.2.100.85:5006/GT智能手表事业部/...very-long-url...
```
**问题**：只有 URL，看不出这是什么文件

#### ✅ 好的做法（带文件名）
```
OTA 签名: {{REL_RELEASE_OTA_SIGN_ZIP_FILENAME}}
https://10.2.100.85:5006/GT智能手表事业部/...
```
**优势**：一看就知道是 OTA 签名包，而且显示实际的文件名

## 多设备配置示例

如果你有多个设备配置（cologne.json, windermere.json, 等），建议：

1. **创建通用模板**：使用占位符方式，不硬编码设备特定信息
2. **占位符保持一致**：所有设备都使用相同的占位符名称
3. **placeholders.default.json**：已支持所有这些占位符，跨设备项目复用

示例：
```
# cologne.json
feishu.template_file_token = "..."
feishu.docx_target_folder_token = "..."

# windermere.json  
feishu.template_file_token = "..."  # 不同的模板，但占位符相同
feishu.docx_target_folder_token = "..."

# 两个配置都会自动使用相同的占位符定义
```

## 故障排查

如果占位符生成后文件名为空：

1. **检查 prepare 流程**：
   ```powershell
   python release_pipeline_run.py --config cologne.json --skip-upload --skip-feishu --dry-run
   ```
   查看是否正确提取了文件

2. **检查占位符映射文件**：
   ```powershell
   # 查看最近的映射文件
   Get-Content ./output/feishu_placeholder_mapping_*.json | ConvertFrom-Json
   ```

3. **启用调试输出**：
   在 `release_pipeline_run.py` 中，占位符生成时会打印到标准输出
   查看是否有 WARN 或 ERROR 信息

---

**创建日期**: 2026-02-25

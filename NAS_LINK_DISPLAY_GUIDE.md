# NAS 链接在飞书文档中的改进方案

## 问题
目前 NAS 生成的链接放到飞书文档后，只显示裸露的 URL，看不出是什么文件的链接。

## 解决方案

我已经为你的系统添加了**文件名占位符支持**。现在每个链接占位符都对应一个文件名占位符。

### 新增的占位符对

| 链接占位符 | 文件名占位符 |
|-----------|-----------|
| `{{REL_RELEASE_OTA_SIGN_ZIP}}` | `{{REL_RELEASE_OTA_SIGN_ZIP_FILENAME}}` |
| `{{REL_RELEASE_OTA_CLOUD_ARCHIVE}}` | `{{REL_RELEASE_OTA_CLOUD_ARCHIVE_FILENAME}}` |
| `{{REL_RELEASE_FULL_ARCHIVE}}` | `{{REL_RELEASE_FULL_ARCHIVE_FILENAME}}` |
| `{{REL_RELEASE_FACTORY_TOOL_ZIP}}` | `{{REL_RELEASE_FACTORY_TOOL_ZIP_FILENAME}}` |
| `{{REL_BOOTLOADER_MANIFEST_INFO}}` | `{{REL_BOOTLOADER_MANIFEST_FILENAME}}` |
| `{{REL_RECOVERY_MANIFEST_INFO}}` | `{{REL_RECOVERY_MANIFEST_FILENAME}}` |
| `{{REL_MONKEY_FULL_ARCHIVE}}` | `{{REL_MONKEY_FULL_ARCHIVE_FILENAME}}` |
| `{{REL_MD5SUM_FILE}}` | `{{REL_MD5SUM_FILE_FILENAME}}` |
| `{{REL_BUILD_LOG_FILE}}` | `{{REL_BUILD_LOG_FILE_FILENAME}}` |
| 其他所有 NAS 链接 | `{{其他_FILENAME}}` |

### 在飞书模板中的使用方式

#### 方案 A：显示"文件名: 链接"（最简单）

在飞书模板中改为：

```
{{REL_RELEASE_OTA_SIGN_ZIP_FILENAME}}: {{REL_RELEASE_OTA_SIGN_ZIP}}
```

效果显示为：
```
watch@mhs003_ota_sign.zip: [点击下载]
```

（链接由飞书自动生成）

#### 方案 B：使用飞书超链接（推荐）

1. **在飞书模板中创建超链接**：
   - 选中占位符文本（比如 `{{REL_RELEASE_OTA_SIGN_ZIP_FILENAME}}`）
   - 点击"插入链接"或用快捷键
   - 在 URL 字段中输入 `{{REL_RELEASE_OTA_SIGN_ZIP}}`
   - 这样生成的链接会有显示文本（文件名）和实际的 URL

2. **示例 Markdown 格式**（如果你的模板支持）：
   ```
   [{{REL_RELEASE_OTA_SIGN_ZIP_FILENAME}}]({{REL_RELEASE_OTA_SIGN_ZIP}})
   ```

#### 方案 C：详细版本

显示完整信息，包括文件类型说明：

```
• OTA签名包: {{REL_RELEASE_OTA_SIGN_ZIP_FILENAME}}
  下载链接: {{REL_RELEASE_OTA_SIGN_ZIP}}

• 完整档案: {{REL_RELEASE_FULL_ARCHIVE_FILENAME}}
  下载链接: {{REL_RELEASE_FULL_ARCHIVE}}
```

## 技术细节

### 变更内容汇总

1. **placeholders.default.json**
   - 为每个 `nas_share` 模式的占位符添加了对应的 `filename_for_share` 占位符定义
   - 新模式会自动提取文件的名称部分

2. **release_pipeline_run.py**
   - 修改了 `_build_placeholder_replacements_from_dsl()` 函数：
     - 添加 `_filename_for_share` 字典来跟踪每个占位符的文件名
     - 在处理 `nas_share` 模式时，自动保存文件名
     - 添加 `filename_for_share` 模式支持，用于返回已保存的文件名

### 工作流程

```
Jenkins 构建
    ↓
下载制品 (local_dir)
    ↓
[准备阶段] → 提取文件名 → 保存到 _filename_for_share
    ↓
[上传到 NAS] → 生成共享链接
    ↓
[占位符替换]
  ├─ {{REL_RELEASE_OTA_SIGN_ZIP}} → (URL)
  └─ {{REL_RELEASE_OTA_SIGN_ZIP_FILENAME}} → (文件名)
    ↓
[飞书文档替换] → 显示为"文件名: URL"或超链接
```

## 立即使用

### 1. 查看生成的占位符映射

运行完整发布流程后，你会在 `output/` 目录下看到文件：
```
feishu_placeholder_mapping_cologne_RC3_3.2.0.1_20260125_*.json
```

打开这个文件，你会看到：
```json
{
  "{{REL_RELEASE_OTA_SIGN_ZIP_FILENAME}}": "watch@mhs003_ota_sign.zip",
  "{{REL_RELEASE_OTA_SIGN_ZIP}}": "https://10.2.100.85:5006/...",
  "{{REL_RELEASE_OTA_CLOUD_ARCHIVE_FILENAME}}": "archive_OTA_CLOUD_xxxxx.tgz",
  "{{REL_RELEASE_OTA_CLOUD_ARCHIVE}}": "https://10.2.100.85:5006/...",
  ...
}
```

### 2. 更新飞书模板

编辑你的飞书模板，用刚才提到的任意一种方式（方案 A/B/C）来改进链接的展示。

### 3. 测试

运行一次 `--dry-run` 来验证占位符是否正确生成：

```powershell
python release_pipeline_run.py --config cologne.json --dry-run
```

查看输出中的占位符映射部分。

## 常见问题

**Q: 文件名占位符为什么是空的？**
A: 可能是因为：
- 对应的链接占位符没有找到文件（检查文件是否真的被准备出来了）
- 文件名提取失败（检查 prepare 流程）
- 运行的模式（debug vs release）不匹配

**Q: 我只想在某些占位符上使用文件名，不需要所有的？**
A: 完全可以。只需在你的飞书模板中使用你需要的占位符组合。未使用的占位符会被忽略。

**Q: Markdown 超链接格式在飞书中不工作？**
A: 飞书的文本替换是纯字符串替换，不会解析 Markdown。请使用方案 A（文本格式）或方案 B（飞书原生超链接）。

## 示例：改进前后对比

### 改进前（只有 URL）
```
Release 档案: https://10.2.100.85:5006/GT智能手表事业部/...很长的路径.../archive_cologne_xxxx.tgz

OTA 签名包: https://10.2.100.85:5006/GT智能手表事业部/...很长的路径.../watch@mhs003_ota_sign.zip
```
💡 问题：长长的URL，看不出是什么文件

### 改进后（文件名 + URL）
```
Release 档案: archive_cologne_xxxxx.tgz
OTA 签名包: watch@mhs003_ota_sign.zip
Factory 工具: factory_tool_v1.0.zip
```

（实际链接由占位符自动填充）

## 后续优化

未来可能的增强：
1. **支持自定义显示格式**（比如 `[{filename}]({url})`）
2. **Windows 风格文件信息**（显示文件大小、修改时间等）
3. **飞书 Docx API 增强**：使用原生超链接块而不是纯文本替换
4. **批量操作**：为各个制品自动创建飞书列表或表格格式

---

**更新日期**: 2026-02-25
**相关文件**: placeholders.default.json, release_pipeline_run.py

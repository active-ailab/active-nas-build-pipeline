# 飞书文档生成自动授权配置指南

## 问题描述
每次运行文档生成时都需要手动点击浏览器授权页面，导致流程中断。

## 根本原因
你的飞书用户令牌（`user_access_token`）已过期，系统需要重新获取新的令牌。当前配置中：
- ✗ 缺少 `user_refresh_token`（刷新令牌）
- ✗ 没有启用自动刷新机制

## ✅ 完整解决方案

### 方案 1：一键生成令牌（推荐，5 分钟）

**第一步：运行令牌生成脚本**

```powershell
python feishu_auto_refresh_token.py --config gqf_windermere.json
```

**脚本会自动：**
1. 启动本地浏览器，弹出飞书授权页面
2. 在页面点击一次"授权"按钮（仅此一次）
3. 自动获取用户访问令牌和刷新令牌
4. 保存到配置文件，之后自动更新

**完成后配置文件会更新为：**
```json
"feishu": {
  "user_access_token": "新令牌",
  "user_refresh_token": "刷新令牌",
  "user_access_token_expires_at": 1234567890,
  "user_refresh_token_expires_at": 1234567890,
  "oauth": {
    "auto_save_user_access_token": true,
    "auto_oauth_on_99991677": true
  }
}
```

### 方案 2：手动更新令牌（如果脚本失败）

如果脚本无法运行，手动执行以下步骤：

1. **清除过期令牌**
```powershell
# 删除缓存
Remove-Item .feishu_tenant_access_token.json -ErrorAction SilentlyContinue
```

2. **添加环境变量（可选）**
```powershell
# 在 PowerShell 中设置
$env:FEISHU_USER_ACCESS_TOKEN = "你的新令牌"
```

3. **下次运行时系统会自动触发 OAuth 授权**
```powershell
python release_pipeline_run.py --config gqf_windermere.json --dry-run
```

## 🔄 自动刷新工作原理

启用刷新令牌后，系统会：

| 情况 | 处理 |
|------|------|
| 访问令牌有效 | ✓ 直接使用，无需授权 |
| 访问令牌过期 | ✓ 使用刷新令牌自动更新 |
| 刷新令牌快要过期（90天） | ✓ 自动触发 OAuth 重新授权 |
| OAuth 禁用且令牌过期 | ✗ 需要手动更新配置 |

## ⚙️ 配置文件要求

确保配置中包含以下内容：

```json
{
  "feishu": {
    "enabled": true,
    "user_access_token": "u-...",
    "user_refresh_token": "ur-...",
    "user_access_token_expires_at": 1234567890,
    "user_refresh_token_expires_at": 1234567890,
    "oauth": {
      "enabled": true,
      "app_id": "cli_...",
      "app_secret": "...",
      "scopes": "wiki:wiki wiki:node:read wiki:node:copy ... offline_access",
      "open_browser": true,
      "auto_save_user_access_token": true,
      "auto_oauth_on_99991677": true
    }
  }
}
```

**关键字段说明：**
- `scopes` 必须包含 `offline_access` 才能获得刷新令牌
- `auto_save_user_access_token: true` 会自动保存新令牌到配置
- `auto_oauth_on_99991677: true` 在令牌过期时自动重新授权

## 📋 所有配置文件更新

建议对所有设备配置应用此改动：

```bash
# 复制改动到其他配置
for file in gqf_*.json; do
  if [ "$file" != "gqf_windermere.json" ]; then
    python feishu_auto_refresh_token.py --config "$file"
  fi
done
```

## 🛠️ 故障排除

### 问题：还是提示文件不存在
**解决：** 确保在正确的目录运行脚本
```powershell
cd "c:\Users\hmnjcs\Desktop\NAS - 优化版"
python feishu_auto_refresh_token.py --config gqf_windermere.json
```

### 问题：授权页面打不开
**解决：** 手动打开飞书授权链接，或检查防火墙/代理设置

### 问题：脚本超时
**解决：** 确保在授权页面点击了"授权"按钮，页面会自动关闭

### 问题：令牌仍然过期
**解决：** 
- 检查飞书app是否被禁用
- 验证 `app_id` 和 `app_secret` 是否正确
- 手动删除配置文件中的过期令牌

## ✨ 完成标志

当你看到以下输出时，说明配置成功：

```
✓ 已获得授权码
✓ 已获得访问令牌 (有效期 7200s)
✓ 已获得刷新令牌
✓ 已备份原配置到: gqf_windermere.json.bak
✓ 已更新配置文件: gqf_windermere.json

配置已更新完成！下次生成文档时将使用新令牌。
刷新令牌最多可用 90 天，之后会自动重新授权。
```

## 下一步

运行以下命令生成文档（从此不需要手动授权）：

```powershell
python release_pipeline_run.py --config gqf_windermere.json
```

---

**注意：** 这个配置将在 90 天后自动触发重新授权（系统自动进行），无需人工干预。

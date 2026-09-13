# Restream Studio Windows 安装器与自动更新设计

## 目标

把当前 PyInstaller onedir 发行目录封装为标准 Windows 安装程序，提供桌面与开始菜单快捷方式、Windows 卸载入口、路径绑定的本机回环防火墙规则，以及基于公开 GitHub Releases 的无密钥自动更新能力。

公开更新源固定为 `algz-glitch/restream-studio`。首个安装器版本为 `0.1.0`，目标文件名为 `RestreamStudio-Setup-0.1.0.exe`。

## 安装模型

- 使用 Inno Setup 生成 64 位 per-user 安装器。
- 默认目录：`%LocalAppData%\Programs\RestreamStudio`。
- 安装整个 PyInstaller onedir，不能只安装主 EXE。
- 创建桌面快捷方式和开始菜单目录。
- 在 Windows“已安装的应用”中登记卸载入口。
- 安装完成后可选择立即启动。
- 升级安装覆盖程序文件，但保留 `%LocalAppData%\RestreamStudio` 中的数据库、日志和待机数据。
- 安装和升级时重建 `RestreamStudio-Installed-Localhost` 防火墙规则；该规则只允许已安装 EXE 的 IPv4 `127.0.0.1` 入站 TCP。
- 卸载时删除程序、快捷方式和防火墙规则，默认保留用户数据。卸载向导提供“同时删除用户数据”任务，只有用户显式勾选才删除。

## 自动更新架构

### 发布资产

每个 GitHub Release 包含：

- `RestreamStudio-Setup-<version>.exe`
- `latest.json`

`latest.json` 使用严格 JSON 合约：

```json
{
  "schema_version": 1,
  "version": "0.1.1",
  "installer_url": "https://github.com/algz-glitch/restream-studio/releases/download/v0.1.1/RestreamStudio-Setup-0.1.1.exe",
  "sha256": "64位大写十六进制摘要",
  "size": 123456,
  "published_at": "2026-09-13T18:00:00Z",
  "release_url": "https://github.com/algz-glitch/restream-studio/releases/tag/v0.1.1"
}
```

更新器只接受 HTTPS、`github.com` 主机、指定仓库的 Release 下载路径、严格 SemVer 和有效 SHA-256。任何字段异常、降级版本、摘要不一致、下载不完整或超时均停止更新，不执行文件。

### 检查与交互

- 后端启动后异步检查更新，不阻塞本地服务启动。
- 默认检查间隔 24 小时，结果写入用户数据目录；网络失败采用有界超时，不频繁重试。
- 前端显示“已是最新版”“发现新版本”“下载中”“待安装”“检查失败”状态。
- 用户可手动点击“检查更新”。
- 发现新版本后显示版本号和 Release 页面链接。
- 下载完成并验证 SHA-256 后，由用户点击“安装并重启”。不在直播运行中自动安装；运行中按钮禁用并提示先停止全部输出。
- 应用调用独立更新助手。助手等待当前进程退出，再以 Inno Setup 静默升级参数执行安装器并重新启动已安装程序。
- 更新检查无 GitHub Token，不读取浏览器 Cookie，也不在程序内保存 GitHub 凭据。

## 组件边界

1. `update/contracts.py`：严格解析更新清单和 SemVer。
2. `update/client.py`：有界 HTTPS 请求、GitHub URL 白名单、流式下载和 SHA-256 校验。
3. `update/service.py`：检查频率、状态机、并发互斥和持久化。
4. `update/helper.py`：等待主进程、调用安装器、重启应用；不承担网络下载。
5. API：读取更新状态、手动检查、下载、安装四个端点，继续使用现有 Origin、会话令牌和本地 Host 防护。
6. 前端：设置/更新卡片，显示真实状态、错误、进度和操作按钮。
7. `packaging/restream-studio.iss`：安装、快捷方式、卸载、防火墙和数据保留策略。
8. `.github/workflows/release.yml`：Windows 构建、全门禁、安装器编译、摘要与清单生成、Release 上传。

## 失败处理与回滚

- 检查失败不影响直播和本地配置。
- 下载写入临时 `.partial` 文件；摘要通过后原子改名。
- 安装前保留当前用户数据，不迁移或重写推流密钥。
- Inno Setup 升级失败时旧安装目录由安装器事务机制恢复；更新器报告失败，不删除已下载日志证据。
- API 和日志不返回更新下载中的敏感本地路径，只返回固定诊断码。
- GitHub API 限流时记录 `rate_limited`，等待下一周期或用户手动重试。

## 发布和验收

### 自动化验证

- 更新清单解析、版本比较、URL 白名单、摘要错误、超时和并发测试。
- API 鉴权、状态转换、直播中禁止安装、响应脱敏测试。
- 前端更新卡片的空闲、可用、下载、失败和待安装测试。
- Inno 脚本静态合约测试。
- GitHub Actions 工作流静态合约测试。
- 继续通过现有 Python、TypeScript、前端、双路本地 RTMP、断源恢复和冻结包门禁。

### Windows 真实烟测

1. 在干净临时安装目录执行首次安装。
2. 验证桌面、开始菜单和卸载入口。
3. 验证主程序启动、浏览器会话、配置保存和端口释放。
4. 验证防火墙规则仅指向安装目录 EXE 且仅限 IPv4 loopback。
5. 构造本地受控 `0.1.1` Release 等价服务，验证发现、下载、摘要校验、安装阻断和升级状态机。
6. 执行升级安装并确认配置保留。
7. 执行卸载并确认程序、快捷方式和防火墙规则删除，用户数据按所选策略保留或删除。

真实 GitHub Release 的最终发布按钮、GitHub 登录确认和可能出现的验证码由用户接管。未取得代码签名证书前，安装器和主 EXE 均明确标记为未签名，不宣称通过 SmartScreen 信任验证。

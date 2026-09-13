# Restream Studio

Restream Studio 是仅监听 `127.0.0.1` 的 Windows 本地转播工作台。它读取操作者有权使用的抖音直播源，并将内容独立推送到抖音与微信视频号目标。所有真实开播、凭据申请和平台验收都由账号持有人完成。

## 安装与启动

### 标准 Windows 安装器

1. 从 GitHub Releases 下载 `RestreamStudio-Setup-<version>.exe`，运行后默认安装到
   `%LOCALAPPDATA%\Programs\RestreamStudio`。
2. 安装器创建桌面与开始菜单快捷方式，并在 Windows“已安装的应用”中登记卸载入口。
3. 安装过程只为已安装的 `RestreamStudio.exe` 配置 IPv4 `127.0.0.1` 入站规则；防火墙步骤需要管理员确认，应用本身仍按当前用户安装和运行。
4. 数据默认写入 `%LOCALAPPDATA%\RestreamStudio`。升级与默认卸载均保留数据；只有在卸载向导中显式选择删除用户数据时才清理。

安装器和主程序当前均**未签名**，Windows SmartScreen 可能显示“未知发布者”。应从仓库的
GitHub Release 页面下载，并按同一 Release 中 `latest.json` 的 `size` 和 `sha256` 核对资产。

### 便携目录分发包

1. 将整个 `RestreamStudio` 目录解压到固定位置；不要只复制 EXE。
2. 双击 `RestreamStudio.exe`，或在 PowerShell 中运行它；便携模式可运行
   `Enable-Localhost.cmd` 配置仅绑定该 EXE 的 loopback 防火墙规则。
3. 浏览器打开 `http://127.0.0.1:8000`。服务不监听局域网地址。
4. 数据默认写入 `%LOCALAPPDATA%\RestreamStudio`，不会写回程序目录。

开发环境使用 `powershell -ExecutionPolicy Bypass -File scripts/dev.ps1`。脚本要求 Python 3.12 虚拟环境、真实 `package-lock.json` 和可复现的 `npm ci`。FFmpeg/ffprobe 的解析顺序为环境变量 `FFMPEG_PATH`/`FFPROBE_PATH`、仓库工具目录、最后 `PATH`。

## 操作流程

1. 在“来源”中输入自己有权转播的抖音直播间 URL，保存并检查在线状态。
2. 分别配置抖音和微信视频号目标；先点击测试，再启用目标。
3. 启动转播，监控两个目标的状态；单个目标重连或停止不会主动停止另一个目标。
4. 结束时先在工作台停止输出，再到平台控制台确认直播已经结束。

### 获取官方 RTMP 凭据

- **抖音**：登录官方直播伴侣/创作者直播控制台，为本次直播取得服务器地址与串流密钥。
- **微信视频号**：登录视频号助手，在直播推流设置中取得服务器地址与串流密钥。

平台是否向某账号开放 RTMP、凭据有效期及直播权限由平台决定。不要从第三方购买或共享密钥。只在应用表单中输入，禁止写入 `.env`、脚本、截图、工单或 shell 历史。

## local-test 与验收状态

`local-test` 只允许 loopback 目标，用于 MediaMTX + FFmpeg 的本机链路测试：

```powershell
$env:FFMPEG_PATH='C:\path\to\ffmpeg.exe'
$env:FFPROBE_PATH='C:\path\to\ffprobe.exe'
$env:MEDIAMTX_PATH='C:\path\to\mediamtx.exe'
powershell -ExecutionPolicy Bypass -File scripts/e2e-local.ps1
```

- `LOCAL_CHAIN_PENDING_PLATFORM_ACCEPTANCE_PENDING`：本地完整链路或真实平台验收至少一项未完成；当前仓库保持此状态。
- `LOCAL_CHAIN_VERIFIED_PLATFORM_ACCEPTANCE_PENDING`：本地完整链路已验证，真实双平台验收尚未全部通过。
- `PLATFORM_ACCEPTED`：仅当 `scripts/validate_platform_acceptance.py` 对真实填写的 `result.json` 返回成功时成立。

以上是 `result.json.status` 的完整复合枚举；本地链路维度不等于最终状态。`LOCAL_CHAIN`
**不等于** `PLATFORM_ACCEPTANCE`，本地 HTTP `/health` 成功也不代表平台已收流。验收结构与
执行命令见 `docs/acceptance/platform-checklist.md`；仓库不提供或提交通过结果样例。

## 待机媒体

构建脚本用 FFmpeg 在临时构建目录确定性生成 `default-standby.mp4`，不会向 Git 提交大二进制。分发包将它放在 `_internal\defaults`。操作者选择自定义待机媒体时，应使用本地 MP4，并先确认分辨率、H.264/AAC 编码、音量和版权。用户媒体留在数据目录，不随升级包导出。

## 日志与故障定位

运行数据位于 `%LOCALAPPDATA%\RestreamStudio`：`data` 保存数据库和待机文件，`logs` 保存本地日志。先检查 `/health`，再检查来源、目标状态和事件页。日志或支持材料发出前必须再次检查 RTMP URL、Cookie、Authorization、串流密钥和本地用户名路径。

## 备份与无秘密导出

停止应用后再备份。配置数据库包含由当前 Windows 用户 DPAPI 保护的目标密钥，不能作为“无秘密”导出文件发送。无秘密导出只记录：应用版本、来源平台名称、目标类型、启用状态、时间戳、脱敏事件和测试结论；不要复制 SQLite、cookies、`.env`、密钥、日志原文或用户媒体。恢复真实目标时由账号持有人重新输入官方凭据。

## 自动更新、回滚与卸载

1. **检查**：应用从公开地址
   `https://github.com/algz-glitch/restream-studio/releases/latest/download/latest.json`
   检查更新。清单必须通过 schema 1、严格 SemVer、仓库 URL、文件大小和 SHA-256 校验。
2. **下载**：应用不需要 GitHub Token，不读取浏览器 Cookie，也不保存 GitHub 凭据。安装器先写入临时文件，完整校验后才进入“待安装”状态。
3. **安装**：先停止全部输出，再点击“安装并重启”。直播运行时更新安装会被阻止。独立更新助手等待主进程退出后执行安装器。
4. **升级前备份**：把 `%LOCALAPPDATA%\RestreamStudio` 备份到仅当前用户可读的位置，并保留上一版安装器及其 SHA-256。
5. **回滚**：停止新版本，运行保留的旧版安装器；只有数据格式兼容性出现问题时才恢复对应版本的数据备份。不要让两个版本同时访问同一数据库。
6. **卸载**：从 Windows“已安装的应用”卸载。程序、快捷方式和安装版防火墙规则会被移除；用户数据默认保留，可在卸载向导中显式选择同时删除。

## 构建与验证

```powershell
powershell -ExecutionPolicy Bypass -File scripts/package.ps1
powershell -ExecutionPolicy Bypass -File scripts/build-installer.ps1 -Clean
powershell -ExecutionPolicy Bypass -File scripts/verify.ps1
```

`package.ps1` 强制先执行 `npm ci` 和前端构建，再运行 PyInstaller 目录打包。分发内容包括前端 assets、ffmpeg/ffprobe、默认待机媒体、licenses 和 metadata；不复制 fixtures、`.env`、数据库、cookies、keys、`.git` 或 logs，更不会复制用户密钥。`verify.ps1` 任一门禁失败都会输出该项 `=FAIL` 并立即非零退出。

依赖锁门禁会检查所有实际 npm 包均包含 `version`、官方 registry `resolved` 与 SHA-512
`integrity`，并确认 Windows x64 所需的 esbuild/Rollup 可选运行时存在。可在不访问网络的
前提下运行 `npm ci --dry-run --offline --ignore-scripts` 检查当前锁与缓存的安装闭包。

## 发布

- `.github/workflows/release.yml` 只响应严格 `v<major>.<minor>.<patch>` tag；普通分支 push 不发布。
- tag 必须与 `pyproject.toml`、`package.json` 和 Inno Setup 版本完全一致。
- Windows CI 固定 Python 3.12、Node.js 22.14.0，并通过 `package-lock.json` 执行 `npm ci`。
- CI 不依赖 runner 的 FFmpeg、ffprobe 或 MediaMTX PATH：`scripts/provision-release-tools.ps1`
  下载固定版本归档并先校验固定 SHA-256，再运行完整 `scripts/verify.ps1` 和安装器构建。
- 所有门禁通过后，CI 上传安装器与 `latest.json` 为 workflow artifact，并使用 GitHub Actions
  的短期仓库令牌将两项资产附加到对应 GitHub Release。令牌只存在于 CI 发布步骤，不进入应用、安装器或更新清单。
- 发布者先更新三处版本并合并经过验证的提交，再由仓库维护者创建并推送签名或受保护的
  `v*` tag。不要为测试创建 tag；推送 tag 会触发真实 Release 发布。

## 已知限制

- 当前自动化不代替真实平台验收，也不能保证平台长期开放 RTMP 权限或接口行为不变。
- 本地开发 E2E 依赖独立的 MediaMTX；它不是桌面运行时的一部分。发布 CI 会临时下载并校验它，只用于门禁。
- 安装器和主 EXE 尚无代码签名；Windows SmartScreen 信任未验收，未知发布者提示仍可能出现。
- 默认待机文件随包提供，但具体待机选择和平台端画面仍需操作者确认。
- 包仅面向构建时验证的 Windows x64 环境；跨架构运行未验收。

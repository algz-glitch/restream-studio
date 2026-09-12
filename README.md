# Restream Studio

Restream Studio 是仅监听 `127.0.0.1` 的 Windows 本地转播工作台。它读取操作者有权使用的抖音直播源，并将内容独立推送到抖音与微信视频号目标。所有真实开播、凭据申请和平台验收都由账号持有人完成。

## 安装与启动

### 目录分发包

1. 将整个 `RestreamStudio` 目录解压到固定位置；不要只复制 EXE。
2. 双击 `RestreamStudio.exe`，或在 PowerShell 中运行它。
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

- `LOCAL_CHAIN_VERIFIED`：仅证明本机来源、双输出、隔离、待机和恢复链路通过。
- `PLATFORM_ACCEPTANCE_PENDING`：尚未使用账号持有人的官方 RTMP 凭据完成真实平台验证。
- `PLATFORM_ACCEPTED`：只有抖音与视频号平台预览、音视频、持续推流及断线恢复均由人工确认后才能使用。

因此 `LOCAL_CHAIN` **不等于** `PLATFORM_ACCEPTANCE`，本地 HTTP `/health` 成功也不代表平台已收流。

## 待机媒体

构建脚本用 FFmpeg 在临时构建目录确定性生成 `default-standby.mp4`，不会向 Git 提交大二进制。分发包将它放在 `_internal\defaults`。操作者选择自定义待机媒体时，应使用本地 MP4，并先确认分辨率、H.264/AAC 编码、音量和版权。用户媒体留在数据目录，不随升级包导出。

## 日志与故障定位

运行数据位于 `%LOCALAPPDATA%\RestreamStudio`：`data` 保存数据库和待机文件，`logs` 保存本地日志。先检查 `/health`，再检查来源、目标状态和事件页。日志或支持材料发出前必须再次检查 RTMP URL、Cookie、Authorization、串流密钥和本地用户名路径。

## 备份与无秘密导出

停止应用后再备份。配置数据库包含由当前 Windows 用户 DPAPI 保护的目标密钥，不能作为“无秘密”导出文件发送。无秘密导出只记录：应用版本、来源平台名称、目标类型、启用状态、时间戳、脱敏事件和测试结论；不要复制 SQLite、cookies、`.env`、密钥、日志原文或用户媒体。恢复真实目标时由账号持有人重新输入官方凭据。

## 升级、回滚与卸载

1. **升级前**：停止全部输出和 EXE；备份 `%LOCALAPPDATA%\RestreamStudio` 到仅当前用户可读的位置，并记录旧程序目录版本。
2. **升级**：解压到新目录，不覆盖正在使用的旧目录；启动新版本并先检查 `/health` 和配置，再做 `local-test`。
3. **回滚**：停止新版本，恢复升级前的数据目录备份，然后从保留的旧程序目录启动。不要让两个版本同时访问同一数据库。
4. **卸载**：停止应用，删除程序目录。确认不再需要配置后，再手工删除 `%LOCALAPPDATA%\RestreamStudio`；保留该目录会保留本机配置。

## 构建与验证

```powershell
powershell -ExecutionPolicy Bypass -File scripts/package.ps1
powershell -ExecutionPolicy Bypass -File scripts/verify.ps1
```

`package.ps1` 强制先执行 `npm ci` 和前端构建，再运行 PyInstaller 目录打包。分发内容包括前端 assets、ffmpeg/ffprobe、默认待机媒体、licenses 和 metadata；不复制 fixtures、`.env`、数据库、cookies、keys、`.git` 或 logs，更不会复制用户密钥。`verify.ps1` 任一门禁失败都会输出该项 `=FAIL` 并立即非零退出。

## 已知限制

- 当前自动化不代替真实平台验收，也不能保证平台长期开放 RTMP 权限或接口行为不变。
- 本地 E2E 依赖独立安装的 MediaMTX；它不是桌面运行时的一部分。
- 目录包未提供代码签名、安装器或自动更新；Windows SmartScreen 可能提示未知发布者。
- 默认待机文件随包提供，但具体待机选择和平台端画面仍需操作者确认。
- 包仅面向构建时验证的 Windows x64 环境；跨架构运行未验收。

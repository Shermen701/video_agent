# Windows 视频会议 OBS 录制 Agent

第一版实现 Windows 单机录制 Agent，核心流程与会议软件解耦：

- 轮询 IECTP 待录制任务
- 提前 5 分钟启动会议软件并入会
- 使用 OBS websocket 开始/停止录制
- 检测会议结束或到达结束时间兜底
- 上传视频到 MinIO
- 回调 IECTP 开始录制和录制完成接口
- 会议软件通过 provider 插件扩展

## 快速开始

复制配置：

```powershell
Copy-Item .\config.example.yaml .\config.yaml
```

启动 Agent：

```powershell
py -m video_agent.agent --config .\config.yaml
```

打包 EXE：

```powershell
.\build_exe.bat
```

生成文件位于 `dist\video_agent.exe`。直接运行 exe 时会使用桌面固定目录 `Desktop\VideoAgent`，首次运行会生成 `config.yaml`、`config\*.pem`、`recordings` 和 `logs`；后续启动会继续读取桌面目录中的 `config.yaml`。录制视频固定保存到 `Desktop\VideoAgent\recordings`。

检查 OBS / 腾讯会议 / 钉钉安装路径：

```powershell
py -m video_agent.tools.discover_apps
```

## 真实录制机依赖

```powershell
py -m pip install -r requirements.txt
```

OBS 需要预安装并启用 obs-websocket。腾讯会议和钉钉 UI 自动化第一版基于 Windows 中文客户端，真实环境中可能需要按客户端版本微调 provider selector 配置。

钉钉 provider 名为 `dingtalk`，平台任务中的 `livePlatform` 可通过 `platform.provider_aliases` 将 `钉钉`、`DingTalk` 或 `dingtalk` 映射到该 provider。钉钉登录会使用任务里的 `loginAccount` 和 `loginPassword` 自动填写账号密码；如果触发验证码、短信、扫码确认、设备验证等人工安全验证，Agent 会判定登录失败并在 diagnostics 中保留窗口文本，后续需要人工处理或调整录制机登录状态。

微信视频号 provider 名为 `wechat_live`。任务需使用 `accessMethod: 搜索口令`，并将已关注视频号的显示昵称放在 `liveToken`（例如 `央视网`）。Agent 只连接录制机上已经登录的微信，不会使用任务账号密码，也不会在任务结束时退出微信。微信 UI 自动化必须在已登录、解锁的交互桌面会话中运行；可先执行 `py -m video_agent.tools.inspect_wechat` 采集窗口控件和截图，用于校准不同微信版本的图标定位。

## IECTP 接口

Agent 会先调用 `platform.token_path` 获取 token，并在后续请求头中发送 `Authorization: <token>`。待录制任务来自 `platform.pending_tasks_path`，OBS 开始录制成功后调用 `platform.start_callback_path`，MinIO 上传完成或失败后调用 `platform.complete_callback_path`。

MinIO 配置位于 `minio` 区块，默认 object key 为 `external-record/{task_id}/{filename}`。

# Doubao Session Video Skill

Doubao OAuth 插件配套的独立视频 Skill。插件仓库：
[`wangxiaoshuai136/doujie-oauth`](https://github.com/wangxiaoshuai136/doujie-oauth)。

本仓库只包含独立的 Python CLI / Skill，不包含 CPA 插件二进制，也不依赖插件仓库目录。

这是一个以 [SilicoGrove Video Skill](https://github.com/Rodert/silicogrove-video-skill) 的
目录结构、任务日志和命令体验为蓝本改造成的 **豆包会话视频 Skill**。

它不再调用 SilicoGrove 或官方火山方舟 API，而是复用你已经登录的豆包网页/桌面会话，
并通过豆包 agent 的视频创作流程生成：

1. 读取豆包桌面客户端已登录的会话 Cookie；
2. 提交 `/chat/completion` 的 agent 视频请求；
3. 在同一条 SSE 流中接收视频结果和真实 MP4 地址；
4. 下载 MP4，并保留不含敏感信息的 JSONL 任务日志。

## 适用范围

- 豆包国内站 `https://www.doubao.com` 的会话视频能力。
- 文生视频。
- 可选择账号当前可用的视频模型：
  `doubao-video`（自动）、`seedance-2.5`、`seedance-2.0`、
  `seedance-2.0-fast`、`seedance-2.0-mini`。
- Windows 桌面端自动导入会话；也支持从 CLIProxyAPI/Doujie 的 `doubao` 认证文件导入。

当前不支持：自动登录、验证码、账号创建、额度绕过、图生视频、视频/音频参考素材。

## 安装与配置

### Windows：推荐直接导入桌面会话

先登录豆包桌面客户端，然后：

```powershell
python .\scripts\doubao_session_video.py config --import-desktop
python .\scripts\doubao_session_video.py config --show-status
```

自定义桌面 `User Data` 路径时：

```powershell
python .\scripts\doubao_session_video.py config --import-desktop --user-data "C:\path\to\Doubao\User Data"
```

会话保存在 `%APPDATA%\doubao-session-video\config.json`。

### 从已有认证文件导入

```powershell
python .\scripts\doubao_session_video.py config --import-auth "C:\path\to\doubao-auth.json"
```

支持插件生成的 `"type": "doubao"` 认证文件，也支持：

```json
{
  "cookies": {
    "sessionid": "...",
    "ttwid": "...",
    "passport_csrf_token": "..."
  },
  "label": "我的豆包账号"
}
```

### 通过标准输入保存 Cookie

适合其他 Agent 在本机安全传递已提供的会话，不要把 Cookie 放进命令行参数：

```powershell
Get-Content -Raw .\session.json | python .\scripts\doubao_session_video.py config --set-session-stdin
```

## 生成视频

### 通过 CPA 插件调用

插件暴露 `doubao-video`、`seedance-2.5`、`seedance-2.0`、`seedance-2.0-fast`
和 `seedance-2.0-mini`。首次请求只返回方案；确认后加 `"confirm": true` 再提交：

```json
{
  "model": "seedance-2.0",
  "confirm": true,
  "messages": [{"role": "user", "content": "海边日落，镜头缓慢推进"}]
}
```

插件会在普通 chat completion 回复中返回最终 MP4 地址。模型名称选择会作为提示词
偏好交给豆包 agent，实际可用模型、时长和分辨率由账号当前能力决定。

### 直接运行 Skill 客户端

```powershell
python .\scripts\doubao_session_video.py generate `
  --model seedance-2.0 `
  --prompt "一只柴犬在雪地里奔跑，电影感，慢镜头" `
  --ratio 16:9 `
  --output-dir .\outputs
```

指定 Seedance 2.5 和时长提示：

```powershell
python .\scripts\doubao_session_video.py generate `
  --model seedance-2.5 `
  --prompt "海边日落，镜头缓慢推进" `
  --ratio 16:9 `
  --seconds 5 `
  --output-dir .\outputs
```

只校验请求、不提交任务：

```powershell
python .\scripts\doubao_session_video.py generate --prompt "测试" --dry-run
```

恢复旧版异步任务（仅兼容旧协议）：

```powershell
python .\scripts\doubao_session_video.py status TASK_ID --output-dir .\outputs
```

## 参数

| 参数 | 说明 |
| --- | --- |
| `--model` | `doubao-video`（自动，默认）或任意 `seedance-*` 模型 |
| `--prompt` | 视频描述，必填 |
| `--ratio` | `16:9`、`9:16`、`1:1`，默认 `16:9` |
| `--seconds` | 可选时长提示，例如 `5` 或 `10 秒` |
| `--camera-movement` | 可选镜头运动描述 |
| `--image` | 保留参数；当前 agent 流程尚不支持图生视频 |
| `--output-dir` | 输出目录，默认 `outputs` |
| `--request-timeout` | 等待豆包 agent 返回的秒数，默认 600 |
| `--no-download` | 只输出远程 URL，不下载 MP4 |
| `--dry-run` | 只验证并打印脱敏请求 |
| `--json` | 以 JSON 输出结果 |

时长和分辨率最终由豆包 agent 当前支持的模型能力决定；Skill 只传递提示，不承诺未经确认的参数。

## 安全与限制

- `config.json` 包含可登录豆包账号的 Cookie，属于敏感凭据；脚本在支持的平台上使用
  owner-only 权限，任务日志不会写入 Cookie、Prompt 或原始响应。
- 不把 Cookie 放进 Prompt、Git 仓库、命令行参数或环境变量。
- 该 Skill 使用非官方网页协议，可能随豆包更新失效；失败时先看任务日志，不要直接重提，
  以免重复消耗额度。
- 本项目仅供学习和个人技术研究；请遵守豆包服务条款及当地法律，不用于商业或非法用途。

## 测试

```powershell
python -m unittest discover -s .\tests -v
```

协议细节见 [`references/api.md`](references/api.md)。

## 归属

结构参考 `Rodert/silicogrove-video-skill`（Apache-2.0）。豆包会话协议实现为本项目独立
重写，未包含 SilicoGrove 的 API 代码。

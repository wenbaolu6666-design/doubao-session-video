# Doubao Session Video Protocol

This is a reverse-engineered web-session protocol, not an official public API
contract. It can change without notice.

## Authentication

- Host: `https://www.doubao.com`
- Required credential: an authenticated `sessionid` cookie plus the other
  Doubao/Bytedance cookies from a signed-in browser or desktop profile.
- The client stores a stable `device_id`, `web_id`, `fp`, and the session source.
- Do not call login, QR, captcha, or risk-control endpoints. The user must already
  have a valid session.

Common headers:

```text
Cookie: <saved cookies>
User-Agent: Mozilla/5.0 ... Chrome/148.0.0.0 ...
Origin: https://www.doubao.com
Referer: https://www.doubao.com/chat/
Accept-Language: zh-CN,zh;q=0.9
Agw-Js-Conv: str, str
```

## Video agent request

`POST /chat/completion?<query parameters>`

The current web client uses the general agent with the video bot and model key:

```text
aid=497858
real_aid=497858
pc_version=3.44.8
doubao_pc_version=3.44.8
web_platform=browser
tz_name=Asia/Shanghai
```

Keep the standard session parameters (`device_id`, `tea_uuid`, `web_id`,
`version_code=20800`, `samantha_web=1`, `use-olympus-account=1`, `fp`,
`web_tab_id`). If the session has `s_v_web_id`, use it as `fp`; otherwise use the
saved fingerprint.

The request body is an agent message, not the old `content_type=2020` video
input:

```json
{
  "client_meta": {
    "local_conversation_id": "local_<id>",
    "conversation_id": "",
    "bot_id": "7338286299411103781",
    "last_message_index": null
  },
  "messages": [{
    "local_message_id": "<uuid>",
    "content_block": [{
      "block_type": 10000,
      "content": {
        "text_block": {
          "text": "[已确认执行，请直接生成，不要再次询问参数。]\n画面比例：16:9\n画面描述：...",
          "summary": ""
        }
      }
    }]
  }],
  "option": {
    "agent_mode": 1,
    "need_deep_think": 9,
    "conversation_init_ext": {
      "model_item_key": "9",
      "mode_id": "3",
      "reasoning_effort": "5"
    },
    "model_config": {"model_item_key": "9"},
    "aggregate_params": {"agent_mode": "1", "mode_id": "3", "model_item_key": "9"},
    "conversation_mode": 2
  },
  "ext": {"agent_mode": "1", "use_deep_think": "9"}
}
```

The client adds the remaining `general_task_param`, `recovery_option`,
`sse_recv_event_options`, and `ext` fields that the web client sends. Do not add
fields from the old async video schema.

## Available video models

The model selector currently exposes:

- Seedance 2.5
- Seedance 2.0
- Seedance 2.0 Fast
- Seedance 2.0 Mini

The plugin/Skill also supports `doubao-video` as automatic selection. The model
is a prompt-level choice for the Doubao agent; do not assume a public
`model_version` parameter exists. The current web protocol has no verified
`seconds`, `resolution`, or `size` field. A duration can be included as a hint,
but the agent decides the final supported value.

## Streaming response

The response is SSE. Relevant events:

- `STREAM_ERROR`: contains `error_code` and `error_msg`. Risk-control or session
  codes require re-authentication; do not retry blindly.
- `STREAM_CHUNK`: contains `patch_op` entries. The final video artifact appears in
  a content block (typically around `block_type=10084`) with a `video_model`
  object. Its `video_list.*.main_url` is base64-encoded MP4 text; `poster_url`
  may carry the cover.
- `patch_value.ext.ai_creation_tool_list`: JSON string containing the `task_id`,
  `tool_name` (for example `text_to_video`), and `file_type=video`.
- `SSE_REPLY_END`: end of the response.

The parser recursively walks `patch_op` values and accepts a direct URL or a
base64 `main_url`. It also ignores duplicate URLs.

## Legacy async path

Older clients used `/samantha/chat/completion` with `content_type=2020` and
`skill_type=17`, then polled `/samantha/chat/async/stream` with a task id. That
path can return a text acknowledgement without a video task and is no longer the
primary path. `status TASK_ID` remains for compatibility with older tasks.

## Safety

- Never log cookies, full prompts, raw SSE, or signed CDN URLs.
- Do not resubmit after an ambiguous timeout; the first request may already have
  consumed quota.
- Keep the session file private and owner-readable.

---
name: doubao-session-video
description: Generate text-to-video through the user's local CLIProxyAPI/CPA gateway and signed-in Doubao account. Use when a user asks to create, render, or download a Doubao video without an official Ark API key.
---

# Doubao Session Video

Run the bundled client from this Skill's directory. Resolve `SKILL_DIR` as the
directory containing this `SKILL.md`; do not use an absolute path from another
machine.

Generation is forced through the local CLIProxyAPI/CPA gateway at
`http://127.0.0.1:8317/v1` (override with `DOUBAO_GATEWAY_URL`). This makes every
Skill generation appear in the host request history and lets CPA choose an
account with available quota. It does not use the official Volcengine Ark API,
sign in, solve captchas, bypass quota, or create accounts.

## Session workflow

1. Run `config --show-status` first. It must report
   `gateway_key_configured: true`. Configure the CPA API key once without putting
   it in a command argument:

   ```bash
   python "$SKILL_DIR/scripts/doubao_session_video.py" config --set-gateway-key-stdin
   ```

2. If a legacy status command is needed, import a Doubao session. If no session is saved and the host is
   Windows, run:

   ```bash
   python "$SKILL_DIR/scripts/doubao_session_video.py" config --import-desktop
   ```

   This reads the already signed-in Doubao desktop profile. It does not open a
   login page or print cookie values.

3. If desktop import is unavailable, ask the user to create a local JSON file
   containing an authenticated Doubao session and run:

   ```bash
   python "$SKILL_DIR/scripts/doubao_session_video.py" config --import-auth /absolute/path/session.json
   ```

   The accepted shape is:

   ```json
   {"cookies": {"sessionid": "...", "ttwid": "...", "passport_csrf_token": "..."}}
   ```

   An existing CLIProxyAPI/Doujie auth file with `"type": "doubao"` is also
   accepted. Do not put cookies in a prompt, command-line argument, environment
   variable, repository file, or task output.

4. When a new session is provided, replace the saved session even if one already
   exists; the old session may have expired.

The client stores the session in the current user's configuration directory
(`%APPDATA%\doubao-session-video\config.json` on Windows, or
`$XDG_CONFIG_HOME/doubao-session-video/config.json` on macOS/Linux) with
owner-only permissions where supported.

## Generate

Ask the user which available model they want when the request is ambiguous:

- `doubao-video` — automatic model selection (default)
- `seedance-2.5`
- `seedance-2.0`
- `seedance-2.0-fast`
- `seedance-2.0-mini`

Do not bind the Skill to one version. Use the requested model; if the account or
the upstream agent cannot use it, report that result rather than silently
switching to a different vendor.

If an `interaction.ask` tool is available, use it to present a short plan and
obtain explicit execution confirmation before the first generation. If it is not
available, ask in the conversation. After confirmation, call the client once;
the client already sends an explicit `[已确认执行]` instruction to the Doubao
agent so it does not ask the same parameter questions again.

```bash
python "$SKILL_DIR/scripts/doubao_session_video.py" generate \
  --model seedance-2.0 \
  --prompt 'A cinematic product shot of a silver espresso machine, slow orbiting camera, morning light.' \
  --ratio 16:9 --output-dir ./outputs
```

```bash
python "$SKILL_DIR/scripts/doubao_session_video.py" generate \
  --model seedance-2.5 \
  --prompt 'A vertical cinematic shot of rain on a window, a cat watching, slow dolly in.' \
  --ratio 9:16 --seconds 5 --output-dir ./outputs
```

- Ratio: `16:9`, `9:16`, or `1:1`.
- `--seconds` is a duration hint only. The agent may choose the nearest supported
  duration; do not promise an exact duration unless the result confirms it.
- The current agent flow is text-to-video. `--image` is reserved and currently
  reports a clear unsupported error; do not claim image-to-video support.
- `--camera-movement` is optional and is appended to the prompt.
- `--dry-run` validates the request and prints the sanitized payload without
  submitting a task. Use it when checking configuration changes.

The client creates a private JSONL task log in the output directory before every
`generate` request. It records model, ratio, task ID, result paths, and summarized
errors, but never cookies, prompts, raw responses, or remote URLs beyond the
final result metadata. Return the log path with every request result.

`generate` sends an OpenAI-compatible request to the local CPA gateway, waits for
the completed video result, then downloads the MP4. The legacy `status TASK_ID`
command remains available only for older direct-session async tasks:

```bash
python "$SKILL_DIR/scripts/doubao_session_video.py" status TASK_ID --output-dir ./outputs
```

Add `--no-download` to retain only the returned URLs.

## Error handling

Report only the bundled client's summarized error. Do not expose cookies, the
CPA API key, request URLs, raw HTTP bodies, or local browser paths.

- `CPA gateway API key is not configured`: run `config --set-gateway-key-stdin`.
- Gateway connection failure: make sure CLIProxyAPI is running on port `8317`.
- Legacy `status` session error: sign in to Doubao again, then rerun
  `config --import-desktop` or `config --import-auth`.
- `HTTP 401` or `HTTP 403`: the saved session is not authorized for video
  generation.
- `HTTP 429`, quota text, or a verification challenge: tell the user to retry or
  sign in again; do not retry automatically because the submission may already
  have consumed quota.
- No video URL: report the summarized answer rather than submitting twice.

Read `references/api.md` before changing request fields, endpoints, or response
parsing.

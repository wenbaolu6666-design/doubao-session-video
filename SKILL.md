---
name: doubao-session-video
description: Generate text-to-video through the user's signed-in Doubao web session with an available Seedance model. Use when a user asks to create, render, or download a Doubao video without an official Ark API key.
---

# Doubao Session Video

Run the bundled client from this Skill's directory. Resolve `SKILL_DIR` as the
directory containing this `SKILL.md`; do not use an absolute path from another
machine.

This Skill uses the user's existing Doubao web session, not the official
Volcengine Ark API. It does not sign in, solve captchas, bypass quota, or create
accounts. The user must already be signed in to the Doubao desktop client or
provide an authenticated session file.

## Session workflow

1. Run `config --show-status` first. If no session is saved and the host is
   Windows, run:

   ```bash
   python "$SKILL_DIR/scripts/doubao_session_video.py" config --import-desktop
   ```

   This reads the already signed-in Doubao desktop profile. It does not open a
   login page or print cookie values.

2. If desktop import is unavailable, ask the user to create a local JSON file
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

3. When a new session is provided, replace the saved session even if one already
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

The current Doubao agent returns the finished video in the same streaming
response. `generate` waits for that response, then downloads the MP4. The legacy
`status TASK_ID` command remains available only for older async tasks:

```bash
python "$SKILL_DIR/scripts/doubao_session_video.py" status TASK_ID --output-dir ./outputs
```

Add `--no-download` to retain only the returned URLs.

## Error handling

Report only the bundled client's summarized error. Do not expose cookies, request
URLs, raw HTTP bodies, proxy details, or local browser paths.

- `sessionid` missing or an expired-session/gateway error: ask the user to sign in
  to Doubao again, then rerun `config --import-desktop` or `config --import-auth`.
- `HTTP 401` or `HTTP 403`: the saved session is not authorized for video
  generation.
- `HTTP 429`, quota text, or a verification challenge: tell the user to retry or
  sign in again; do not retry automatically because the submission may already
  have consumed quota.
- No video URL: report the summarized answer rather than submitting twice.

Read `references/api.md` before changing request fields, endpoints, or response
parsing.

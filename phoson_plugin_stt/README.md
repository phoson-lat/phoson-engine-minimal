# phoson-plugin-stt

On-device, multilingual speech-to-text for the Phoson CLI, backed by
[Moonshine](https://github.com/moonshine-ai/moonshine). Everything runs
locally: no API keys, no network at inference time (models download once and
are cached under `~/.cache/moonshine_voice`).

## What it adds

| Surface | Trigger | Description |
|---|---|---|
| Dictation | **`Ctrl+O`** (full-screen TUI) | Push-to-talk: speak and the transcript streams **live into your prompt**. Press `Ctrl+O` again to stop. |
| Agent tool | `transcribe_audio(path, language)` | Transcribe a WAV file offline. |

There is **no `/listen` command**: dictation is a key binding, so it never
consumes the prompt. The transcript is inserted **at the cursor**, which means
you can dictate repeatedly into the same message and keep everything you
already typed.

## Languages

Moonshine ships models for: `ar`, `de`, `en`, `es`, `ja`, `ko`, `tl`, `uk`,
`vi`, `zh`. Pick one with `language` / `PHOSON_STT_LANGUAGE` (friendly names
like `spanish`, `japones`, `chino` work too).

## Install

The runtime is an **optional extra** so the base CLI stays light:

```bash
pip install 'phoson-engine-minimal[stt]'      # or: uv pip install moonshine-voice
```

Loading the plugin never imports Moonshine: a host without the extra starts
fine and only the calls fail — with an actionable message.

### PortAudio (Linux)

Microphone input needs the system PortAudio library, which some minimal
distros omit (`sounddevice` raises *PortAudio library not found*):

```bash
sudo apt install libportaudio2        # Debian / Ubuntu
```

If you cannot install system packages, point the plugin at a local copy:

```bash
export PHOSON_STT_PORTAUDIO=/path/to/libportaudio.so.2   # file or directory
```

File transcription (`transcribe_audio`) does **not** need PortAudio.

## Enable

Add the module to `plugins` in `~/.phoson/config.toml`:

```toml
plugins = ["phoson_plugin_stt"]
```

The engine loads `config.plugins` first (see `phoson_cli.session_utils`).

## Using it

```text
Ctrl+O          start listening (words appear in the prompt as you speak)
Ctrl+O          stop — what you already said is kept
…edit the text…
Enter           send it
```

There is **no time limit**: keep talking as long as you like and press
`Ctrl+O` when you are done. (Set `seconds = <n>` only if you *want* a cap.)

`Ctrl+O` is a push-to-talk toggle: it inserts at the cursor, so if the prompt
already has text (typed or from an earlier dictation) nothing is lost.

## Configuration

| Key / env var | Default | Meaning |
|---|---|---|
| `language` / `PHOSON_STT_LANGUAGE` | `es` | Language to dictate/transcribe. |
| `seconds` | *(none — until `Ctrl+O`)* | Optional cap in seconds. `0`/absent means **no time limit** (listen until you press `Ctrl+O` again); an explicit value is capped at 120 s. |
| `model_arch` / `PHOSON_STT_MODEL_ARCH` | library default | `tiny`, `base`, `tiny_streaming`, `base_streaming`, `small_streaming`, `medium_streaming` (or the integer). |
| `insert_into_prompt` | `true` | Prefill the prompt instead of printing a card (falls back automatically when unsupported). |
| `stream_preview` | `true` | Stream the partial transcript into the prompt while listening. |

```toml
[[plugin]]                      # inline spec form
name = "phoson_plugin_stt"
[plugin.config]
language = "en"
seconds = 12
model_arch = "small_streaming"
```

## Design notes

- **Push-to-talk lives in a plugin, the key lives in the CLI.** `Ctrl+O` just
  looks for a loaded plugin exposing the duck-typed `dictate(context)` entry
  point and runs it on a background task — the same "optional capability"
  style as `picker_unavailable`. No plugin means a clear notice, not a crash.
- **Live preview.** The plugin polls Moonshine's partial transcript
  (`on_text`) ~5×/s and calls `CliCommandContext.stream_prompt_text`, which
  rewrites only the preview segment in the prompt (nothing the user typed is
  touched). The host keeps a short repaint ticker alive while a preview is on
  screen, because a full-screen `Application` otherwise only redraws on input.
- **`engine.py`** wraps Moonshine with a lazy import (`_moonshine()`), a
  PortAudio locator (`PHOSON_STT_PORTAUDIO`, then `~/.cache/phoson/portaudio`)
  and a small `SttUnavailable` error type. It also redirects the native
  library's `stderr` (license/tokenizer spam) to `/dev/null` around each call,
  which would otherwise corrupt a full-screen TUI frame.

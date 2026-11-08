# Optional features (`/extras`)

Some features need packages that the CLI does not ship, because they are
large or only useful to some people. The standalone binary has no `pip`, so
install them with `/extras` instead:

| Extra | Feature | Packages |
|---|---|---|
| `stt` | Speech to text (voice input, `Ctrl+O`) | `moonshine-voice` (not on macOS Intel) |
| `computeruse` | Computer Use: screenshots, mouse and keyboard | `mss`, `Pillow`, plus `pyobjc-framework-Quartz` on macOS |
| `ssh` | SSH remote execution | `asyncssh` |
| `mcp` | Model Context Protocol servers | `mcp` |
| `memory` | Memory backends | `redis`, `asyncpg`, `qdrant-client` |
| `checkpoint` | Postgres checkpoints | `asyncpg` |
| `gemini`, `mistral`, `aws` | Provider SDKs | `google-genai`, `mistralai`, `boto3` |

## Commands

```text
/extras                       # what is ready, installed or missing
/extras install stt           # install one or more
/extras remove stt            # delete it (restart to unload)
```

The same works from a terminal, and from the installers:

```bash
phoson-cli extras install stt computeruse
curl -fsSL https://phoson.lat/install | sh -s -- --with stt,computeruse
```

```powershell
.\phoson-installer.ps1 -With stt,computeruse
```

The setup wizard also offers to install the extras that the plugins you pick
need. When a feature finds its packages missing, the error message names the
command to run.

## How it works

- Packages go to `~/.phoson/extras/<name>/` (`uv pip install --target`), built
  for the CLI's Python version. The CLI's own environment is never modified.
- `uv` is used from `PATH`. Otherwise, the binary downloads a pinned,
  checksum-verified copy once into `~/.phoson/bin`.
- Packages the CLI already ships (for example `numpy`) are pinned to the CLI's
  versions, and their duplicate copies are removed after the install.
- At startup each extra's directory is appended to `sys.path`, after the CLI's
  own paths, so the CLI's packages always take precedence.
- If an update changes the CLI's Python version, an extra built for the old
  version is not loaded. `/extras` marks it as *reinstall needed*.

In a Python environment (`uv tool install`, `pip`, a source checkout) the
package extras still work, for example
`pip install 'phoson-engine-minimal[stt]'`. `/extras` reports anything found
there as *ready*.

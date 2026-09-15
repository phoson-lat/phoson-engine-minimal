# Computer Use — SOTA research (2025–2026)

Research synthesis for issue **#223** (`phoson_plugin_computeruse`): a plugin that
gives the agent a *screenshot → decide → input* loop over the local desktop
(screen + mouse + keyboard).

> Scope note. Benchmark scores are **not directly comparable**: they differ in
> environment, task set, step budget, observation modality, model access, and
> self-reported vs independently verified. Always quote the benchmark *version*
> and *step budget* alongside a number.

---

## 1. TL;DR — what the SOTA actually converges on

1. **Grounding is the bottleneck, not planning.** A 90 % single-step
   click-localization model still fails a 30-action task because one early
   mis-click changes the state. Long-horizon scores are far below grounding
   scores.
2. **Screenshot + accessibility tree beats screenshot-only** on standard desktop
   software; screenshot-only is the *fallback* for canvas / custom-rendered /
   game / remote-desktop surfaces.
3. **A general planner should not own pixels.** Strongest systems split
   *planner* (task semantics) from *grounder* (target localization), then verify.
4. **One action at a time, then verify.** Execute a small action, wait for the UI
   to settle, re-observe, and only then continue — with bounded retries and a
   recovery plan (escape / undo / back / reopen).
5. **Coordinate transforms are the #1 practical source of failures.** DPI,
   fractional scaling, multi-monitor, Retina, and pre-API downscaling must be
   handled centrally by the plugin, never guessed.
6. **Gate the irreversible action, outside the model.** Reading and navigation
   can be automatic; send / submit / purchase / delete / publish / credentials
   must pass a permission gate.
7. **Native Wayland cannot be driven silently from userspace.** The blessed path
   is the xdg-desktop-portal RemoteDesktop + libei; X11 and Xvfb are the
   friction-free paths; macOS needs TCC grants.

---

## 2. Benchmark landscape

| Benchmark | Measures | Scale / protocol | Strong reported results | Human / gap |
|---|---|---|---|---|
| **OSWorld** (original) | Open-ended desktop tasks, real apps, execution-state evaluators | 369 tasks; 15/50/100/200-step budgets | Paper baseline: best model **12.24 %** | Human **72.36 %** |
| **OSWorld-Verified** | Corrected/reproducible OSWorld | 361–369 tasks; screenshot / a11y-tree / SoM variants | UI-TARS-2 **47.5 %**; Jedi-7B+o3 ≈ **50.2 %** @100 steps; top frontier models 78–86 % self-reported | Original human figure is the clearest reference |
| **OSWorld 2.0** | Newer, more diverse/realistic desktop tasks | Released ~Jun 2026; leaderboard still evolving | Treat early third-party numbers cautiously | No stable public human baseline |
| **OSWorld-G** | Fine-grained GUI **grounding** (text/icon/layout/manipulation) | 564 samples | Jedi-7B **54.1 %**; UI-TARS-7B **47.5 %**; Gemini-2.5-Pro **45.2 %**; Operator **40.6 %** | Grounding only, not task success |
| **WindowsAgentArena** | Windows 11 desktop tasks | 150+ tasks; screenshot / a11y / hybrid | UI-TARS-2 **50.6 %** | No robust universal human baseline |
| **AndroidWorld** | Dynamic Android tasks, state-based rewards | 116 tasks / 20 apps | UI-TARS-2 **73.3 %**; UI-TARS-1.5 46.6 % | Dynamic variants expose brittleness |
| **WebArena** | Browser tasks over self-hosted sites | 812 examples | Setup-dependent (DOM vs visual agents not comparable) | ≈78 % human (protocol-sensitive) |
| **ScreenSpot-Pro** | Single-step grounding on **high-res pro software** | 23 apps, 5 industries, 3 OSes | Best end-to-end **18.9 %** originally; ScreenSeekeR **48.1 %**; UI-TARS-1.5 **61.6 %** (other eval) | Exposes high-resolution grounding failure |
| **ScreenSpot-v2** | General element grounding | screenshot + instruction → target | UI-TARS-1.5 **94.2 %** | Not end-to-end |

**Reading the table:** single-step grounding reaches ~90 %, but end-to-end
long-horizon desktop success is ~50 % for the best agents and ~72 % for humans.
The gap is operational (state transitions, recovery, grounding under scale), not
raw reasoning.

Sources: [OSWorld](https://arxiv.org/abs/2404.07972) ·
[OSWorld project](https://os-world.github.io/) ·
[OSWorld-G](https://arxiv.org/abs/2505.13227) ·
[WindowsAgentArena](https://github.com/microsoft/WindowsAgentArena) ·
[AndroidWorld](https://arxiv.org/abs/2405.14573) ·
[WebArena](https://webarena.dev/) ·
[ScreenSpot-Pro](https://arxiv.org/abs/2504.07981)

---

## 3. Leading systems

| System | Perception | Action space | Memory / reflection | Notable |
|---|---|---|---|---|
| **Anthropic Claude Computer Use** | Screenshots (not a11y-dependent) | `screenshot`, mouse move/click/drag, key, type, scroll; pixel coords | Multi-turn history; per-action inspect/recover; **zoom** tool for dense UI | Official guidance: pre-downscale screenshots to the API image limits, else the model clicks on a degraded image → **primary cause of mis-clicks**. Text-before-image improves accuracy. `medium`/`high` thinking effort is the sweet spot; `max` gives no accuracy gain. |
| **OpenAI CUA / Operator** | Screenshots of a remote browser/computer | Low-level GUI actions, coordinate-style | Trajectory context, self-correction, user takeover | GPT-4o vision + RL; no OS-specific APIs. |
| **Google Gemini Computer Use** | Screenshots + supplied browser/computer state | click, type, scroll, key, wait, navigate | Multi-turn tool loop; dev owns state/retries | Explicit guidance: confirm **before the final irreversible click**; prompt-injection risk called out. |
| **ByteDance UI-TARS / 1.5 / 2** | **Screenshot-only** native GUI model | Unified cross-platform actions; normalized coords → pixels | System-2 reasoning, milestone recognition, reflection, multi-turn RL (UI-TARS-2) | 1.5: 42.5 OSWorld-100, 94.2 ScreenSpot-v2. 2: **47.5 OSWorld**, 50.6 WAA, 73.3 AndroidWorld. Open 7B is materially weaker than the largest model. |
| **Simular Agent-S / S2 / S3** | Screenshot + optional OCR / a11y / visual grounders | GUI primitives via specialist grounders | **Proactive hierarchical planning** + **Mixture-of-Grounding**; S3 adds planning/memory/recovery | Generalist planner + specialist grounders; strong relative gains, absolute values config-sensitive. |
| **Qwen-VL computer use** | VLM over screenshots (+ optional OCR/a11y) | **Absolute** coords, often normalized 0–1000; converted to actions | Framework-managed history/retries | Popular local base; needs careful resize / original-resolution conversion. |
| **Jedi grounding model** | Screenshot + multi-scale crops + UI decomposition | Target coordinates/boxes, paired with a planner | Stateless grounder; planner supplies subgoals | 4 M synthetic grounding examples; 54.1 OSWorld-G. |

Sources: [UI-TARS](https://arxiv.org/abs/2501.12326) ·
[UI-TARS-2](https://arxiv.org/abs/2509.02544) ·
[Agent S2](https://arxiv.org/abs/2504.00906) ·
[OpenAI CUA](https://openai.com/index/computer-using-agent/) ·
[Gemini computer use](https://ai.google.dev/gemini-api/docs/computer-use) ·
[Anthropic best practices](https://claude.com/blog/best-practices-for-computer-and-browser-use-with-claude)

---

## 4. Technique catalog

### Proven / consistently useful
- **Screenshot + accessibility-tree fusion.** Role, name, bounds, focus, enabled
  state, hierarchy → reliable label/control selection, avoids OCR errors,
  detects hidden/disabled controls, verifies focus changes. Falls down on
  canvas/games/remote-desktop and stale bounds.
- **Set-of-Mark (SoM) prompting.** Overlay numbered boxes → turns continuous
  coordinate prediction into a discrete choice. Helps menus/toolbars/dense UIs;
  hurts when the detector misses the target or a mark obscures a small control.
- **Coordinate normalization & scaling.** Keep `model_coord → displayed_size →
  physical_screen` explicit; account for DPR/HiDPI, decorations, multi-monitor,
  app/OS zoom, cropping, remote-desktop scaling.
- **Zoom / hierarchical visual search.** Global low-res view → crop/zoom the
  likely region → ground a smaller candidate set → transform local→global.
  Big gains on high-res pro apps without retraining.
- **Reflection & post-action verification.** Fresh screenshot, expected-change
  check, retry with a different strategy, else undo/escape/return to checkpoint.
  Signals: focus changed, dialog appeared, file exists, button enabled, title/URL
  changed.
- **Hierarchical planning.** High-level plan + 2–5 action local plans, replan at
  milestones, carry an explicit compact state summary instead of replaying every
  screenshot.
- **Specialist grounding models.** Generalist VLM for semantics/planning,
  specialist for pixel localization + confidence + alternatives.

### Promising but still experimental
- Multi-turn RL (UI-TARS-2): compelling, but rollout cost / reward design /
  transfer are hard.
- Agentic memory of successful UI procedures: helps repeats, not robust across
  layout changes.
- Self-generated reflective traces: can reinforce systematic mistakes.
- Mixture-of-grounding arbitration & confidence calibration.
- Planner/grounder model pairs: better accuracy, extra latency & error
  propagation.
- Inference-time scaling (sample N candidates, verify): reliability up, cost up.

---

## 5. Platform control stack (Python, 2025–2026)

**Recommendation:** define three provider interfaces — `CaptureBackend`,
`PointerBackend`, `KeyboardBackend` — and do **not** make PyAutoGUI/pynput the
abstraction boundary (both are X11/macOS-centric and fail on native Wayland).

| Capability | X11 | Wayland / GNOME | Wayland / wlroots | KDE Wayland | macOS |
|---|---:|---:|---:|---:|---:|
| Full-screen capture | ✅ `mss` | Portal + PipeWire | `grim`; portal | Portal + PipeWire | Quartz / `screencapture` |
| Region capture | ✅ | Portal selection UI | `grim -g` + `slurp` | Portal selection UI | Quartz / Pillow bbox |
| Per-monitor | ✅ | Portal streams | `grim -o` | Portal streams | CGDisplay APIs |
| Arbitrary window | X11 window id | ✗ (no portable API) | Compositor-specific | Portal-dependent | `CGWindowListCreateImage` |
| Pointer move | XTEST/`xdotool` | Portal + libei | Portal/libei or wlroots vpointer | Portal/libei | CGEvent |
| Keyboard | XTEST/`xdotool` | Portal + libei | Portal/libei or `wtype` | Portal/libei or `wtype` | CGEvent |
| Needs root | No | No (uinput only for `ydotool`) | No | No | No (TCC grants) |

**Per-platform default:**
- **Linux X11** — `mss` capture + `python-xlib` XTEST input (or `xdotool`
  subprocess). No root, easiest deployment. **Best first target.**
- **Linux Wayland** — `org.freedesktop.portal.RemoteDesktop` +
  `ScreenCast`, `ConnectToEIS()`/**libei** for input, `persist_mode=2` with a
  securely rotated `restore_token`. `grim`/`slurp` and `wtype` are wlroots-only
  fallbacks. Requires user consent; **not** silently drivable without it.
- **macOS** — PyObjC **Quartz** (`CGWindowListCreateImage`, `CGEvent*`).
  Requires **Screen Recording** + **Accessibility** TCC permissions; capture is
  physical px while window bounds are logical points → explicit `× backing_scale`.
- **Headless CI** — **Xvfb** + the X11 backend. Still the most reliable,
  lowest-friction CI path. Optionally a small nested-Wayland matrix
  (Weston/Sway `--headless`).

**Packages** (versions observed, pin them): `mss` 10.1 (MIT, active) ·
Pillow (active) · `python-xlib` 0.33 (LGPL-2.1+, mature/slow) · `pynput` 1.8.1
(LGPLv3, active) · PyAutoGUI 0.9.54 (BSD, slow) · `python-libei` 0.5 (beta) ·
`wayland-automation` 0.2.8 (MIT, wlroots-focused) · PyObjC (MIT, active) ·
`grim`/`slurp`/`wtype` (MIT) · `ydotool` (AGPL-3.0, needs `/dev/uinput` daemon) ·
Xvfb.

**Not reliably possible without privilege/consent:** silently capture an
arbitrary/minimized window on Wayland, warp the global pointer, or inject
input into another native-Wayland client. Options are user-approved portals,
compositor protocols, a privileged helper, or an X11/Xvfb session.

---

## 6. Safety & permissions

- **Human confirmation.** Auto-allow navigation/reading/reversible edits; ask
  immediately before the irreversible action (send, submit, purchase, delete,
  publish, accept terms, change security settings, submit credentials). Confirming
  *after* fields are filled but *before* the final click is the standard pattern.
- **Allow/deny lists** for shell, `sudo`, credential managers, financial/health
  sites, email send/share, file deletion/bulk rename, downloads/executables,
  CAPTCHA. **Default-deny** beats inferring risk from model text.
- **Sandboxing.** Benchmarks use VMs; a local plugin has no such margin. Prefer a
  dedicated OS user, separate browser profile, restricted FS roots, network
  policy, no ambient admin, optional VM/remote-desktop mode, full audit log,
  emergency-stop hotkey.
- **Credentials.** Never in prompt/trajectory. Use an OS credential broker,
  redacted screenshots, placeholders, user-controlled injection; the agent may
  focus a password field and ask the broker to fill it without observing it.
- **Prompt injection.** On-screen content is untrusted data. Screen text must
  never change tool permissions; keep allow/deny policy outside model context.

---

## 7. Implications for `phoson_plugin_computeruse` (#223)

- **Map the three provider interfaces to tools**, not a monolith:
  `screenshot(region?, window?)`, `mouse_move/click/drag`, `keyboard_type/
  shortcut`, plus `observe()`/`wait()`. Keep the low-level executor deterministic
  and put policy above it.
- **Perception v1 = screenshot (+ optional a11y where cheap); SoM/zoom as
  follow-ups.** Screenshots are `ImageBlock`s, so the run must use a
  vision-capable model. Note the per-turn token cost and send a *moderate*
  resolution + crops, not full-res every turn.
- **Centralize coordinate transforms** (logical → capture px → input protocol),
  detect session type / compositor / scale at init, and unit-test the transforms
  across DPI + multi-monitor. This is the highest-leverage correctness work.
- **Reuse the #169/#227 safety pattern:** every input tool publishes a mutating
  risk hint → resolves to `ask` and fails closed in one-shot; read-only
  `screenshot` publishes a read-only hint. Gate irreversible actions. This
  matches the existing workaround documented in `phoson_plugin_ssh/_plugin.py`
  (hint-based `ask`, since plugins cannot extend host match-tables yet).
- **Platform support should be incremental:** land **X11 + Xvfb** first
  (testable in CI, no consent dialogs), then macOS Quartz, then the Wayland
  portal/libei path (hard to test headless — needs a real session/VM).
- **Design for recovery:** escape / undo / back / reopen / restart-checkpoint are
  first-class, and each action should be followed by a settle + verify step.

### Open decisions
1. Backend scope for v1: **X11+Xvfb only**, or also macOS?
2. Perception: screenshot-only v1, or screenshot + a11y-tree fusion from the start?
3. Does the plugin own a planner/grounder split, or expose primitives and let the
   engine's model drive? (SOTA says split, but that is a bigger design.)
4. Coordinate/token budget knobs: default capture resolution, crop-on-demand,
   and whether to expose a `zoom` tool.
5. Whether the Wayland portal path ships in v1 or is deferred (consent UI +
   PipeWire make CI integration tests hard).

---

## Sources
- OSWorld — https://arxiv.org/abs/2404.07972 · https://os-world.github.io/
- OSWorld-G / Jedi — https://arxiv.org/abs/2505.13227 · https://osworld-grounding.github.io/
- UI-TARS — https://arxiv.org/abs/2501.12326 · https://github.com/bytedance/UI-TARS
- UI-TARS-2 — https://arxiv.org/abs/2509.02544
- Agent S2 — https://arxiv.org/abs/2504.00906
- ScreenSpot-Pro — https://arxiv.org/abs/2504.07981
- WindowsAgentArena — https://github.com/microsoft/WindowsAgentArena
- AndroidWorld — https://arxiv.org/abs/2405.14573
- WebArena — https://webarena.dev/
- OpenAI CUA — https://openai.com/index/computer-using-agent/
- Gemini computer use — https://ai.google.dev/gemini-api/docs/computer-use
- Anthropic computer use best practices — https://claude.com/blog/best-practices-for-computer-and-browser-use-with-claude
- xdg-desktop-portal RemoteDesktop — https://flatpak.github.io/xdg-desktop-portal/docs/doc-org.freedesktop.portal.RemoteDesktop.html
- libei — https://libinput.pages.freedesktop.org/libei/
- python-libei — https://pypi.org/project/python-libei/
- wayland-automation — https://github.com/OTAKUWeBer/Wayland-automation
- grim — https://github.com/emersion/grim · wtype — https://github.com/atx/wtype · ydotool — https://github.com/ReimuNotMoe/ydotool
- mss — https://pypi.org/project/mss/ · pynput — https://pypi.org/project/pynput/ · python-xlib — https://pypi.org/project/python-xlib/ · PyObjC — https://pypi.org/project/PyObjC/

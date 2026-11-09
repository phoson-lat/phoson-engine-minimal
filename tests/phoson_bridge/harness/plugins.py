"""Settings "Plugins" section: the bridge's catalog and config toggles.

Covers `phoson_bridge.plugins` (catalog + `set_enabled`) as the user sees it:
which official plugins show up, which mechanism enables them, which ones cannot
be touched from here, and **what ends up written to `config.toml`** when enabling
or disabling — which is what the engine will read at startup.

It neither touches the real `config.toml` nor builds the engine: `HOME` points to
a temporary directory (the engine resolves `~/.phoson` with `expanduser`) and the
`PHOSON_*` variables are cleared from the environment so the file is the only
source.

    python bridge/tests/test_plugins.py
"""

import os
import sys
import asyncio
import tomllib
import tempfile
import importlib
from types import SimpleNamespace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# Temporary HOME BEFORE importing the engine: `~/.phoson/config.toml` is resolved
# via expanduser, so this isolates the test from the user's config (and from
# their keys in the environment, which are cleared as well).
_TMP = tempfile.mkdtemp(prefix="phoson-plugins-test-")
for _var in ("HOME", "USERPROFILE"):
    os.environ[_var] = _TMP
for _key in [k for k in os.environ if k.startswith("PHOSON_")]:
    os.environ.pop(_key)

from phoson_bridge import plugins as P  # noqa: E402
from phoson_cli.config import load_config  # noqa: E402
from phoson_cli.labels import PLUGIN_LABELS, PLUGIN_REQUIRED_EXTRAS  # noqa: E402
from phoson_bridge.server import Bridge, SessionManager  # noqa: E402

CONFIG = Path(_TMP) / ".phoson" / "config.toml"
CONFIG.parent.mkdir(parents=True, exist_ok=True)

failures: list[str] = []


def _check(name: str, ok: bool, detail: str = "") -> None:
    print(f"{'PASS' if ok else 'FAIL'}: {name}{f' — {detail}' if detail else ''}")
    if not ok:
        failures.append(name)


def _fresh(text: str = "") -> object:
    """A fresh (or absent) config.toml + the config the bridge would load."""
    CONFIG.unlink(missing_ok=True)
    if text:
        CONFIG.write_text(text, encoding="utf-8")
    return load_config()


def _defaults() -> dict:
    return tomllib.loads(CONFIG.read_text(encoding="utf-8"))["defaults"]


def _entry(cfg: object, plugin_id: str) -> dict:
    return next(e for e in P.catalog(cfg) if e["id"] == plugin_id)


# 1) Catalog: the official plugins, with the engine's name and description.
cfg = _fresh()
entries = P.catalog(cfg)
_check(
    "el catálogo trae todos los plugins oficiales",
    {e["id"] for e in entries} == set(PLUGIN_LABELS),
    f"{sorted(e['id'] for e in entries)}",
)
_check(
    "nombre y descripción salen de phoson_cli.labels",
    all(
        (e["name"], e["description"]) == PLUGIN_LABELS[e["id"]]
        for e in entries
        if e["id"] in PLUGIN_LABELS
    ),
)
_check(
    "sin config.toml ningún plugin del catálogo está activo",
    all(not e["enabled"] for e in entries),
    str([e["id"] for e in entries if e["enabled"]]),
)

# 2) State: the spec list, the flags, and what is not controlled from here.
cfg = _fresh(
    "[defaults]\n"
    "enable_monitors = true\n"
    "enable_ssh = true\n"
    'theme = "dark"  # no gestionado por Plugins\n'
    'plugins = ["otro-plugin", '
    '{ name = "phoson-plugin-memory", config = { db = "redis://x" } }]\n'
    'disabled_plugins = ["phoson-plugin-bgjobs"]\n'
)
monitors = _entry(cfg, "phoson-plugin-monitor")
_check(
    "un flag activo marca el plugin activo por flag (no por lista)",
    monitors["enabled"] and monitors["mechanism"] == "flag" and not monitors["listed"],
    str(monitors),
)
ssh = _entry(cfg, "phoson-plugin-ssh")
_check(
    "un flag que save_config no persiste bloquea el toggle y nombra la fuente",
    ssh["enabled"] and ssh["locked"] and ssh["lockReason"] == "file:enable_ssh",
    str(ssh),
)
memory = _entry(cfg, "phoson-plugin-memory")
_check(
    "una spec de la lista está activa por lista",
    memory["enabled"] and memory["mechanism"] == "list" and memory["listed"],
    str(memory),
)
bgjobs = _entry(cfg, "phoson-plugin-bgjobs")
_check(
    "una spec en disabled_plugins se muestra inactiva",
    not bgjobs["enabled"] and bgjobs["mechanism"] == "flag",
    str(bgjobs),
)
stt = _entry(cfg, "phoson-plugin-stt")
_check(
    "el dictado se muestra y se marca como no gobernable aquí",
    stt["locked"] and stt["lockReason"] == "dictation" and not stt["enabled"],
    str(stt),
)
third = _entry(cfg, "otro-plugin")
_check(
    "una spec desconocida también se lista, marcada como no oficial",
    third["enabled"]
    and third["name"] == "otro-plugin"
    and not third["locked"]
    and third["official"] is False
    and memory["official"] is True,
    str(third),
)

# 3) Source of the active flag: the environment wins and is named as is.
os.environ["PHOSON_ENABLE_OTEL"] = "1"
cfg = _fresh("[defaults]\n")
otel = _entry(cfg, "phoson-plugin-otel")
_check(
    "un flag activo por entorno se nombra como env:VAR",
    otel["enabled"]
    and otel["locked"]
    and otel["lockReason"] == "env:PHOSON_ENABLE_OTEL",
    str(otel),
)
os.environ.pop("PHOSON_ENABLE_OTEL")

# 4) Enabling a flag plugin: writes the flag and NOT the list (if it were in
#    both, the engine would load it twice: the list and the flag's builder).
cfg = _fresh('[defaults]\ntheme = "dark"\n# comentario que debe sobrevivir\n')
result = P.set_enabled(cfg, "phoson-plugin-monitor", True)
text = CONFIG.read_text(encoding="utf-8")
_check(
    "activar un plugin con flag persiste el flag y no toca las listas",
    result["ok"]
    and result["fields"] == ["enable_monitors"]
    and cfg.enable_monitors is True
    and _defaults()["enable_monitors"] is True
    and "plugins" not in _defaults()
    and "disabled_plugins" not in _defaults(),
    f"{result} · {text!r}",
)
_check(
    "la escritura conserva los ajustes y comentarios ajenos a Plugins",
    'theme = "dark"' in text and "# comentario que debe sobrevivir" in text,
    repr(text),
)
_check(
    "reactivar lo ya activo no reescribe el archivo",
    P.set_enabled(cfg, "phoson-plugin-monitor", True) == {"ok": True, "fields": []},
)

# 5) Disabling that same plugin: turns off the flag and does not park it in the list.
cfg = _fresh("[defaults]\nenable_monitors = true\n")
result = P.set_enabled(cfg, "phoson-plugin-monitor", False)
_check(
    "desactivar un plugin con flag apaga solo el flag",
    result["ok"]
    and result["fields"] == ["enable_monitors"]
    and cfg.enable_monitors is False
    and _defaults()["enable_monitors"] is False
    and "disabled_plugins" not in _defaults(),
    f"{result} · {CONFIG.read_text(encoding='utf-8')!r}",
)
_check(
    "desactivar lo ya desactivado no reescribe el archivo",
    P.set_enabled(cfg, "phoson-plugin-monitor", False) == {"ok": True, "fields": []},
)

# 6) Enabling a plugin without a flag: it goes into the spec list.
cfg = _fresh('[defaults]\ntheme = "dark"\n')
result = P.set_enabled(cfg, "phoson-plugin-memory", True)
_check(
    "activar un plugin sin flag lo añade a plugins",
    result["ok"]
    and result["fields"] == ["plugins"]
    and cfg.plugins == ["phoson-plugin-memory"]
    and _defaults()["plugins"] == ["phoson-plugin-memory"]
    and "disabled_plugins" not in _defaults(),
    f"{result} · {CONFIG.read_text(encoding='utf-8')!r}",
)

# 7) Round trip with an inline table: the plugin's config is not lost.
cfg = _fresh(
    "[defaults]\n"
    'plugins = [{ name = "phoson-plugin-memory", config = { db = "redis://x" } }]\n'
)
result = P.set_enabled(cfg, "phoson-plugin-memory", False)
_check(
    "desactivar mueve la spec entera (con su config) a disabled_plugins",
    result["ok"]
    and _defaults()["plugins"] == []
    and _defaults()["disabled_plugins"]
    == [{"name": "phoson-plugin-memory", "config": {"db": "redis://x"}}],
    f"{result} · {CONFIG.read_text(encoding='utf-8')!r}",
)
cfg = load_config()
result = P.set_enabled(cfg, "phoson-plugin-memory", True)
_check(
    "reactivar restaura la spec original con su config",
    result["ok"]
    and _defaults()["plugins"]
    == [{"name": "phoson-plugin-memory", "config": {"db": "redis://x"}}]
    and _defaults()["disabled_plugins"] == [],
    f"{result} · {CONFIG.read_text(encoding='utf-8')!r}",
)

# 8) Switching a plugin parked in the list to a flag leaves no misleading trace.
cfg = _fresh('[defaults]\ndisabled_plugins = ["phoson-plugin-monitor"]\n')
result = P.set_enabled(cfg, "phoson-plugin-monitor", True)
_check(
    "activar por flag descarta la spec aparcada (plugin list no mentiría)",
    result["ok"]
    and cfg.enable_monitors is True
    and cfg.disabled_plugins == []
    and _defaults().get("disabled_plugins", []) == [],
    f"{result} · {CONFIG.read_text(encoding='utf-8')!r}",
)

# 9) What the UI cannot control.
cfg = _fresh("[defaults]\nenable_ssh = true\n")
result = P.set_enabled(cfg, "phoson-plugin-ssh", False)
_check(
    "no se puede apagar un flag que save_config no persiste",
    result == {"ok": False, "reason": "locked", "detail": "file:enable_ssh"},
    str(result),
)
result = P.set_enabled(cfg, "phoson-plugin-stt", True)
_check(
    "el dictado no se activa desde Plugins",
    result["ok"] is False
    and result["reason"] == "locked"
    and result["detail"] == "dictation",
    str(result),
)

# 10) First write: with no config.toml (new user) the file is created.
cfg = _fresh()
_check("sin config.toml no hay nada que leer", not CONFIG.exists())
result = P.set_enabled(cfg, "phoson-plugin-memory", True)
_check(
    "la primera escritura crea el config.toml con el plugin activo",
    result["ok"]
    and CONFIG.exists()
    and _defaults()["plugins"] == ["phoson-plugin-memory"],
    f"{result} · {CONFIG.read_text(encoding='utf-8')!r}",
)

# 11) Usage error: an empty spec cannot write anything.
try:
    P.set_enabled(cfg, "   ", True)
    empty_ok, detail = False, "no lanzó"
except ValueError as exc:
    empty_ok, detail = True, str(exc)
_check("una spec vacía se rechaza", empty_ok, detail)


# 12) Optional extras: the probe is the same one the wizard uses.
def _has_module(module: str) -> bool:
    import importlib.util

    try:
        return importlib.util.find_spec(module) is not None
    except (ImportError, ValueError):
        return False


_check(
    "un extra que falta se reporta con su nombre",
    P._missing_extra("x", {"x": ("phoson_modulo_que_no_existe", "the-extra")})
    == "the-extra",
)
_check(
    "un módulo presente no reporta extra",
    P._missing_extra("json", {"json": ("json", "the-extra")}) == "",
)
missing = [
    spec
    for spec, (module, extra) in PLUGIN_REQUIRED_EXTRAS.items()
    if _entry(cfg, spec)["missingExtra"] != ("" if _has_module(module) else extra)
]
_check(
    "missingExtra sale de PLUGIN_REQUIRED_EXTRAS del engine",
    missing == [],
    str(missing),
)

# 13) Install: there is no `uv` in the frozen sidecar, and it says so without trying.
try:
    sys.frozen = True
    frozen = P.installable()
finally:
    del sys.frozen
_check(
    "un sidecar congelado no puede instalar", frozen == (False, "frozen"), str(frozen)
)

# 14) `install` delegates to the engine (with a spy: nothing goes to the network)
#     and the freshly installed plugin shows up enabled in the catalog.
_manager = importlib.import_module("phoson_cli.plugin_manager")
_seen: dict[str, str] = {}


def _fake_install(source: str, config: object) -> str:  # pragma: no cover - spy
    _seen["source"] = source
    config.plugins.append("entrypoint:demo-plugin")
    return "demo-plugin"


_real_install = _manager.install_plugin
_manager.install_plugin = _fake_install
try:
    cfg = _fresh("[defaults]\n")
    name = P.install(cfg, "  git+https://example.invalid/demo  ")
    catalog_ids = {e["id"]: e["enabled"] for e in P.catalog(cfg)}
finally:
    _manager.install_plugin = _real_install
_check(
    "install delega en el engine con el origen ya recortado",
    name == "demo-plugin" and _seen["source"] == "git+https://example.invalid/demo",
    f"{name} {_seen}",
)
_check(
    "el plugin instalado aparece en el catálogo",
    catalog_ids.get("entrypoint:demo-plugin") is True,
    str(catalog_ids),
)


# 15) Propagation: plugins are global state (`config.toml`), so the toggle's
#     mirror has to reach every session — those that already have an engine and
#     the open ones not built yet. With a stale copy, that session would start
#     without the freshly enabled plugin (the bug where the toggle "gave no
#     tools" to the other sessions).
async def _propagation_case() -> tuple[bool, str]:
    base = _fresh('[defaults]\nplugins = ["phoson-plugin-memory"]\n')
    mgr = SessionManager(base, lambda *_a, **_k: None)
    otra = mgr.create()  # opened before the change and still without an engine
    toggled = mgr.create()
    # `get()` moves the session from `_pending` to `_repls` when building its
    # engine; here it is enough to simulate that step: the mirror only reads the
    # config.
    cfg_toggled = mgr._pending.pop(toggled)[0]
    repl = SimpleNamespace(config=cfg_toggled)
    mgr._repls[toggled] = repl

    cfg_toggled.enable_computeruse = True  # the user's toggle
    Bridge._mirror_config_fields(
        SimpleNamespace(config=base),
        cfg_toggled,
        ["enable_computeruse"],
        mgr.session_configs(),
    )

    configs = mgr.session_configs()
    ok = (
        base.enable_computeruse is True
        and mgr.config_for(otra).enable_computeruse is True
        and mgr.built_repls() == [repl]
        and cfg_toggled in configs
        and mgr.config_for(otra) in configs
    )
    return ok, (
        f"base={base.enable_computeruse} "
        f"otra={mgr.config_for(otra).enable_computeruse} "
        f"built={len(mgr.built_repls())} configs={len(configs)}"
    )


_passed, _detail = asyncio.run(_propagation_case())
_check(
    "el espejo del toggle alcanza a las sesiones vivas y a las no construidas",
    _passed,
    _detail,
)

CONFIG.unlink(missing_ok=True)
print("-" * 40)
print("FAILED:", failures) if failures else print("todo OK")
sys.exit(1 if failures else 0)

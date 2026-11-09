"""Report whether the dictation engine is operational and with which model/language."""

import os
import sys
import json

for _root in (os.path.abspath("bridge"), os.getcwd()):
    if os.path.isdir(_root) and _root not in sys.path:
        sys.path.insert(0, _root)

try:
    from phoson_bridge.stt import SttManager
except ImportError as exc:  # pragma: no cover - bridge missing
    print("STT SKIP:", exc)
    sys.exit(0)

try:
    import phoson_plugin_stt  # noqa: F401
except ImportError:  # pragma: no cover - CI without engine
    print("STT SKIP: phoson_plugin_stt no instalable aquí")
    sys.exit(0)

try:
    import moonshine_voice  # noqa: F401

    _HAS_RUNTIME = True
except ImportError:
    # The plugin ships in the engine's base package, but the dictation runtime
    # only comes with the [stt] extra (CI does not install it).
    _HAS_RUNTIME = False

manager = SttManager(lambda _m, _p: None)
status = manager.status()
print(json.dumps(status, ensure_ascii=False))
if _HAS_RUNTIME:
    if not status["supported"]:
        print("STT FAIL:", status.get("reason"))
        sys.exit(1)
    print("STT OK")
else:
    print(
        "STT SKIP: moonshine-voice no instalado; solo se valida el estado sin runtime"
    )

# Without the runtime, the status names the extra that fixes it (the UI offers
# to install it from the composer). `None` in `sys.modules` simulates it missing.
_saved = sys.modules.get("moonshine_voice")
sys.modules["moonshine_voice"] = None  # type: ignore[assignment]
missing = SttManager(lambda _m, _p: None).status()
if _saved is not None:
    sys.modules["moonshine_voice"] = _saved
if missing["supported"] or missing.get("missingExtra") != "stt":
    print("STT FAIL: sin moonshine-voice el estado debe pedir el extra [stt]:", missing)
    sys.exit(1)
print("STT OK: sin runtime se ofrece instalar [stt]")

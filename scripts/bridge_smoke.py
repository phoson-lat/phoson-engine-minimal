"""Validation harness for the sidecar against the real engine.

Usage (from the engine repo):
    PHOSON_PROVIDER=ollama PHOSON_MODEL=test-model \
        uv run python scripts/bridge_smoke.py [--turn]

``tests/phoson_bridge/test_harnesses.py`` runs it (without ``--turn``) in CI.

Without `--turn` it tests the whole control plane (no cost). With `--turn` it
makes ONE minimal call to the configured LLM to validate end-to-end streaming.

Variables:
    PHOSON_ENGINE_DIR  (optional) engine root; defaults to the cwd.
"""

import os
import sys
import json
import time
import threading
import subprocess

ENGINE_DIR = os.environ.get("PHOSON_ENGINE_DIR", os.getcwd())
BRIDGE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # engine root
PYTHON = sys.executable
WANT_TURN = "--turn" in sys.argv


def spawn():
    env = {
        **os.environ,
        "PYTHONPATH": BRIDGE_DIR + os.pathsep + os.environ.get("PYTHONPATH", ""),
    }
    return subprocess.Popen(
        [PYTHON, "-m", "phoson_bridge"],
        cwd=ENGINE_DIR,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        bufsize=1,
        env=env,
    )


class Client:
    def __init__(self, proc):
        self.proc = proc
        self.responses: dict[int, dict] = {}
        self.notifications: list[dict] = []
        self.bad_framing: list[str] = []
        self.events: list[dict] = []
        self._rid = 1
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        for line in self.proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                self.bad_framing.append(line[:160])
                continue
            if msg.get("id") is not None:
                self.responses[msg["id"]] = msg
            else:
                self.notifications.append(msg)
                if msg.get("method") == "agent.event":
                    self.events.append(msg["params"]["event"])

    def call(self, method, params=None, timeout=120):
        rid = self._rid
        self._rid += 1
        self.proc.stdin.write(
            json.dumps(
                {"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}}
            )
            + "\n"
        )
        self.proc.stdin.flush()
        deadline = time.time() + timeout
        while time.time() < deadline:
            if rid in self.responses:
                return self.responses[rid]
            time.sleep(0.05)
        raise TimeoutError(method)


def main() -> int:
    proc = spawn()
    cli = Client(proc)
    checks: list[tuple[str, bool, str]] = []

    def check(name, cond, detail=""):
        checks.append((name, bool(cond), detail))

    try:
        r = cli.call("system.ping")
        check(
            "system.ping",
            r.get("result", {}).get("ok") is True
            and r.get("result", {}).get("pid", 0) > 0,
            str(r.get("result", r.get("error", {}))),
        )

        r = cli.call("initialize")
        check("initialize", "result" in r, r.get("error", {}).get("message", ""))
        info = r["result"]
        default = info["defaultSessionId"]

        r = cli.call("session.new")
        check("session.new", "result" in r)
        s2 = r["result"]["sessionId"]
        check(
            "multi-sesión (claves distintas)",
            s2 != default,
            f"{s2[:8]} vs {default[:8]}",
        )

        # Teams: a named session claims presence on the real bus without running
        # turns (no LLM cost and no messages that would wake the peer).
        team = f"smoke-{os.getpid()}"
        r = cli.call("session.new", {"peerName": "backend", "peerTeam": team})
        peer = r.get("result", {}).get("sessionId", "")
        check("session.new con identidad peers", bool(peer), str(r.get("error", {})))
        r = cli.call("peers.start", {"sessionId": peer})
        check(
            "peers.start reclama presencia",
            r.get("result", {}).get("name") == "backend",
            str(r.get("result", r.get("error", {}))),
        )
        r = cli.call("peers.list", {"team": team})
        check(
            "peers.list compartido",
            any(
                p.get("name") == "backend" and p.get("live")
                for p in r.get("result", {}).get("peers", [])
            ),
            str(r.get("result", r.get("error", {}))),
        )
        if peer:
            cli.call("session.close", {"sessionId": peer})

        r = cli.call("session.list")
        check(
            "session.list",
            "result" in r,
            f"{len(r.get('result', {}).get('sessions', []))} sesiones",
        )

        r = cli.call("session.planCompact", {"sessionId": default})
        check("session.planCompact", "result" in r)

        r = cli.call("session.jumpCandidates", {"sessionId": default})
        check("session.jumpCandidates", "result" in r)

        png = os.path.join(ENGINE_DIR, "assets", "tui.png")
        if os.path.exists(png):
            r = cli.call("attachment.add", {"sessionId": default, "path": png})
            check(
                "attachment.add", "result" in r, r.get("error", {}).get("message", "")
            )
            r = cli.call("attachment.clear", {"sessionId": default})
            check("attachment.clear", "result" in r)

        r = cli.call("session.close", {"sessionId": s2})
        check("session.close", "result" in r)

        # Plugins: only side-effect-free checks — read the catalog, check that
        # `plugins.install` requires confirmation and that a plugin governed by
        # another section (dictation) is not touched from here. Enabling/disabling
        # would write to the real config.toml, so bridge/tests/test_plugins.py
        # covers that.
        r = cli.call("plugins.get", {"sessionId": default})
        state = r.get("result", {})
        check(
            "plugins.get",
            "result" in r
            and isinstance(state.get("plugins"), list)
            and bool(state.get("configPath")),
            f"{len(state.get('plugins', []))} plugins",
        )

        r = cli.call(
            "plugins.install",
            {"sessionId": default, "source": "https://example.invalid/x"},
        )
        check(
            "plugins.install sin confirmación",
            "error" in r,
            r.get("error", {}).get("message", ""),
        )

        r = cli.call(
            "plugins.set",
            {"sessionId": default, "id": "phoson-plugin-stt", "enabled": True},
        )
        check(
            "plugins.set no toca el dictado",
            r.get("result", {}).get("ok") is False
            and r["result"].get("reason") == "locked",
            str(r.get("result")),
        )

        # Enabling an already enabled plugin is idempotent (`fields: []`: it does
        # not write config.toml) and returns the live tool catalog — which the UI
        # uses to refresh the tool selectors after a change.
        activo = next(
            (
                p
                for p in state.get("plugins", [])
                if p.get("enabled") and not p.get("locked")
            ),
            None,
        )
        if activo is None:
            # Clean HOME in CI: no plugin is enabled yet. Enable one without
            # extras and repeat the same call to check idempotency.
            activo = next(
                (
                    p
                    for p in state.get("plugins", [])
                    if not p.get("locked") and not p.get("missingExtra")
                ),
                None,
            )
            if activo is not None:
                cli.call(
                    "plugins.set",
                    {"sessionId": default, "id": activo["id"], "enabled": True},
                )
        r = cli.call(
            "plugins.set",
            {
                "sessionId": default,
                "id": activo["id"] if activo else "",
                "enabled": True,
            },
        )
        res = r.get("result", {})
        tools = res.get("tools") or {}
        check(
            "plugins.set idempotente + catálogo de tools",
            res.get("ok") is True
            and res.get("fields") == []
            and bool(tools.get("visible")),
            f"{activo['id'] if activo else '-'}: fields={res.get('fields')} "
            f"tools={len(tools.get('visible', []))}",
        )

        r = cli.call("metodo.inventado")
        check("error método desconocido", "error" in r)

        r = cli.call("turn.cancel", {"sessionId": "no-existe"})
        check("error sesión desconocida", "error" in r)

        if WANT_TURN:
            r = cli.call(
                "turn.run",
                {
                    "sessionId": default,
                    "text": "Responde exactamente con la palabra: OK",
                },
            )
            result = r.get("result", {})
            check(
                "turn.run status=done",
                result.get("status") == "done",
                str(result.get("status")),
            )
            tokens = "".join(
                str(e.get("content", ""))
                for e in cli.events
                if e["type"] == "AgentTokenEvent"
            )
            check("streaming reconstruido", bool(tokens), repr(tokens)[:60])

    finally:
        try:
            proc.stdin.close()
        except Exception:
            pass
        time.sleep(0.4)
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()

    print("=== smoke test ===")
    failed = 0
    for name, ok, detail in checks:
        mark = "OK  " if ok else "FAIL"
        if not ok:
            failed += 1
        print(f"  [{mark}] {name}" + (f"  ({detail})" if detail else ""))
    framing = "OK" if not cli.bad_framing else "ERRORES: " + str(cli.bad_framing)
    print(f"\nframing: {framing}")
    print(f"notificaciones: {len(cli.notifications)} | eventos: {len(cli.events)}")
    print(f"RESULTADO: {'TODO OK' if failed == 0 else f'{failed} FALLOS'}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())

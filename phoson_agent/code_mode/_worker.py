"""Child-side runtime for ``run_code`` (code mode).

This module runs in a **separate Python process**, never in the engine's.
It must stay standard-library only and compatible with Python 3.8+: the
process may be a system ``python3`` rather than the interpreter running
Phoson (the standalone binary has no general-purpose interpreter of its
own). The parent passes this file's source with ``python -I -c``.

Channels (set up before any model code runs):

* the original fd 0 / fd 1 carry the JSON-lines RPC with the parent;
* fd 0 is then pointed at ``/dev/null`` and fd 1 at fd 2, so everything the
  script (or a child process it starts) prints lands on the stderr pipe,
  which the parent captures as the script's output — the RPC channel can
  never be corrupted by a stray ``print`` or ``os.system``.

Protocol (one JSON object per line):

* parent → child, first line: ``{"type": "run", "code": ..., "tools": [...]}``
  where each tool spec is ``{"name", "py", "params", "required", "doc"}``;
* child → parent: ``{"id", "type": "call", "tool", "args"}``,
  ``{"id", "type": "search", "query", "limit"}``,
  ``{"id", "type": "help", "tool"}`` and finally ``{"type": "done", "ok"}``;
* parent → child replies: ``{"id", "ok": true, "value": ...}`` or
  ``{"id", "ok": false, "error": "..."}``.
"""

import os
import sys
import json
import linecache
import threading
import traceback

_RUN_FILENAME = "<run_code>"


class ToolError(Exception):
    """A tool call failed or was refused (permission gate, bad args, ...)."""


class _Rpc:
    """Thread-safe request/response client over the inherited RPC pipes."""

    def __init__(self, rin, rout):
        self._rin = rin
        self._rout = rout
        self._write_lock = threading.Lock()
        self._pending = {}
        self._pending_lock = threading.Lock()
        self._next_id = 0
        self._closed = False

    def start(self):
        reader = threading.Thread(target=self._read_loop, name="rpc-reader")
        reader.daemon = True
        reader.start()

    def send(self, message):
        line = json.dumps(message, ensure_ascii=True, default=str)
        with self._write_lock:
            self._rout.write(line + "\n")
            self._rout.flush()

    def request(self, message):
        with self._pending_lock:
            if self._closed:
                raise ToolError("the host closed the tool channel")
            self._next_id += 1
            request_id = self._next_id
            slot = {"event": threading.Event(), "reply": None}
            self._pending[request_id] = slot
        message = dict(message)
        message["id"] = request_id
        self.send(message)
        slot["event"].wait()
        reply = slot["reply"]
        if not reply or not reply.get("ok"):
            raise ToolError((reply or {}).get("error") or "tool call failed")
        return reply.get("value")

    def _read_loop(self):
        try:
            for line in self._rin:
                line = line.strip()
                if not line:
                    continue
                try:
                    reply = json.loads(line)
                except ValueError:
                    continue
                with self._pending_lock:
                    slot = self._pending.pop(reply.get("id"), None)
                if slot is not None:
                    slot["reply"] = reply
                    slot["event"].set()
        finally:
            # Parent gone (cancelled run, timeout): fail every waiter so the
            # script unwinds instead of blocking forever.
            with self._pending_lock:
                self._closed = True
                pending = list(self._pending.values())
                self._pending.clear()
            for slot in pending:
                slot["reply"] = {
                    "ok": False,
                    "error": "the host closed the tool channel",
                }
                slot["event"].set()


def _make_function(rpc, spec):
    name = spec["name"]
    params = list(spec.get("params") or [])

    def call(*args, **kwargs):
        if len(args) > len(params):
            raise TypeError(
                "{}() takes at most {} positional argument(s)".format(
                    spec["py"], len(params)
                )
            )
        payload = dict(zip(params, args))
        for key, value in kwargs.items():
            if key in payload:
                raise TypeError(
                    "{}() got multiple values for argument {!r}".format(spec["py"], key)
                )
            payload[key] = value
        return rpc.request({"type": "call", "tool": name, "args": payload})

    call.__name__ = spec["py"]
    call.__qualname__ = "tools." + spec["py"]
    call.__doc__ = spec.get("doc") or ""
    return call


class _Tools:
    """The ``tools`` namespace exposed to the script."""

    def __init__(self, rpc, specs):
        self._rpc = rpc
        self._by_name = {}
        for spec in specs:
            fn = _make_function(rpc, spec)
            self._by_name[spec["name"]] = fn
            object.__setattr__(self, spec["py"], fn)

    def call(self, name, **args):
        """Call a tool by its exact registered name (also masked/MCP tools)."""
        fn = self._by_name.get(name)
        if fn is not None:
            return fn(**args)
        return self._rpc.request({"type": "call", "tool": name, "args": args})

    def search(self, query, limit=10):
        """Find tools by keyword: a list of {name, py, signature, description}."""
        return self._rpc.request({"type": "search", "query": query, "limit": limit})

    def help(self, name):
        """Full description and JSON schema of one tool, as text."""
        return self._rpc.request({"type": "help", "tool": name})

    def list(self):
        """Names of every tool callable from this script."""
        return sorted(self._by_name)

    def __getattr__(self, item):
        raise AttributeError(
            f"no tool named {item!r}; use tools.search('keyword') or "
            "tools.call('exact_name', ...)"
        )


def parallel(*calls, **kwargs):
    """Run zero-argument callables concurrently; return their results in order.

    ``parallel(lambda: tools.read_file(path="a"), lambda: tools.read_file(path="b"))``.
    The first exception is re-raised after every call finished, unless
    ``return_exceptions=True`` (then exceptions are returned in place).
    """
    return_exceptions = bool(kwargs.pop("return_exceptions", False))
    max_workers = int(kwargs.pop("max_workers", 8) or 8)
    if kwargs:
        raise TypeError("unexpected keyword argument(s): " + ", ".join(kwargs))
    if len(calls) == 1 and isinstance(calls[0], (list, tuple)):
        calls = tuple(calls[0])
    results: list = [None] * len(calls)
    errors: list = [None] * len(calls)
    semaphore = threading.Semaphore(max(1, max_workers))

    def run(index, fn):
        with semaphore:
            try:
                results[index] = fn()
            except BaseException as exc:  # noqa: BLE001 - reported to the caller
                errors[index] = exc

    threads = [
        threading.Thread(target=run, args=(i, fn), name=f"parallel-{i}")
        for i, fn in enumerate(calls)
    ]
    for thread in threads:
        thread.daemon = True
        thread.start()
    for thread in threads:
        thread.join()
    if return_exceptions:
        return [
            errors[i] if errors[i] is not None else results[i]
            for i in range(len(calls))
        ]
    for error in errors:
        if error is not None:
            raise error
    return results


def _redirect_stdio():
    """Move the RPC to private fds; script stdout/stderr → the stderr pipe."""
    rin = open(os.dup(0), encoding="utf-8", newline="\n")
    rout = open(os.dup(1), "w", encoding="utf-8", newline="\n")
    devnull = os.open(os.devnull, os.O_RDONLY)
    os.dup2(devnull, 0)
    os.close(devnull)
    os.dup2(2, 1)
    # The pipe makes stdout block-buffered; line buffering keeps prints and
    # tracebacks (stderr) in order in the captured output.
    try:
        getattr(sys.stdout, "reconfigure")(line_buffering=True)
    except (AttributeError, ValueError):
        pass
    return rin, rout


def main():
    rin, rout = _redirect_stdio()
    rpc = _Rpc(rin, rout)
    first = rin.readline()
    try:
        request = json.loads(first)
    except ValueError:
        sys.stderr.write("run_code: malformed start message\n")
        return 2
    rpc.start()

    namespace = {
        "__name__": "__main__",
        "__builtins__": __builtins__,
        "tools": _Tools(rpc, request.get("tools") or []),
        "parallel": parallel,
        "ToolError": ToolError,
    }
    source = request.get("code") or ""
    # Tracebacks show the offending source line, not just its number.
    linecache.cache[_RUN_FILENAME] = (  # type: ignore[assignment]
        len(source),
        None,
        source.splitlines(True),
        _RUN_FILENAME,
    )
    ok = True
    try:
        code = compile(source, _RUN_FILENAME, "exec")
        exec(code, namespace)
    except SystemExit as exc:
        ok = exc.code in (None, 0)
    except BaseException:  # noqa: BLE001 - the model needs the traceback
        ok = False
        etype, value, tb = sys.exc_info()
        # Drop this module's own frames: the model only wrote <run_code>.
        while tb is not None and tb.tb_frame.f_code.co_filename != _RUN_FILENAME:
            tb = tb.tb_next
        traceback.print_exception(etype, value, tb)
    finally:
        try:
            sys.stdout.flush()
            sys.stderr.flush()
        except (OSError, ValueError):
            pass
    try:
        rpc.send({"type": "done", "ok": ok})
    except (OSError, ValueError):
        pass
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

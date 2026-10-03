#!/usr/bin/env python
"""Offline startup benchmark; run with the project's Python interpreter.

Usage: .venv/bin/python scripts/bench_cli_startup.py --compare-head
Reports medians from fresh processes (warm filesystem cache). The app probe
measures imports + construction, NOT first terminal paint or provider latency.
RSS is process peak RSS on Linux, not retained heap. No user config is loaded.
"""

import io
import sys
import json
import tarfile
import argparse
import statistics
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
PROBE = """
import sys, time, resource, json
start = time.perf_counter()
cpu = time.process_time()
mode = sys.argv[1]
if mode == 'app':
    import tempfile
    from pathlib import Path
    from unittest.mock import MagicMock, patch
    from phoson_cli.config import PhosonConfig
    from phoson_cli.fullscreen.app import PhosonApp
    with tempfile.TemporaryDirectory() as tmp:
        config = PhosonConfig(provider='ollama', sessions_dir=Path(tmp),
                              history_file=Path(tmp) / 'history')
        with patch('phoson_cli.controller.build_chat', return_value=MagicMock()):
            app = PhosonApp(config)
else:
    import contextlib, io
    with contextlib.redirect_stdout(io.StringIO()):
        import phoson_cli.__main__ as cli
        if mode != 'import':
            sys.argv = ['phoson-cli', '--' + mode]
            try:
                cli.main()
            except SystemExit as exc:
                if exc.code != 0:
                    raise
print(json.dumps(dict(wall_ms=(time.perf_counter()-start)*1000,
                      cpu_ms=(time.process_time()-cpu)*1000,
                      rss_mib=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss/1024)))
"""


def measure(root: Path, runs: int) -> dict:
    result = {}
    for mode in ("import", "help", "version", "app"):
        samples = []
        # Discard one run so newly extracted HEAD sources also have bytecode
        # caches; compare warm-cache startup rather than compilation costs.
        for run in range(runs + 1):
            process = subprocess.run(
                [sys.executable, "-c", PROBE, mode],
                cwd=root,
                capture_output=True,
                text=True,
                check=True,
                timeout=30,
            )
            if run:
                samples.append(json.loads(process.stdout))
        result[mode] = {
            metric: round(statistics.median(s[metric] for s in samples), 2)
            for metric in samples[0]
        }
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs", type=int, default=5)
    parser.add_argument("--compare-head", action="store_true")
    args = parser.parse_args()
    if args.runs < 1:
        parser.error("--runs must be positive")
    results = {}
    if args.compare_head:
        archive = subprocess.run(
            ["git", "archive", "HEAD"], cwd=ROOT, capture_output=True, check=True
        ).stdout
        with TemporaryDirectory(prefix="phoson-startup-baseline-") as tmp:
            with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
                tar.extractall(tmp, filter="data")
            results["HEAD"] = measure(Path(tmp), args.runs)
    results["working_tree"] = measure(ROOT, args.runs)
    print(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()

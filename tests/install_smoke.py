"""Install the tested checkout through npx in an isolated project and run its CLI."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "skills" / "codex-token-xray" / "scripts"))
from txray import __version__  # noqa: E402


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", default=str(REPO))
    args = parser.parse_args()
    npx = shutil.which("npx.cmd" if os.name == "nt" else "npx")
    if not npx:
        raise RuntimeError("npx is required for the distribution smoke test")
    with tempfile.TemporaryDirectory(prefix="token-xray-install-") as temp:
        work = Path(temp)
        env = dict(os.environ, DO_NOT_TRACK="1", CODEX_TOKEN_XRAY_HOME=str(work / "state"), PYTHONUTF8="1")
        subprocess.run([npx, "--yes", "skills@1.5.26", "add", args.source, "--agent", "codex", "--yes"],
                       cwd=work, env=env, check=True, timeout=180)
        installed = work / ".agents" / "skills" / "codex-token-xray"
        source = REPO / "skills" / "codex-token-xray"
        expected = {p.relative_to(source) for p in source.rglob("*") if p.is_file() and "__pycache__" not in p.parts and p.suffix != ".pyc"}
        actual = {p.relative_to(installed) for p in installed.rglob("*") if p.is_file() and "__pycache__" not in p.parts and p.suffix != ".pyc"}
        if actual != expected:
            raise RuntimeError(f"installed file set differs: {actual ^ expected}")
        for rel in expected:
            if (source / rel).read_bytes() != (installed / rel).read_bytes():
                raise RuntimeError(f"installed bytes differ: {rel}")
        for name in ("xray", "apply", "backup", "restore"):
            subprocess.run([sys.executable, str(installed / "scripts" / f"{name}.py"), "--help"],
                           cwd=work, env=env, check=True, stdout=subprocess.PIPE, timeout=30)
        result = subprocess.run([sys.executable, str(installed / "scripts" / "xray.py"), "--codex-home", str(work / "empty-codex"), "--json"],
                                cwd=work, env=env, check=True, capture_output=True, encoding="utf-8", timeout=60)
        report = json.loads(result.stdout)
        assert report["version"] == __version__, report["version"]
        assert report["environment"]["sessions_parsed"] == 0
        print(f"Verified {len(expected)} installed files and all four CLI entrypoints on {sys.platform}.")


if __name__ == "__main__":
    main()

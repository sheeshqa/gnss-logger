"""Build separate GUI/CLI onedir bundles in a newly reserved directory."""

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from uuid import uuid4


REPO = Path(__file__).resolve().parents[1]


def source_hashes() -> dict:
    paths = sorted((REPO / "gnss_logger").rglob("*.py"))
    paths.extend(REPO / name for name in (
        "pyproject.toml", "tools/build_gui_stage4_2.py", "tools/launch_gui_stage4_2.py"))
    return {path.relative_to(REPO).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in paths}


def build_commands(build_root: Path, gui_console: bool = False) -> list:
    """Return argument vectors without importing Qt or running application code."""
    commands = []
    for name, launcher, mode in (
        ("gnss-logger-gui", REPO / "tools" / "launch_gui_stage4_2.py",
         "--console" if gui_console else "--windowed"),
        ("gnss-logger", build_root / "launch_cli_stage4_2.py", "--console"),
    ):
        commands.append([
            sys.executable, "-m", "PyInstaller", "--onedir", mode,
            "--noupx", "--name", name,
            "--paths", str(REPO),
            "--distpath", str(build_root / "dist"),
            "--workpath", str(build_root / "work" / name),
            "--specpath", str(build_root / "spec" / name),
            str(launcher),
        ])
    return commands


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-root", type=Path,
                        default=Path(tempfile.gettempdir()) / "gnss-stage4-2")
    parser.add_argument("--build-id", help="New directory name; existing names are refused")
    parser.add_argument("--gui-console", action="store_true",
                        help="Use a diagnostic console GUI instead of windowed GUI")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print commands only; create no files and run no build")
    args = parser.parse_args(argv)
    build_id = args.build_id or (
        datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid4().hex
    )
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}", build_id):
        parser.error("--build-id must contain 1-80 ASCII letters, digits, '_' or '-'; start with a letter/digit")
    build_root = args.output_root.expanduser().resolve() / build_id
    if build_root.exists():
        parser.error(f"Build directory already exists; choose a new id: {build_root}")
    commands = build_commands(build_root, args.gui_console)
    print(json.dumps({"build_root": str(build_root), "commands": commands}, indent=2), flush=True)
    if args.dry_run:
        return 0
    for source in (REPO / "gnss_logger" / "gui" / "app.py",
                   REPO / "gnss_logger" / "cli.py",
                   REPO / "tools" / "launch_gui_stage4_2.py"):
        if not source.is_file():
            parser.error(f"Required source is missing: {source}")
    build_root.parent.mkdir(parents=True, exist_ok=True)
    try:
        build_root.mkdir(exist_ok=False)
    except FileExistsError:
        parser.error(f"Build directory was reserved concurrently: {build_root}")
    # Generated only inside this fresh run; no extra repository launcher is needed.
    with (build_root / "launch_cli_stage4_2.py").open("x", encoding="utf-8") as stream:
        stream.write('from gnss_logger.cli import main\n\nif __name__ == "__main__":\n    raise SystemExit(main())\n')
    with (build_root / "commands.json").open("x", encoding="utf-8") as stream:
        json.dump(commands, stream, indent=2)
    before = source_hashes()
    with (build_root / "source-hashes-before.json").open("x", encoding="utf-8") as stream:
        json.dump(before, stream, indent=2)
    for name, command in zip(("gnss-logger-gui", "gnss-logger"), commands):
        (build_root / "spec" / name).mkdir(parents=True, exist_ok=False)
        with (build_root / f"{name}.build.log").open("x", encoding="utf-8") as log:
            result = subprocess.run(command, cwd=build_root, shell=False,
                                    stdout=log, stderr=subprocess.STDOUT, check=False)
        if result.returncode:
            print(f"Build failed ({result.returncode}); inspect {log.name}", file=sys.stderr)
            return result.returncode
    after = source_hashes()
    with (build_root / "source-hashes-after.json").open("x", encoding="utf-8") as stream:
        json.dump(after, stream, indent=2)
    if before != after:
        print("Source changed during build; discard this run for acceptance.", file=sys.stderr)
        return 1
    print("Source SHA256 snapshots match.", flush=True)
    print(f"Built GUI and CLI: {build_root / 'dist'}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

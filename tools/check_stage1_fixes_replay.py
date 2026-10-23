"""Read-only Replay regression for existing real UBX. Never opens serial or changes inputs."""

import argparse
import contextlib
import hashlib
import io
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from gnss_logger.cli import main


def fingerprint(path):
    h = hashlib.sha256()
    count = 0
    with path.open("rb") as stream:
        while chunk := stream.read(65536):
            h.update(chunk)
            count += len(chunk)
    return count, h.hexdigest()


def validate(paths, output):
    output.mkdir(parents=True, exist_ok=False)
    summary = []
    for index, path in enumerate(paths):
        size, digest = fingerprint(path)
        argv = ["replay", str(path), "--speed", "0", "--session-dir", str(output / "sessions")]
        with contextlib.redirect_stdout(io.StringIO()) as stdout:
            code = main(argv)
        report = json.loads(stdout.getvalue())
        old_path = path.with_suffix(".json")
        old = json.loads(old_path.read_text(encoding="utf-8")) if old_path.exists() else None
        checks = dict(hash_matches=report["stream_sha256"] == digest,
                      bytes_match=report["bytes_received"] == size,
                      input_unchanged=fingerprint(path) == (size, digest))
        if old:
            checks["messages_equal_old_live"] = old["messages"] == report["messages"]
            checks["epochs_equal_old_live"] = old["RAWX"]["epochs"] == report["RAWX"]["epochs"]
            checks["signals_equal_old_live"] = old["RAWX"]["signals"] == report["RAWX"]["signals"]
            checks["phase_valid_equal_old_live"] = old["RAWX"]["phase_valid"] == report["RAWX"]["phase_valid"]
            checks["timing_equal_old_live"] = old["timing"] == report["timing"]
        if code != (0 if size else 2) or not all(checks.values()):
            raise RuntimeError(f"Replay regression failed: {path}: exit={code}, checks={checks}")
        summary.append(dict(source=str(path.resolve()), exit_code=code, bytes=size, sha256=digest,
            rawx=report["RAWX"]["epochs"], sfrbx=report["SFRBX"]["messages"],
            checksum_errors=report["checksum_errors"], decode_errors=report["decode_errors"],
            phase_quality=report["phase_quality"]["status"], phase_events=report["phase_quality"]["counts"],
            session_id=report["session_id"], report_path=report["paths"]["report"],
            checks=checks))
    target = output / "summary.json"
    target.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--hardware-root", type=Path, required=True)
    p.add_argument("--historical", type=Path)
    p.add_argument("--output", type=Path, required=True)
    args = p.parse_args()
    paths = sorted(args.hardware_root.rglob("*.ubx"))
    if args.historical:
        paths.append(args.historical)
    validate(paths, args.output)


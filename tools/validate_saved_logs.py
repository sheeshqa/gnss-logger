"""Repeatable integration evidence. Never opens a serial port; keeps the source intact."""

import argparse
import contextlib
from datetime import datetime, timedelta, timezone
import hashlib
import io
import json
from pathlib import Path
import sys
import time

# Permit running the tool directly from a source checkout.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from gnss_logger.cli import main
from gnss_logger.diagnostics import Diagnostics
from gnss_logger.engine import run_source
from gnss_logger.mock import MockReceiver, synthetic_epoch, synthetic_nav
from gnss_logger.device.requests import Probe


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while data := stream.read(65536):
            digest.update(data)
    return digest.hexdigest()


def cli(argv):
    with contextlib.redirect_stdout(io.StringIO()) as output:
        code = main(argv)
    result = json.loads(output.getvalue())
    if code != 0:
        raise RuntimeError(f"Replay/mock validation failed: {code}, {result.get('error', result.get('decode_errors'))}")
    return result


def create_fixture(path, data):
    if path.exists():
        if path.read_bytes() != data:
            raise FileExistsError(f"Refusing to replace different fixture: {path}")
    else:
        path.write_bytes(data)
    sidecar = Path(str(path) + ".meta.json")
    if not sidecar.exists():
        sidecar.write_text(json.dumps(dict(simulated=True, description="Synthetic framing fixture, not scientific data")),
                           encoding="utf-8")


def validate(source, output):
    output.mkdir(parents=True, exist_ok=False)
    fixtures = Path("tests/fixtures")
    fixtures.mkdir(parents=True, exist_ok=True)
    cases = {}
    for profile in ("f9p", "x20p"):
        receiver = MockReceiver(profile, epochs=3)
        capture = io.BytesIO()
        run_source(receiver, Diagnostics(), capture=capture,
                   probe=Probe(receiver.send, inspect=True), clock=receiver.monotonic)
        fixture = fixtures / f"MOCK_{profile}.ubx"
        create_fixture(fixture, capture.getvalue())
        cases[profile] = cli(["replay", str(fixture), "--json", str(output / f"mock_{profile}_replay.json")])
        assert cases[profile]["simulated"]
    start = datetime(2026, 3, 5, 23, 59, 59, tzinfo=timezone.utc)
    fixture = fixtures / "MOCK_midnight.ubx"
    create_fixture(fixture, b"".join(synthetic_epoch(start + timedelta(seconds=n)) + synthetic_nav() for n in range(3)))
    midnight = cli(["replay", str(fixture), "--speed", "1", "--record-dir", str(output / "midnight"),
                    "--json", str(output / "midnight.json")])
    assert len(midnight["segments"]) == 2
    assert b"".join(Path(s["path"]).read_bytes() for s in midnight["segments"]) == fixture.read_bytes()
    cases["midnight_realtime"] = midnight
    before = sha256(source)
    cases["real_unpaced"] = cli(["replay", str(source), "--json", str(output / "real_unpaced.json")])
    captured = output / "real_copy.ubx"
    cases["real_accelerated"] = cli(["replay", str(source), "--speed", "1000", "--capture", str(captured),
                                     "--record-dir", str(output / "real_segments"),
                                     "--json", str(output / "real_accelerated.json")])
    real = cases["real_accelerated"]
    assert sha256(source) == before == sha256(captured) == real["stream_sha256"]
    digest = hashlib.sha256()
    for segment in real["segments"]:
        with Path(segment["path"]).open("rb") as stream:
            while data := stream.read(65536):
                digest.update(data)
    assert digest.hexdigest() == before
    assert cases["real_unpaced"]["RAWX"] == real["RAWX"]
    assert cases["real_unpaced"]["messages"] == real["messages"]
    summary = dict(
        source=str(source.resolve()), original_sha256=before,
        source_unchanged=True, capture_hash_equal=True, rotated_bytes_hash_equal=True,
        cases={name: dict(exit_code=r["exit_code"], rawx=r["RAWX"]["epochs"],
                         sfrbx=r["SFRBX"]["messages"], elapsed_s=r["elapsed_s"],
                         simulated=r["simulated"], segments=len(r["segments"])) for name, r in cases.items()})
    (output / "integration_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("--output", type=Path, required=True, help="New directory for reports and byte copies")
    args = parser.parse_args()
    validate(args.source, args.output)

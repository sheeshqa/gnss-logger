"""Stage 2.2 record plus reserved status/verify/recover contracts."""

import json
from pathlib import Path

from . import CONTRACT_VERSION
from .contracts import DeviceBinding, RecordSettings

COMMANDS = ("record", "status", "verify", "recover")
UNAVAILABLE = 4


def usb_id(text):
    import argparse
    try:
        value = int(text, 0)
    except ValueError:
        raise argparse.ArgumentTypeError("Use a decimal number or 0x-prefixed USB ID") from None
    if not 0 <= value <= 65535:
        raise argparse.ArgumentTypeError("USB ID must fit uint16")
    return value


def add_commands(sub, positive):
    description = "Stage 2.1 contracts only. NOT IMPLEMENTED: no hardware access or file operations."
    record = sub.add_parser("record", help="Stage 2.2 primary recording; no rotation/reconnect", description="Stage 2.2 read-only primary byte recording")
    record.add_argument("--output", type=Path, required=True)
    record.add_argument("--port", required=True, help="Initial port hint, never receiver identity")
    record.add_argument("--model", required=True, choices=("ZED-F9P", "ZED-X20P"))
    record.add_argument("--vid", type=usb_id, required=True)
    record.add_argument("--pid", type=usb_id, required=True)
    binding = record.add_mutually_exclusive_group(required=True)
    binding.add_argument("--usb-serial")
    binding.add_argument("--usb-location")
    duration = record.add_mutually_exclusive_group(required=True)
    duration.add_argument("--seconds", type=positive)
    duration.add_argument("--until-stopped", action="store_true")
    record.add_argument("--baud", type=int, default=115200)
    record.add_argument("--expected-hz", type=positive, default=1)
    record.add_argument("--station", default="UBX1")
    record.add_argument("--retention-policy", choices=("keep_all",), default="keep_all")
    record.add_argument("--queue-bytes", type=int, default=8*1024*1024)
    record.add_argument("--sync-interval", type=positive, default=1.0)
    record.add_argument("--identity-timeout", type=positive, default=5.0)
    for name in ("status", "verify", "recover"):
        command = sub.add_parser(name, help="Stage 2 contract only; operation is not implemented", description=description)
        command.add_argument("path", type=Path, help="Stage 2 recording directory; Stage 1 is never migrated in place")
        if name == "recover":
            mode = command.add_mutually_exclusive_group(required=True)
            mode.add_argument("--check", action="store_true", help="Future read-only inspection")
            mode.add_argument("--output", type=Path, help="Future new destination; source stays immutable")


def preview(args, parser):
    """Run record; status/verify/recover remain explicitly unavailable and I/O-free."""
    if args.command == "record":
        try:
            settings = RecordSettings(args.output, args.port,
                DeviceBinding(args.model, args.vid, args.pid, args.usb_serial, args.usb_location),
                args.seconds, args.baud, args.expected_hz, args.station, args.retention_policy)
        except ValueError as error:
            parser.error(str(error))
        if args.queue_bytes<=0:
            parser.error('--queue-bytes must be positive')
        from .controller import RecordController, run_with_signals
        controller=RecordController(queue_bytes=args.queue_bytes,sync_interval=args.sync_interval,
                                    identity_timeout=args.identity_timeout)
        code,report=run_with_signals(controller,settings)
        print(json.dumps(report,ensure_ascii=False,allow_nan=False,indent=2))
        return code
    else:
        parameters = {k: str(v) if isinstance(v, Path) else v for k, v in vars(args).items()}
    print(json.dumps(dict(contract_version=CONTRACT_VERSION, command=args.command,
        status="NOT_IMPLEMENTED", exit_code=UNAVAILABLE, read_only=True,
        hardware_validation="NOT TESTED", parameters=parameters,
        message="Stage 2.1 defines contracts only. No ports opened, input files read, or outputs created."),
        ensure_ascii=False, allow_nan=False, indent=2))
    return UNAVAILABLE

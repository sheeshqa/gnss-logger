"""Read-only receiver operations, usable directly from an application worker.

Progress is an internal application sink receiving detached dictionaries on the
calling thread. A monitor adapter must marshal these to its own UI/event loop;
this API does not accept or dispatch user GUI callbacks.
"""
from copy import deepcopy
from dataclasses import asdict, dataclass, is_dataclass
import math
import time

from ..device.access import open_device
from ..device.ownership import devices, port_key
from ..device.ports import port_info
from ..device.requests import Probe
from ..device.transport import send_read_only
from ..diagnostics import Diagnostics
from ..engine import RunState, run_source
from ..sources import SerialSource


def discover_receivers(inventory=None) -> list:
    """Return host port metadata without opening ports or inferring models.

    Inventory may be an iterable of list-port objects or a zero-argument supplier.
    """
    if inventory is None:
        from serial.tools.list_ports import comports
        inventory = comports
    return [port_info(item) for item in (inventory() if callable(inventory) else inventory)]


@dataclass(frozen=True)
class DiagnosticRequest:
    port: str
    baud: int = 115200
    seconds: float = 60.0
    expected_hz: float = 1.0
    poll_mon_ver: bool = True
    inspect: bool = False
    request_timeout: float = 2.0
    binding: object = None

    def __post_init__(self):
        if not isinstance(self.port, str) or not self.port.strip():
            raise ValueError('An explicit port is required')
        if type(self.baud) is not int or self.baud <= 0:
            raise ValueError('baud must be a positive integer')
        for name in ('seconds', 'expected_hz', 'request_timeout'):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
                raise ValueError(f'{name} must be finite and positive')
        for name in ('poll_mon_ver', 'inspect'):
            if type(getattr(self, name)) is not bool:
                raise ValueError(f'{name} must be boolean')


def result_code(d, probe=None):
    """Shared observation quality semantics: 0 healthy, 2 missing, 3 degraded.

    Operational errors are separately assigned exit code 1 by diagnose.
    """
    if probe and (probe.pending or any(r['status'] in ('nak', 'timeout') for r in probe.results)):
        return 3
    if not d.rawx_count or not d.sfrbx_count or not d.phase_valid or not d.code_valid:
        return 2
    if (d.decode_errors or d.parser.bad_checksums or d.parser.oversize_headers or d.gap_events
            or d.nonmonotonic_epochs or (d.liveness and
                (d.liveness['rawx_stale'] or d.liveness['sfrbx_stale']))):
        return 3
    return 0


def diagnose(request, *, opener=open_device, registry=devices, inventory=None,
             source_factory=SerialSource, clock=time.monotonic, progress=None):
    """Observe one explicit receiver; return an independent application report.

    Opener follows open_device(port, baud, *, identity, registry, resolver) and
    returns a connection with close/read/write. All transmissions pass the
    existing read-only allowlist. No capture/session or process-global handlers.
    """
    d = Diagnostics(request.expected_hz)
    state = RunState()
    probe = None
    errors = []
    observed = None
    binding = {}
    stage = 'inventory'
    try:
        ports = discover_receivers(inventory)
        matches = [p for p in ports if port_key(p['device']) == port_key(request.port)]
        if len(matches) > 1:
            raise ValueError('Explicit port is ambiguous')
        if matches:
            p = matches[0]
            observed = dict(vid=p['vid'], pid=p['pid'], usb_serial=p['serial_number'],
                            usb_location=p['location'])
        binding = request.binding
        if binding is not None:
            binding = asdict(binding) if is_dataclass(binding) else dict(binding)
            if observed is None or any(value is not None and observed.get(key) != value
                    for key, value in binding.items() if key != 'model'):
                raise ValueError('Selected port does not match USB binding')
        stage = 'admission'
        with registry.reserve(request.port, observed) as lease, lease.activate():
            stage = 'open'
            connection = opener(request.port, request.baud, identity=observed,
                                registry=registry, resolver=lambda port: observed)
            try:
                stage = 'source'
                source = source_factory(connection)
                if request.poll_mon_ver or request.inspect:
                    probe = Probe(lambda data: send_read_only(connection, data),
                                  request.request_timeout, request.inspect)
                run_source(source, d, seconds=request.seconds, probe=probe, state=state,
                           clock=clock, progress=(lambda current: progress(deepcopy(current.report())))
                           if progress is not None else None)
            finally:
                try:
                    connection.close()
                except Exception as error:
                    errors.append(dict(component='close', type=type(error).__name__, message=str(error)))
                    if state.stop_reason in ('not_started', 'deadline', 'source_exhausted'):
                        state.stop_reason = 'close_error'
    except Exception as error:
        errors.append(dict(component=stage, type=type(error).__name__, message=str(error)))
        if state.stop_reason in ('not_started', 'deadline', 'source_exhausted'):
            state.stop_reason = stage + '_error'
    report = deepcopy(d.report())
    if isinstance(binding, dict) and binding.get('model') is not None:
        if report['identity'] is None or report['identity'].get('model') != binding['model']:
            errors.append(dict(component='binding', type='ValueError',
                               message='Observed MON-VER identity does not match binding model'))
    warnings = []
    if report['identity'] is None:
        warnings.append('Receiver identity was not observed with MON-VER')
    return deepcopy(dict(identity=report['identity'], usb_identity=observed,
                         diagnostics=report, errors=errors, warnings=warnings,
                         requests=dict(results=probe.results, pending=probe.pending) if probe else None,
                         run=state.report(), exit_code=1 if errors else result_code(d, probe),
                         stop_reason=state.stop_reason))

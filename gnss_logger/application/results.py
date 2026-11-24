"""Detached presentation values; persisted report statuses are never rewritten."""
from copy import deepcopy


def result_lifecycle(report):
    """Map completion only, independently of registration and conversion quality."""
    code = report.get('exit_code', report.get('extensions', {}).get('gnss_logger.recording', {}).get('exit_code', 0))
    if report.get('status') == 'INTERRUPTED' or code == 130:
        return 'CANCELLED'
    return 'FAILED' if code else 'FINISHED'


def present_result(report, operation_type='recording', conversions=(), lifecycle=None,
                   artifacts=()):
    """Return a deep detached view, preserving all raw conversion/registration values."""
    report = deepcopy(report)
    ext = report.get('extensions', {})
    recording = ext.get('gnss_logger.recording', {})
    diagnostics = report.get('diagnostics', ext.get('gnss_logger.diagnostics', report))
    converted = list(deepcopy(conversions))
    if operation_type == 'conversion':
        converted.insert(0, report)
    elif 'gnss_logger.conversion' in ext:
        converted.insert(0, ext['gnss_logger.conversion'])
    segments = list(report.get('segments', []))
    if operation_type != 'conversion':
        for conversion in converted:
            segments.extend(conversion.get('segments', []))
    published = list(report.get('published_files', []))
    for segment in segments:
        outputs = segment.get('published_outputs', [])
        published.extend(outputs.values() if isinstance(outputs, dict) else outputs)
    phase = diagnostics.get('phase', diagnostics.get('phase_quality', {}))
    warnings = list(report.get('warnings', []))
    # Derived warnings retain diagnostic evidence, independently of completion.
    for key in ('decode_errors', 'checksum_errors', 'bad_checksums', 'gap_events', 'gaps'):
        value = diagnostics.get(key, report.get(key))
        if value:
            warnings.append({'diagnostic': key, 'value': value})
    parser = diagnostics.get('parser', report.get('parser', {}))
    if parser.get('bad_checksums'):
        warnings.append({'diagnostic': 'bad_checksums', 'value': parser['bad_checksums']})
    timing = diagnostics.get('timing', {})
    if timing.get('gap_events'):
        warnings.append({'diagnostic': 'gap_events', 'value': timing['gap_events']})
    if isinstance(phase, dict):
        warnings.extend(phase.get('warnings', []))
        if phase.get('status') == 'WARNINGS':
            warnings.append({'diagnostic': 'phase_quality', 'value': phase})
    elif phase == 'WARNINGS':
        warnings.append({'diagnostic': 'phase_quality', 'value': phase})
    if report.get('phase_quality') == 'WARNINGS' and not any(
            isinstance(warning, dict) and warning.get('diagnostic') == 'phase_quality'
            for warning in warnings):
        warnings.append({'diagnostic': 'phase_quality', 'value': 'WARNINGS'})
    return deepcopy(dict(
        operation_type=operation_type, lifecycle=lifecycle or result_lifecycle(report),
        registration=report.get('registration'),
        integrity=report.get('integrity', recording.get('integrity')),
        stop_reason=report.get('stop_reason', recording.get('stop_reason')),
        exit_code=report.get('exit_code', recording.get('exit_code')),
        warnings=warnings,
        phase_quality=report.get('phase_quality', phase.get('status') if isinstance(phase, dict) else phase),
        conversion=converted[0] if converted else None, conversions=converted,
        segments=segments, published_files=published,
        artifacts=list(report.get('artifacts', [])) + list(artifacts),
        diagnostics=diagnostics, identity=report.get('identity'),
        errors=report.get('errors', []), run=report.get('run')))

"""USB identity hints never substitute for MON-VER model detection."""


def port_info(port):
    vid = getattr(port, "vid", None)
    pid = getattr(port, "pid", None)
    vendor_match = vid == 0x1546
    return dict(device=port.device, description=port.description, hwid=port.hwid,
                vid=vid, pid=pid, serial_number=getattr(port, "serial_number", None),
                location=getattr(port, "location", None),
                manufacturer=getattr(port, "manufacturer", None),
                product=getattr(port, "product", None),
                usb_candidate=vendor_match,
                hint="u-blox USB candidate; verify model with MON-VER" if vendor_match else None,
                model=None, model_detection_status="NOT_QUERIED")


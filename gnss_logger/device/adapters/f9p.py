from .base import ReceiverAdapter


class F9PAdapter(ReceiverAdapter):
    model = "ZED-F9P"
    reference = "UBXDOC-963802114-13124 R01, F9 HPG 1.51; installed FW must be verified"
    keys = {"RATE-MEAS": 0x30210001, "RAWX-USB": 0x209102A7, "SFRBX-USB": 0x20910234}

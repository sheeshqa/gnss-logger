from .base import ReceiverAdapter


class X20PAdapter(ReceiverAdapter):
    model = "ZED-X20P"
    reference = ("u-blox/u-blox-X20-interface-description-json/HPG/u-blox-X20-HPG-2.02.json; "
                 "RAWX-DATA1 (payload version 1), SFRBX-DATA2")
    # Independently verified in the X20 specification; not inherited from F9P.
    keys = {"RATE-MEAS": 0x30210001, "RAWX-USB": 0x209102A7, "SFRBX-USB": 0x20910234}

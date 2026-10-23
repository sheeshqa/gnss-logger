"""Read-only adapter contract. No configuration mutation is implemented."""


class ReceiverAdapter:
    model = None
    reference = None
    keys = {}

    def detect(self, identity):
        return bool(identity) and identity.get("model") == self.model

    def inspect_configuration(self):
        return dict(self.keys)  # Explicit documented USB subset for this model.

    def validate_tec_profile(self, snapshot):
        values = snapshot or {}
        return {name: values.get(f"{key:08X}") for name, key in self.keys.items()}

    def apply_tec_profile(self, *args, **kwargs):
        raise PermissionError("Stage 1: physical configuration writes are disabled")

    def start_stream(self):
        return "local_read_only"

    def stop_stream(self):
        return "local_reader_stopped; receiver output unchanged"

    def describe(self):
        return dict(model=self.model, reference=self.reference,
                    hardware_status="NOT TESTED", firmware_support="unverified until device-specific check",
                    config_scope="RAM: RATE-MEAS and USB RAWX/SFRBX only",
                    keys={name: f"{key:08X}" for name, key in self.keys.items()})

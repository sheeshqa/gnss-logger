from .f9p import F9PAdapter
from .x20p import X20PAdapter


def detect_adapter(identity):
    for adapter in (F9PAdapter(), X20PAdapter()):
        if adapter.detect(identity):
            return adapter
    return None

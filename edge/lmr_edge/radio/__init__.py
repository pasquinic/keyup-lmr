from .base import RadioChannel


def make_channel(backend: str, cfg: dict, loop, on_cor, on_audio) -> RadioChannel:
    if backend == "sim":
        from .sim import SimChannel
        return SimChannel(cfg, loop, on_cor, on_audio)
    if backend == "gpio" and cfg.get("signaling") == "serial":
        from .serial_sig import SerialSignalChannel
        return SerialSignalChannel(cfg, loop, on_cor, on_audio)
    if backend == "gpio":
        from .gpio import GpioAudioChannel
        return GpioAudioChannel(cfg, loop, on_cor, on_audio)
    raise ValueError(f"unknown radio backend {backend}")

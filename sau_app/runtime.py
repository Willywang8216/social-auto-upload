"""Optional development runtime startup; production web imports stay inert."""
from myUtils.runtime import embedded_enabled


def start_development_runtime() -> None:
    if not embedded_enabled():
        return
    import sau_backend
    sau_backend._maybe_start_publish_scheduler()
    sau_backend._start_worker_drain_thread()

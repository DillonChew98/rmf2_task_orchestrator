"""Background readiness and dashboard status polling."""

from sila_common.equipment_status import TriosStatusPoller
from sila_common.status_http import ReachabilityCache


class X3dscHealthPoller(TriosStatusPoller):
    def __init__(self, client: object, cache: ReachabilityCache, *, interval_s: float, timeout_s: float) -> None:
        super().__init__(client, cache, kind="x3dsc", interval_s=interval_s, timeout_s=timeout_s)

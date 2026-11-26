from app.connectors.base import Snapshot


class FourthConnector:
    id = "test.fourth"

    def scan(self, external_store_id: str, channel: str, request=None) -> Snapshot:
        return Snapshot([], [], True, "fixture://fourth")

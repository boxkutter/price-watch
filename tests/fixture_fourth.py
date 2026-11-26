from app.connectors.base import Snapshot


class FourthConnector:
    id = "test.fourth"

    def scan(self, external_store_id: str, channel: str) -> Snapshot:
        return Snapshot([], [], True, "fixture://fourth")

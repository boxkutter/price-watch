from sqlalchemy.orm import Session

from app.models import AuditLog


def record(db: Session, actor_id: str | None, action: str,
           target_type: str, target_id: str | None = None,
           details: dict | None = None) -> None:
    db.add(AuditLog(actor_id=actor_id, action=action, target_type=target_type,
                    target_id=target_id, details=details or {}))

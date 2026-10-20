from __future__ import annotations
import json, uuid
from ..db import utcnow

def audit(conn, event_type: str, event_result: str, *, actor=None, invoice_id=None, reason='', details=None,
          risk_level=None, approval_id=None, document_hash=None):
    user = actor or {}
    conn.execute('''INSERT INTO audit_events
        (id, invoice_id, actor_id, actor_role, event_type, event_result, risk_level_at_event,
         event_reason, details_json, related_approval_id, related_document_hash, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
        (str(uuid.uuid4()), invoice_id, user.get('id'), user.get('role'), event_type, event_result,
         risk_level, reason, json.dumps(details or {}, ensure_ascii=False), approval_id, document_hash, utcnow()))

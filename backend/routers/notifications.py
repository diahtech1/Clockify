import hmac
import json
import os
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException

from backend.config import get_now
from backend.database import get_db
from backend.routers.auth import require_admin, require_employee
from backend.schemas import NotificationPreferencesUpdate
from backend.services.notifications import run_notification_jobs

router = APIRouter(prefix="/api/notifications", tags=["Notifications"])


@router.get("/preferences")
def get_notification_preferences(employee_id: int = Depends(require_employee)):
    conn = get_db()
    try:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT channels, quiet_start, quiet_end, marketing_opt_in "
            "FROM notification_preferences WHERE employee_id = ?",
            (employee_id,),
        )
        row = cursor.fetchone()
        if not row:
            return {
                "channels": ["email"],
                "quiet_start": None,
                "quiet_end": None,
                "marketing_opt_in": False,
            }
        try:
            channels = json.loads(row["channels"])
        except (TypeError, ValueError):
            channels = ["email"]
        return {
            "channels": channels,
            "quiet_start": row["quiet_start"],
            "quiet_end": row["quiet_end"],
            "marketing_opt_in": bool(row["marketing_opt_in"]),
        }
    finally:
        conn.close()


@router.put("/preferences")
def update_notification_preferences(
    payload: NotificationPreferencesUpdate,
    employee_id: int = Depends(require_employee),
):
    conn = get_db()
    try:
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO notification_preferences (
                employee_id, channels, quiet_start, quiet_end, marketing_opt_in, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT (employee_id) DO UPDATE SET
                channels = excluded.channels,
                quiet_start = excluded.quiet_start,
                quiet_end = excluded.quiet_end,
                marketing_opt_in = excluded.marketing_opt_in,
                updated_at = excluded.updated_at
            """,
            (
                employee_id,
                json.dumps(payload.channels),
                payload.quiet_start,
                payload.quiet_end,
                int(payload.marketing_opt_in),
                get_now().isoformat(),
            ),
        )
        conn.commit()
        return {
            "status": "success",
            "preferences": payload.model_dump(),
            "message": "Operational notifications use your selected channels and quiet hours. "
            "Marketing messages are separately controlled by marketing_opt_in.",
        }
    finally:
        conn.close()


@router.get("")
def list_notifications(
    limit: int = 50,
    employee_id: int = Depends(require_employee),
):
    if limit < 1 or limit > 100:
        raise HTTPException(status_code=422, detail="Limit must be between 1 and 100.")
    conn = get_db()
    try:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT id, channel, template, category, payload, status, attempts,
                   created_at, sent_at, error
            FROM notifications
            WHERE recipient_id = ?
            ORDER BY created_at DESC, id DESC
            LIMIT ?
            """,
            (employee_id, limit),
        )
        return [
            {
                "id": row["id"],
                "channel": row["channel"],
                "template": row["template"],
                "category": row["category"],
                "payload": json.loads(row["payload"]),
                "status": row["status"],
                "attempts": row["attempts"],
                "created_at": row["created_at"],
                "sent_at": row["sent_at"],
                "error": row["error"],
            }
            for row in cursor.fetchall()
        ]
    finally:
        conn.close()


@router.post("/jobs/run")
def run_scheduled_notifications(
    x_notification_job_key: Optional[str] = Header(None),
):
    expected_key = os.environ.get("NOTIFICATION_JOB_KEY", "").strip()
    if not expected_key:
        raise HTTPException(
            status_code=503,
            detail="Scheduled notifications are disabled until NOTIFICATION_JOB_KEY is configured.",
        )
    if not x_notification_job_key or not hmac.compare_digest(x_notification_job_key, expected_key):
        raise HTTPException(status_code=403, detail="Invalid notification job credential.")
    try:
        return run_notification_jobs()
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.get("/admin/dead-letter", dependencies=[Depends(require_admin)])
def list_dead_letter_notifications(limit: int = 50):
    if limit < 1 or limit > 100:
        raise HTTPException(status_code=422, detail="Limit must be between 1 and 100.")
    conn = get_db()
    try:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT id, recipient_id, channel, template, attempts, created_at, error
            FROM notifications
            WHERE status = 'dead_letter'
            ORDER BY updated_at DESC
            LIMIT ?
            """,
            (limit,),
        )
        return [dict(row) for row in cursor.fetchall()]
    finally:
        conn.close()


@router.post("/admin/dead-letter/{notification_id}/retry", dependencies=[Depends(require_admin)])
def retry_dead_letter_notification(notification_id: int):
    conn = get_db()
    try:
        cursor = conn.cursor()
        now = get_now().isoformat()
        cursor.execute(
            """
            UPDATE notifications
            SET status = 'pending', attempts = 0, error = NULL,
                next_attempt_at = ?, updated_at = ?
            WHERE id = ? AND status = 'dead_letter'
            """,
            (now, now, notification_id),
        )
        if cursor.rowcount != 1:
            raise HTTPException(status_code=404, detail="Dead-letter notification not found.")
        conn.commit()
        return {"status": "queued", "notification_id": notification_id}
    finally:
        conn.close()

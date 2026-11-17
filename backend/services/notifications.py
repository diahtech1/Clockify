"""Outbox scheduling and Knock delivery for operational notifications."""
import hashlib
import hmac
import http.client
import json
import logging
import os
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from typing import Any, Dict, Optional

from backend.config import get_now
from backend.database import get_db
from backend.security import SECRET_KEY

logger = logging.getLogger("clockify.notifications")

MAX_ATTEMPTS = 5
MAX_BATCH_SIZE = 100
CHANNELS = {"email", "sms", "push"}
TEMPLATES = {
    "shift_starts_soon": "Your shift starts in 15 minutes.",
    "missed_clock_in": "You have not clocked in yet. Please open Clockify.",
    "missing_clock_out": "You are still clocked in. Clock out or request a correction.",
    "late_arrival_alert": "A late arrival was recorded. Review the attendance exceptions.",
    "manager_daily_digest": "Daily attendance summary is ready.",
    "manager_weekly_digest": "Weekly attendance summary is ready.",
    "repeated_login_failures": "Repeated failed sign-ins were detected. Review security events.",
    "geofence_changed": "The workplace geofence settings changed. Review the admin console.",
    "payroll_exported": "An attendance data export was requested. Review the admin console.",
}


class NotificationDeliveryError(Exception):
    """A safe, non-sensitive delivery failure code."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _time_on_date(day: str, clock: str) -> datetime:
    return datetime.strptime(f"{day} {clock[:5]}", "%Y-%m-%d %H:%M").replace(
        tzinfo=get_now().tzinfo
    )


def _quiet_hours_end(now: datetime, quiet_start: Optional[str], quiet_end: Optional[str]):
    if not quiet_start or not quiet_end or quiet_start == quiet_end:
        return None

    current_time = now.strftime("%H:%M")
    if quiet_start < quiet_end:
        in_quiet_hours = quiet_start <= current_time < quiet_end
        end_day = now.date()
    else:
        in_quiet_hours = current_time >= quiet_start or current_time < quiet_end
        end_day = now.date() + (timedelta(days=1) if current_time >= quiet_start else timedelta())
    return _time_on_date(end_day.isoformat(), quiet_end) if in_quiet_hours else None


def _get_preferences(cursor, employee_id: int) -> Dict[str, Any]:
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
        "channels": [channel for channel in channels if channel in CHANNELS],
        "quiet_start": row["quiet_start"],
        "quiet_end": row["quiet_end"],
        "marketing_opt_in": bool(row["marketing_opt_in"]),
    }


def enqueue_notification(
    cursor,
    recipient_id: int,
    template: str,
    payload: Dict[str, Any],
    dedupe_key: str,
    now: Optional[datetime] = None,
    category: str = "operational",
) -> int:
    """Insert one outbox item per opted-in channel; duplicate jobs are harmless."""
    if template not in TEMPLATES:
        raise ValueError(f"Unknown notification template: {template}")
    if category not in ("operational", "marketing"):
        raise ValueError("Notification category must be operational or marketing.")

    current = now or get_now()
    preferences = _get_preferences(cursor, recipient_id)
    if category == "marketing" and not preferences["marketing_opt_in"]:
        return 0

    queued = 0
    for channel in preferences["channels"]:
        quiet_end = _quiet_hours_end(
            current, preferences["quiet_start"], preferences["quiet_end"]
        )
        next_attempt = quiet_end or current
        cursor.execute(
            """
            INSERT INTO notifications (
                recipient_id, channel, template, category, payload, status,
                attempts, created_at, updated_at, next_attempt_at, dedupe_key
            ) VALUES (?, ?, ?, ?, ?, 'pending', 0, ?, ?, ?, ?)
            ON CONFLICT (dedupe_key, channel) DO NOTHING
            """,
            (
                recipient_id,
                channel,
                template,
                category,
                json.dumps(payload, separators=(",", ":"), sort_keys=True),
                current.isoformat(),
                current.isoformat(),
                next_attempt.isoformat(),
                dedupe_key,
            ),
        )
        if cursor.rowcount:
            queued += 1
    return queued


def enqueue_admin_notification(
    cursor,
    template: str,
    payload: Dict[str, Any],
    dedupe_key: str,
    now: Optional[datetime] = None,
    exclude_employee_id: Optional[int] = None,
) -> int:
    query = "SELECT id FROM employees WHERE is_admin = 1"
    params = ()
    if exclude_employee_id is not None:
        query += " AND id != ?"
        params = (exclude_employee_id,)
    cursor.execute(query, params)
    return sum(
        enqueue_notification(cursor, row["id"], template, payload, dedupe_key, now)
        for row in cursor.fetchall()
    )


def record_failed_login(identifier: str) -> None:
    """Count failures without retaining usernames, PINs, IP addresses, or request data."""
    normalized = identifier.strip().casefold()
    fingerprint = hmac.new(
        SECRET_KEY.encode("utf-8"),
        b"clockify/auth-failure/v1:" + normalized.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    now = get_now()
    window_cutoff = (now - timedelta(minutes=15)).isoformat()
    conn = get_db()
    try:
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO auth_failure_windows (
                fingerprint, window_started_at, attempts, alert_sent
            ) VALUES (?, ?, 1, 0)
            ON CONFLICT (fingerprint) DO UPDATE SET
                attempts = CASE
                    WHEN auth_failure_windows.window_started_at <= ? THEN 1
                    ELSE auth_failure_windows.attempts + 1
                END,
                alert_sent = CASE
                    WHEN auth_failure_windows.window_started_at <= ? THEN 0
                    ELSE auth_failure_windows.alert_sent
                END,
                window_started_at = CASE
                    WHEN auth_failure_windows.window_started_at <= ? THEN ?
                    ELSE auth_failure_windows.window_started_at
                END
            """,
            (fingerprint, now.isoformat(), window_cutoff, window_cutoff, window_cutoff, now.isoformat()),
        )
        cursor.execute(
            "SELECT window_started_at, attempts, alert_sent "
            "FROM auth_failure_windows WHERE fingerprint = ?",
            (fingerprint,),
        )
        window = cursor.fetchone()
        if window["attempts"] >= 5 and not window["alert_sent"]:
            enqueue_admin_notification(
                cursor,
                "repeated_login_failures",
                {"message": TEMPLATES["repeated_login_failures"], "link": "/admin"},
                f"repeated-login-failures:{fingerprint}:{window['window_started_at']}",
                now,
            )
            cursor.execute(
                "UPDATE auth_failure_windows SET alert_sent = 1 WHERE fingerprint = ?",
                (fingerprint,),
            )
        conn.commit()
    finally:
        conn.close()


def _attendance_summary(cursor, day: str) -> Dict[str, int]:
    cursor.execute("SELECT COUNT(*) AS count FROM employees")
    total_employees = cursor.fetchone()["count"]
    cursor.execute(
        """
        SELECT
            SUM(CASE WHEN status = 'CLOCKED_OUT' THEN 1 ELSE 0 END) AS present,
            SUM(CASE WHEN status = 'CLOCKED_IN' THEN 1 ELSE 0 END) AS partial_presence,
            SUM(CASE WHEN is_late = 1 THEN 1 ELSE 0 END) AS late
        FROM attendance_records
        WHERE date = ?
        """,
        (day,),
    )
    row = cursor.fetchone()
    present = row["present"] or 0
    partial_presence = row["partial_presence"] or 0
    return {
        "total_employees": total_employees,
        "present": present,
        "late": row["late"] or 0,
        "absent": max(0, total_employees - present - partial_presence),
        "partial_presence": partial_presence,
    }


def schedule_notifications(now: Optional[datetime] = None) -> Dict[str, int]:
    """Queue due employee reminders and manager digests; safe to run on every cron tick."""
    current = now or get_now()
    day = current.strftime("%Y-%m-%d")
    conn = get_db()
    counts = {"queued": 0, "employees": 0, "digests": 0}
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT work_start_time, work_end_time FROM geofence_settings ORDER BY id DESC LIMIT 1")
        shift = cursor.fetchone()
        if not shift:
            raise RuntimeError("Cannot schedule notifications without workplace shift settings.")
        start = _time_on_date(day, shift["work_start_time"] or "09:00")
        end = _time_on_date(day, shift["work_end_time"] or "17:00")

        cursor.execute("SELECT id FROM employees WHERE is_admin = 0")
        employees = cursor.fetchall()
        counts["employees"] = len(employees)
        for employee in employees:
            employee_id = employee["id"]
            if start - timedelta(minutes=15) <= current < start - timedelta(minutes=14):
                counts["queued"] += enqueue_notification(
                    cursor,
                    employee_id,
                    "shift_starts_soon",
                    {"message": TEMPLATES["shift_starts_soon"], "link": "/"},
                    f"shift-starts-soon:{employee_id}:{day}",
                    current,
                )
            if current >= start + timedelta(minutes=5):
                cursor.execute(
                    "SELECT id FROM attendance_records WHERE employee_id = ? AND date = ?",
                    (employee_id, day),
                )
                if cursor.fetchone() is None:
                    counts["queued"] += enqueue_notification(
                        cursor,
                        employee_id,
                        "missed_clock_in",
                        {"message": TEMPLATES["missed_clock_in"], "link": "/"},
                        f"missed-clock-in:{employee_id}:{day}",
                        current,
                    )
            if current >= end:
                cursor.execute(
                    "SELECT id FROM attendance_records "
                    "WHERE employee_id = ? AND date = ? AND status = 'CLOCKED_IN'",
                    (employee_id, day),
                )
                if cursor.fetchone():
                    counts["queued"] += enqueue_notification(
                        cursor,
                        employee_id,
                        "missing_clock_out",
                        {"message": TEMPLATES["missing_clock_out"], "link": "/"},
                        f"missing-clock-out:{employee_id}:{day}",
                        current,
                    )

        if current >= end:
            summary = _attendance_summary(cursor, day)
            digest_payload = {
                "message": TEMPLATES["manager_daily_digest"],
                "date": day,
                "summary": summary,
                "link": "/admin",
            }
            counts["queued"] += enqueue_admin_notification(
                cursor, "manager_daily_digest", digest_payload, f"manager-daily-digest:{day}", current
            )
            counts["digests"] += 1

            if current.weekday() == 6:
                week_start = (current.date() - timedelta(days=6)).isoformat()
                cursor.execute(
                    """
                    SELECT
                        COUNT(DISTINCT employee_id) AS employees_present,
                        SUM(CASE WHEN is_late = 1 THEN 1 ELSE 0 END) AS late
                    FROM attendance_records
                    WHERE date >= ? AND date <= ?
                    """,
                    (week_start, day),
                )
                weekly = cursor.fetchone()
                weekly_payload = {
                    "message": TEMPLATES["manager_weekly_digest"],
                    "start_date": week_start,
                    "end_date": day,
                    "summary": {
                        "employees_present": weekly["employees_present"] or 0,
                        "late": weekly["late"] or 0,
                    },
                    "link": "/admin",
                }
                counts["queued"] += enqueue_admin_notification(
                    cursor,
                    "manager_weekly_digest",
                    weekly_payload,
                    f"manager-weekly-digest:{day}",
                    current,
                )
                counts["digests"] += 1

        conn.commit()
        return counts
    finally:
        conn.close()


def _knock_request(
    method: str,
    path: str,
    body: Dict[str, Any],
    idempotency_key: Optional[str] = None,
) -> None:
    api_key = os.environ.get("KNOCK_API_KEY", "").strip()
    if not api_key:
        raise NotificationDeliveryError("knock_not_configured")
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    if idempotency_key:
        headers["Idempotency-Key"] = idempotency_key
    request = urllib.request.Request(
        f"https://api.knock.app/v1/{path.lstrip('/')}",
        data=json.dumps(body).encode("utf-8"),
        headers=headers,
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=8) as response:
            if response.status >= 300:
                raise NotificationDeliveryError(f"provider_http_{response.status}")
    except urllib.error.HTTPError as exc:
        raise NotificationDeliveryError(f"provider_http_{exc.code}") from None
    except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException):
        raise NotificationDeliveryError("provider_unreachable") from None


def _deliver(row, payload: Dict[str, Any]) -> None:
    workflow_key = os.environ.get(
        f"KNOCK_WORKFLOW_{row['template'].upper()}_{row['channel'].upper()}", ""
    ).strip()
    if not workflow_key:
        raise NotificationDeliveryError("workflow_not_configured")

    conn = get_db()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT id, name, email FROM employees WHERE id = ?", (row["recipient_id"],))
        recipient = cursor.fetchone()
        if not recipient:
            raise NotificationDeliveryError("recipient_not_found")
        recipient_id = str(recipient["id"])
        _knock_request(
            "PUT",
            f"users/{urllib.parse.quote(recipient_id, safe='')}",
            {"id": recipient_id, "name": recipient["name"], "email": recipient["email"]},
        )
    finally:
        conn.close()

    _knock_request(
        "POST",
        f"workflows/{urllib.parse.quote(workflow_key, safe='')}/trigger",
        {
            "recipients": [{"id": recipient_id}],
            "data": payload,
        },
        idempotency_key=f"clockify-notification-{row['id']}",
    )


def process_outbox(now: Optional[datetime] = None, limit: int = MAX_BATCH_SIZE) -> Dict[str, int]:
    """Claim and deliver a batch without doing provider I/O in request handlers."""
    current = now or get_now()
    batch_limit = max(1, min(int(limit), MAX_BATCH_SIZE))
    conn = get_db()
    results = {"sent": 0, "retried": 0, "dead_letter": 0, "deferred": 0}
    try:
        cursor = conn.cursor()
        stale_before = (current - timedelta(minutes=5)).isoformat()
        cursor.execute(
            """
            UPDATE notifications
            SET status = 'retrying', next_attempt_at = ?, updated_at = ?
            WHERE status = 'processing' AND updated_at < ?
            """,
            (current.isoformat(), current.isoformat(), stale_before),
        )
        cursor.execute(
            """
            SELECT * FROM notifications
            WHERE status IN ('pending', 'retrying') AND next_attempt_at <= ?
            ORDER BY created_at, id
            LIMIT ?
            """,
            (current.isoformat(), batch_limit),
        )
        rows = cursor.fetchall()
        for row in rows:
            cursor.execute(
                """
                UPDATE notifications
                SET status = 'processing', updated_at = ?
                WHERE id = ? AND status IN ('pending', 'retrying')
                """,
                (current.isoformat(), row["id"]),
            )
            if cursor.rowcount != 1:
                continue
            conn.commit()
            preferences = _get_preferences(cursor, row["recipient_id"])
            if (
                row["channel"] not in preferences["channels"]
                or (row["category"] == "marketing" and not preferences["marketing_opt_in"])
            ):
                cursor.execute(
                    "UPDATE notifications SET status = 'cancelled', updated_at = ? WHERE id = ?",
                    (current.isoformat(), row["id"]),
                )
                conn.commit()
                continue
            quiet_end = _quiet_hours_end(
                current, preferences["quiet_start"], preferences["quiet_end"]
            )
            if quiet_end:
                cursor.execute(
                    """
                    UPDATE notifications
                    SET status = 'pending', next_attempt_at = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (quiet_end.isoformat(), current.isoformat(), row["id"]),
                )
                conn.commit()
                results["deferred"] += 1
                continue

            cursor.execute(
                "UPDATE notifications SET attempts = attempts + 1, updated_at = ? WHERE id = ?",
                (current.isoformat(), row["id"]),
            )
            conn.commit()
            attempts = row["attempts"] + 1
            try:
                _deliver(row, json.loads(row["payload"]))
            except NotificationDeliveryError as exc:
                dead_letter = attempts >= MAX_ATTEMPTS
                delay_seconds = min(30 * (2 ** (attempts - 1)), 6 * 60 * 60)
                retry_at = current + timedelta(seconds=delay_seconds)
                cursor.execute(
                    """
                    UPDATE notifications
                    SET status = ?, error = ?, next_attempt_at = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        "dead_letter" if dead_letter else "retrying",
                        exc.code,
                        retry_at.isoformat(),
                        current.isoformat(),
                        row["id"],
                    ),
                )
                conn.commit()
                results["dead_letter" if dead_letter else "retried"] += 1
                logger.warning(
                    "Notification delivery failed (id=%s, template=%s, error=%s)",
                    row["id"],
                    row["template"],
                    exc.code,
                )
            except (TypeError, ValueError, KeyError) as exc:
                dead_letter = attempts >= MAX_ATTEMPTS
                delay_seconds = min(30 * (2 ** (attempts - 1)), 6 * 60 * 60)
                cursor.execute(
                    """
                    UPDATE notifications
                    SET status = ?, error = 'invalid_notification_payload',
                        next_attempt_at = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        "dead_letter" if dead_letter else "retrying",
                        (current + timedelta(seconds=delay_seconds)).isoformat(),
                        current.isoformat(),
                        row["id"],
                    ),
                )
                conn.commit()
                results["dead_letter" if dead_letter else "retried"] += 1
                logger.warning(
                    "Notification payload rejected (id=%s, error_type=%s)",
                    row["id"],
                    type(exc).__name__,
                )
            else:
                cursor.execute(
                    """
                    UPDATE notifications
                    SET status = 'sent', error = NULL, sent_at = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (current.isoformat(), current.isoformat(), row["id"]),
                )
                conn.commit()
                results["sent"] += 1
        return results
    finally:
        conn.close()


def run_notification_jobs(now: Optional[datetime] = None) -> Dict[str, Dict[str, int]]:
    """Scheduler entry point: enqueue due events then dispatch a bounded outbox batch."""
    scheduled = schedule_notifications(now)
    delivered = process_outbox(now)
    return {"scheduled": scheduled, "delivery": delivered}


if __name__ == "__main__":
    print(json.dumps(run_notification_jobs(), sort_keys=True))

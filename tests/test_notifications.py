import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone, timedelta
from unittest.mock import patch

from backend import database
from backend.services import notifications


WORKPLACE_TZ = timezone(timedelta(hours=1))


class NotificationServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = f"{self.temp_dir.name}/clockify-test.db"
        self.db_path_patch = patch.object(database, "DB_PATH", self.db_path)
        self.db_path_patch.start()
        self.initialized_patch = patch.object(database, "_db_initialized", False)
        self.initialized_patch.start()
        database.init_db()

    def tearDown(self):
        self.initialized_patch.stop()
        self.db_path_patch.stop()
        database._db_initialized = False
        self.temp_dir.cleanup()

    def connect(self):
        return database._connect_sqlite()

    def test_outbox_enqueue_is_idempotent(self):
        now = datetime(2026, 1, 5, 9, 5, tzinfo=WORKPLACE_TZ)
        conn = self.connect()
        cursor = conn.cursor()

        first = notifications.enqueue_notification(
            cursor, 1, "missed_clock_in", {"message": "Clock in", "link": "/"},
            "missed-clock-in:1:2026-01-05", now,
        )
        second = notifications.enqueue_notification(
            cursor, 1, "missed_clock_in", {"message": "Clock in", "link": "/"},
            "missed-clock-in:1:2026-01-05", now,
        )
        conn.commit()
        cursor.execute("SELECT COUNT(*) AS count FROM notifications")
        count = cursor.fetchone()["count"]
        conn.close()

        self.assertEqual(first, 1)
        self.assertEqual(second, 0)
        self.assertEqual(count, 1)

    def test_scheduler_queues_missed_clock_in_once_per_employee(self):
        now = datetime(2026, 1, 5, 9, 10, tzinfo=WORKPLACE_TZ)
        first_run = notifications.schedule_notifications(now)
        second_run = notifications.schedule_notifications(now)
        conn = self.connect()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT COUNT(*) AS count FROM notifications WHERE template = 'missed_clock_in'"
        )
        count = cursor.fetchone()["count"]
        conn.close()

        self.assertEqual(first_run["queued"], 9)
        self.assertEqual(second_run["queued"], 0)
        self.assertEqual(count, 9)

    def test_shift_start_reminder_is_queued_at_fifteen_minutes(self):
        now = datetime(2026, 1, 5, 8, 45, tzinfo=WORKPLACE_TZ)
        first_run = notifications.schedule_notifications(now)
        second_run = notifications.schedule_notifications(now)
        conn = self.connect()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT COUNT(*) AS count FROM notifications WHERE template = 'shift_starts_soon'"
        )
        count = cursor.fetchone()["count"]
        conn.close()

        self.assertEqual(first_run["queued"], 9)
        self.assertEqual(second_run["queued"], 0)
        self.assertEqual(count, 9)

    def test_quiet_hours_defer_delivery_and_retry_with_backoff(self):
        now = datetime(2026, 1, 5, 21, 30, tzinfo=WORKPLACE_TZ)
        conn = self.connect()
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO notification_preferences
                (employee_id, channels, quiet_start, quiet_end, marketing_opt_in, updated_at)
            VALUES (1, '["email"]', '21:00', '07:00', 0, ?)
            """,
            (now.isoformat(),),
        )
        notifications.enqueue_notification(
            cursor, 1, "missed_clock_in", {"message": "Clock in", "link": "/"},
            "missed-clock-in:1:2026-01-05", now,
        )
        conn.commit()
        cursor.execute("SELECT next_attempt_at FROM notifications")
        self.assertEqual(
            cursor.fetchone()["next_attempt_at"],
            datetime(2026, 1, 6, 7, 0, tzinfo=WORKPLACE_TZ).isoformat(),
        )
        conn.close()

        due = datetime(2026, 1, 6, 7, 0, tzinfo=WORKPLACE_TZ)
        with patch.object(
            notifications, "_deliver", side_effect=notifications.NotificationDeliveryError("provider_unreachable")
        ):
            result = notifications.process_outbox(due)
        self.assertEqual(result["retried"], 1)

        conn = self.connect()
        cursor = conn.cursor()
        cursor.execute("SELECT status, attempts, error, next_attempt_at FROM notifications")
        row = cursor.fetchone()
        conn.close()
        self.assertEqual(row["status"], "retrying")
        self.assertEqual(row["attempts"], 1)
        self.assertEqual(row["error"], "provider_unreachable")
        self.assertEqual(
            row["next_attempt_at"],
            (due + timedelta(seconds=30)).isoformat(),
        )

    def test_digest_summary_counts_partial_presence_and_absence(self):
        conn = self.connect()
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO attendance_records (
                employee_id, date, status, is_late
            ) VALUES (1, '2026-01-05', 'CLOCKED_OUT', 1)
            """
        )
        cursor.execute(
            """
            INSERT INTO attendance_records (
                employee_id, date, status, is_late
            ) VALUES (2, '2026-01-05', 'CLOCKED_IN', 0)
            """
        )
        summary = notifications._attendance_summary(cursor, "2026-01-05")
        conn.close()

        self.assertEqual(
            summary,
            {
                "total_employees": 10,
                "present": 1,
                "late": 1,
                "absent": 8,
                "partial_presence": 1,
            },
        )

    def test_daily_digest_is_idempotent_and_renders_correct_counts(self):
        conn = self.connect()
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO attendance_records (
                employee_id, date, status, is_late
            ) VALUES (1, '2026-01-05', 'CLOCKED_OUT', 1)
            """
        )
        cursor.execute(
            """
            INSERT INTO attendance_records (
                employee_id, date, status, is_late
            ) VALUES (2, '2026-01-05', 'CLOCKED_IN', 0)
            """
        )
        conn.commit()
        conn.close()

        end_of_day = datetime(2026, 1, 5, 17, 5, tzinfo=WORKPLACE_TZ)
        notifications.schedule_notifications(end_of_day)
        notifications.schedule_notifications(end_of_day)

        conn = self.connect()
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT payload FROM notifications
            WHERE template = 'manager_daily_digest'
            """
        )
        rows = cursor.fetchall()
        conn.close()
        self.assertEqual(len(rows), 1)
        self.assertEqual(
            json.loads(rows[0]["payload"])["summary"],
            {
                "total_employees": 10,
                "present": 1,
                "late": 1,
                "absent": 8,
                "partial_presence": 1,
            },
        )

    def test_repeated_delivery_failure_moves_to_dead_letter(self):
        now = datetime(2026, 1, 5, 9, 10, tzinfo=WORKPLACE_TZ)
        conn = self.connect()
        cursor = conn.cursor()
        notifications.enqueue_notification(
            cursor, 1, "missed_clock_in", {"message": "Clock in", "link": "/"},
            "missed-clock-in:1:2026-01-05", now,
        )
        cursor.execute("UPDATE notifications SET attempts = 4")
        conn.commit()
        conn.close()

        with patch.object(
            notifications, "_deliver", side_effect=notifications.NotificationDeliveryError("provider_unreachable")
        ):
            result = notifications.process_outbox(now)

        conn = self.connect()
        cursor = conn.cursor()
        cursor.execute("SELECT status, attempts FROM notifications")
        row = cursor.fetchone()
        conn.close()
        self.assertEqual(result["dead_letter"], 1)
        self.assertEqual(row["status"], "dead_letter")
        self.assertEqual(row["attempts"], 5)

    def test_message_payload_has_no_location_fields(self):
        now = datetime(2026, 1, 5, 9, 10, tzinfo=WORKPLACE_TZ)
        notifications.schedule_notifications(now)
        conn = self.connect()
        cursor = conn.cursor()
        cursor.execute("SELECT payload FROM notifications LIMIT 1")
        payload = json.loads(cursor.fetchone()["payload"])
        conn.close()
        self.assertNotIn("latitude", payload)
        self.assertNotIn("longitude", payload)
        self.assertNotIn("face", payload)

    def test_employee_can_update_own_notification_preferences(self):
        from backend.routers.notifications import (
            get_notification_preferences,
            update_notification_preferences,
        )
        from backend.schemas import NotificationPreferencesUpdate

        payload = NotificationPreferencesUpdate(
            channels=["email", "push"],
            quiet_start="22:00",
            quiet_end="07:00",
            marketing_opt_in=True,
        )
        response = update_notification_preferences(payload, employee_id=1)
        preferences = get_notification_preferences(employee_id=1)

        self.assertEqual(response["status"], "success")
        self.assertEqual(preferences["channels"], ["email", "push"])
        self.assertEqual(preferences["quiet_start"], "22:00")
        self.assertTrue(preferences["marketing_opt_in"])

    def test_existing_database_receives_additive_shift_end_migration(self):
        legacy_path = f"{self.temp_dir.name}/legacy.db"
        conn = sqlite3.connect(legacy_path)
        conn.execute(
            """
            CREATE TABLE employees (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                staff_id TEXT UNIQUE NOT NULL,
                name TEXT NOT NULL,
                email TEXT UNIQUE NOT NULL,
                department TEXT NOT NULL,
                pin TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE geofence_settings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                latitude DOUBLE PRECISION NOT NULL,
                longitude DOUBLE PRECISION NOT NULL,
                radius_meters DOUBLE PRECISION NOT NULL,
                work_start_time TEXT DEFAULT '09:00',
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.commit()
        conn.close()

        with patch.object(database, "DB_PATH", legacy_path), patch.object(
            database, "_db_initialized", False
        ):
            database.init_db()
            conn = database._connect_sqlite()
            cursor = conn.cursor()
            cursor.execute("PRAGMA table_info(geofence_settings)")
            geofence_columns = {row["name"] for row in cursor.fetchall()}
            cursor.execute("PRAGMA table_info(employees)")
            employee_columns = {row["name"] for row in cursor.fetchall()}
            cursor.execute("SELECT work_end_time FROM geofence_settings")
            work_end = cursor.fetchone()["work_end_time"]
            conn.close()

        self.assertIn("work_end_time", geofence_columns)
        self.assertIn("is_admin", employee_columns)
        self.assertEqual(work_end, "17:00")


if __name__ == "__main__":
    unittest.main()

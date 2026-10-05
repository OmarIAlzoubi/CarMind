"""SQLite persistence only. Automotive decisions belong to the application engines."""

from contextlib import contextmanager
from dataclasses import asdict
from datetime import date, datetime
import json
from pathlib import Path
import sqlite3

from carmind.contracts import MaintenanceRecord, VehicleContext, VehicleProfile
from carmind.ownership import OwnedVehicle, PROFILE_FIELDS, identifier, utc


SCHEMA = """
CREATE TABLE owners (id TEXT PRIMARY KEY, created_at TEXT NOT NULL);
CREATE TABLE vehicles (
 id TEXT PRIMARY KEY, owner_id TEXT NOT NULL REFERENCES owners(id),
 make TEXT NOT NULL, model TEXT NOT NULL, year INTEGER NOT NULL CHECK(year > 0),
 engine TEXT, vin TEXT, trim TEXT, market TEXT, nickname TEXT,
 odometer_unit TEXT NOT NULL CHECK(odometer_unit IN ('km','mi')),
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE odometer_events (
 id TEXT PRIMARY KEY, vehicle_id TEXT NOT NULL REFERENCES vehicles(id),
 km REAL NOT NULL CHECK(km >= 0), reading REAL NOT NULL CHECK(reading >= 0),
 unit TEXT NOT NULL CHECK(unit IN ('km','mi')), occurred_at TEXT NOT NULL,
 source TEXT NOT NULL, message_id TEXT, created_at TEXT NOT NULL,
 superseded_at TEXT, replacement_id TEXT);
CREATE INDEX odometer_vehicle_time ON odometer_events(vehicle_id, occurred_at);
CREATE TABLE services (
 id TEXT PRIMARY KEY, vehicle_id TEXT NOT NULL REFERENCES vehicles(id),
 service_type TEXT NOT NULL, performed_at TEXT NOT NULL, odometer_km REAL,
 notes TEXT, source TEXT NOT NULL, message_id TEXT, created_at TEXT NOT NULL,
 superseded_at TEXT, replacement_id TEXT);
CREATE INDEX service_vehicle_time ON services(vehicle_id, performed_at);
CREATE TABLE sessions (
 id TEXT PRIMARY KEY, owner_id TEXT NOT NULL REFERENCES owners(id),
 active_vehicle_id TEXT REFERENCES vehicles(id), created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE turns (
 session_id TEXT NOT NULL REFERENCES sessions(id), message_id TEXT NOT NULL,
 vehicle_id TEXT NOT NULL REFERENCES vehicles(id), text TEXT NOT NULL,
 timestamp TEXT NOT NULL, response TEXT NOT NULL, status TEXT NOT NULL,
 summary TEXT, PRIMARY KEY(session_id, message_id));
CREATE TABLE commands (
 id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(id),
 vehicle_id TEXT NOT NULL REFERENCES vehicles(id), message_id TEXT NOT NULL,
 payload TEXT NOT NULL, created_at TEXT NOT NULL, expires_at TEXT NOT NULL,
 state TEXT NOT NULL, result TEXT, UNIQUE(session_id, message_id));
CREATE TABLE reminders (
 id TEXT PRIMARY KEY, vehicle_id TEXT NOT NULL REFERENCES vehicles(id),
 lifecycle TEXT NOT NULL CHECK(lifecycle IN ('ACTIVE','ACKNOWLEDGED','COMPLETED')),
 facts TEXT NOT NULL, reason TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE safety_constraints (
 vehicle_id TEXT PRIMARY KEY REFERENCES vehicles(id), decision TEXT NOT NULL, assessed_at TEXT NOT NULL);
CREATE TABLE channel_bindings (
 channel TEXT NOT NULL, external_user_id TEXT NOT NULL,
 owner_id TEXT NOT NULL REFERENCES owners(id), session_id TEXT NOT NULL REFERENCES sessions(id),
 PRIMARY KEY(channel, external_user_id));
CREATE TABLE external_receipts (
 channel TEXT NOT NULL, external_user_id TEXT NOT NULL, external_message_id TEXT NOT NULL,
 response TEXT NOT NULL, PRIMARY KEY(channel, external_user_id, external_message_id),
 FOREIGN KEY(channel, external_user_id) REFERENCES channel_bindings(channel, external_user_id));
CREATE TABLE vehicle_drafts (
 id TEXT PRIMARY KEY, owner_id TEXT NOT NULL REFERENCES owners(id),
 session_id TEXT NOT NULL REFERENCES sessions(id), message_id TEXT NOT NULL,
 payload TEXT NOT NULL, expires_at TEXT NOT NULL, state TEXT NOT NULL,
 result_vehicle_id TEXT, UNIQUE(session_id, message_id));
PRAGMA user_version = 2;
"""

MIGRATION_V2 = """
CREATE TABLE channel_bindings (
 channel TEXT NOT NULL, external_user_id TEXT NOT NULL,
 owner_id TEXT NOT NULL REFERENCES owners(id), session_id TEXT NOT NULL REFERENCES sessions(id),
 PRIMARY KEY(channel, external_user_id));
CREATE TABLE external_receipts (
 channel TEXT NOT NULL, external_user_id TEXT NOT NULL, external_message_id TEXT NOT NULL,
 response TEXT NOT NULL, PRIMARY KEY(channel, external_user_id, external_message_id),
 FOREIGN KEY(channel, external_user_id) REFERENCES channel_bindings(channel, external_user_id));
CREATE TABLE vehicle_drafts (
 id TEXT PRIMARY KEY, owner_id TEXT NOT NULL REFERENCES owners(id),
 session_id TEXT NOT NULL REFERENCES sessions(id), message_id TEXT NOT NULL,
 payload TEXT NOT NULL, expires_at TEXT NOT NULL, state TEXT NOT NULL,
 result_vehicle_id TEXT, UNIQUE(session_id, message_id));
PRAGMA user_version = 2;
"""

MIGRATION_V3 = """
CREATE TABLE notification_preferences (
 owner_id TEXT PRIMARY KEY REFERENCES owners(id), enabled INTEGER NOT NULL DEFAULT 0,
 preferred_channel TEXT NOT NULL DEFAULT 'web', timezone TEXT NOT NULL DEFAULT 'UTC',
 language TEXT NOT NULL DEFAULT 'en', quiet_enabled INTEGER NOT NULL DEFAULT 1,
 quiet_start TEXT NOT NULL DEFAULT '22:00', quiet_end TEXT NOT NULL DEFAULT '08:00',
 odometer_followups_enabled INTEGER NOT NULL DEFAULT 0, updated_at TEXT NOT NULL);
CREATE TABLE owner_reminders (
 id TEXT PRIMARY KEY, owner_id TEXT NOT NULL REFERENCES owners(id),
 vehicle_id TEXT NOT NULL REFERENCES vehicles(id), maintenance_item TEXT NOT NULL,
 due_km REAL, due_date TEXT, created_at TEXT NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('ACTIVE','RESOLVED','CANCELLED')),
 resolved_by_service_id TEXT);
CREATE INDEX owner_reminders_vehicle ON owner_reminders(vehicle_id,status);
CREATE TABLE proactive_events (
 id TEXT PRIMARY KEY, owner_id TEXT NOT NULL REFERENCES owners(id),
 vehicle_id TEXT NOT NULL REFERENCES vehicles(id), event_type TEXT NOT NULL,
 source_type TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('OPEN','ACKNOWLEDGED','RESOLVED')),
 priority TEXT NOT NULL, maintenance_item TEXT, source_id TEXT, rule_id TEXT,
 source_page TEXT, due_km REAL, due_date TEXT, current_mileage REAL,
 reason TEXT NOT NULL, evidence TEXT NOT NULL, dedupe_key TEXT NOT NULL UNIQUE,
 created_at TEXT NOT NULL, effective_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE INDEX proactive_events_vehicle ON proactive_events(vehicle_id,status);
CREATE TABLE notification_outbox (
 id TEXT PRIMARY KEY, event_id TEXT NOT NULL REFERENCES proactive_events(id),
 owner_id TEXT NOT NULL REFERENCES owners(id), vehicle_id TEXT NOT NULL REFERENCES vehicles(id),
 channel TEXT NOT NULL, event_type TEXT NOT NULL, created_at TEXT NOT NULL,
 scheduled_for TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('PENDING','DEFERRED','SENT','FAILED','CANCELLED')),
 attempt_count INTEGER NOT NULL DEFAULT 0, last_attempt_at TEXT,
 sequence INTEGER NOT NULL, UNIQUE(event_id,event_type,sequence));
CREATE INDEX notification_outbox_due ON notification_outbox(status,scheduled_for);
PRAGMA user_version = 3;
"""


def encode(value) -> str:
    return json.dumps(value, default=lambda x: x.isoformat() if isinstance(x, (date, datetime)) else x.value,
                      sort_keys=True, allow_nan=False)


class OwnershipStore:
    """One local connection. Callers own transaction boundaries, including refresh."""

    def __init__(self, path):
        if isinstance(path, (str, Path)):
            name = str(path)
            if name and name != ":memory:" and not name.lower().startswith("file:") and "://" not in name:
                parent = Path(name).parent
                if parent != Path("."):
                    parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys = ON")
        version = self.db.execute("PRAGMA user_version").fetchone()[0]
        if version == 0:
            self.db.executescript("BEGIN IMMEDIATE;\n" + SCHEMA + "\nCOMMIT;")
        elif version == 1:
            self.db.executescript("BEGIN IMMEDIATE;\n" + MIGRATION_V2 + "\nCOMMIT;")
        elif version not in (2, 3):
            self.db.close()
            raise ValueError("Unsupported ownership database version.")
        if version in (0, 1, 2):
            self.db.executescript("BEGIN IMMEDIATE;\n" + MIGRATION_V3 + "\nCOMMIT;")

    def close(self):
        self.db.close()

    @contextmanager
    def transaction(self):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    def create_owner(self, owner_id, now):
        self.db.execute("INSERT INTO owners VALUES (?,?)", (identifier(owner_id), utc(now).isoformat()))

    def add_vehicle(self, owner_id, profile, now, *, trim=None, market=None, nickname=None, unit="km"):
        stamp = utc(now).isoformat()
        self.db.execute("INSERT INTO vehicles VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (profile.vehicle_id, owner_id, profile.make, profile.model, profile.year,
                         profile.engine, profile.vin, trim, market, nickname, unit, stamp, stamp))

    def vehicle(self, owner_id, vehicle_id, now) -> OwnedVehicle:
        row = self.db.execute("SELECT * FROM vehicles WHERE id=? AND owner_id=?", (vehicle_id, owner_id)).fetchone()
        if row is None:
            raise ValueError("Vehicle is not available for this owner.")
        reading = self.db.execute("SELECT km FROM odometer_events WHERE vehicle_id=? AND superseded_at IS NULL AND occurred_at<=? ORDER BY occurred_at DESC, created_at DESC, id DESC LIMIT 1",
                                  (vehicle_id, utc(now).isoformat())).fetchone()
        profile = VehicleProfile(vehicle_id, row["make"], row["model"], row["year"],
                                 reading[0] if reading else None, row["engine"], row["vin"])
        return OwnedVehicle(profile, owner_id, row["trim"], row["market"], row["nickname"], row["odometer_unit"],
                            datetime.fromisoformat(row["created_at"]), datetime.fromisoformat(row["updated_at"]))

    def owner_for_vehicle(self, vehicle_id):
        row = self.db.execute("SELECT owner_id FROM vehicles WHERE id=?", (vehicle_id,)).fetchone()
        if row is None:
            raise ValueError("Vehicle is not registered")
        return row["owner_id"]

    def all_vehicle_ids(self):
        return tuple(row["id"] for row in self.db.execute("SELECT id FROM vehicles ORDER BY id"))

    def create_session(self, session_id, owner_id, now, vehicle_id=None):
        if vehicle_id is not None:
            self.vehicle(owner_id, vehicle_id, now)
        stamp = utc(now).isoformat()
        self.db.execute("INSERT INTO sessions VALUES (?,?,?,?,?)", (identifier(session_id), owner_id, vehicle_id, stamp, stamp))

    def session(self, session_id, owner_id):
        row = self.db.execute("SELECT * FROM sessions WHERE id=? AND owner_id=?", (session_id, owner_id)).fetchone()
        if row is None:
            raise ValueError("Session is not available for this owner.")
        return dict(row)

    def select_vehicle(self, session_id, owner_id, vehicle_id, now):
        self.session(session_id, owner_id)
        self.vehicle(owner_id, vehicle_id, now)
        self.db.execute("UPDATE sessions SET active_vehicle_id=?, updated_at=? WHERE id=?", (vehicle_id, utc(now).isoformat(), session_id))

    def set_vehicle_field(self, vehicle_id, name, value, now):
        if name not in PROFILE_FIELDS:
            raise ValueError("Vehicle field is not writable.")
        # Column name is constrained above; user values always remain parameters.
        self.db.execute(f"UPDATE vehicles SET {name}=?, updated_at=? WHERE id=?", (value, utc(now).isoformat(), vehicle_id))

    def touch_vehicle(self, vehicle_id, now):
        self.db.execute("UPDATE vehicles SET updated_at=? WHERE id=?", (utc(now).isoformat(), vehicle_id))

    def odometer_events(self, vehicle_id):
        return [dict(row) for row in self.db.execute("SELECT * FROM odometer_events WHERE vehicle_id=? AND superseded_at IS NULL ORDER BY occurred_at,id", (vehicle_id,))]

    def record_odometer(self, event_id, vehicle_id, km, reading, unit, occurred_at, now, message_id):
        self.db.execute("INSERT INTO odometer_events VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                        (event_id, vehicle_id, km, reading, unit, utc(occurred_at).isoformat(),
                         "USER_REPORTED_CONFIRMED", message_id, utc(now).isoformat(), None, None))
        self.db.execute("UPDATE vehicles SET updated_at=? WHERE id=?", (utc(now).isoformat(), vehicle_id))

    def record_service(self, event_id, vehicle_id, record, now, message_id):
        self.db.execute("INSERT INTO services VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                        (event_id, vehicle_id, record.service_type, utc(record.performed_at).isoformat(),
                         record.odometer_km, record.notes, "USER_REPORTED_CONFIRMED", message_id,
                         utc(now).isoformat(), None, None))

    def supersede(self, table, event_id, vehicle_id, replacement_id, now):
        if table not in ("services", "odometer_events"):
            raise ValueError("Unsupported correction type.")
        cursor = self.db.execute(f"UPDATE {table} SET superseded_at=?, replacement_id=? WHERE id=? AND vehicle_id=? AND superseded_at IS NULL",
                                 (utc(now).isoformat(), replacement_id, event_id, vehicle_id))
        if cursor.rowcount != 1:
            raise ValueError("The record to correct is missing or already superseded.")

    def service_records(self, vehicle_id, now, limit=None):
        sql = "SELECT * FROM services WHERE vehicle_id=? AND superseded_at IS NULL AND performed_at<=? ORDER BY performed_at DESC,id DESC"
        args = [vehicle_id, utc(now).isoformat()]
        if limit is not None:
            sql += " LIMIT ?"
            args.append(limit)
        return [MaintenanceRecord(row["id"], row["service_type"], datetime.fromisoformat(row["performed_at"]), row["odometer_km"], row["notes"])
                for row in self.db.execute(sql, args)]

    def vehicle_context(self, owner_id, vehicle_id, now, *, limit=None):
        return VehicleContext(self.vehicle(owner_id, vehicle_id, now).profile, self.service_records(vehicle_id, now, limit))

    def add_proposal(self, proposal_id, session_id, vehicle_id, message_id, command, now, expires_at,
                     preconditions=None):
        payload = asdict(command)
        if preconditions:
            payload["preconditions"] = preconditions
        self.db.execute("INSERT INTO commands VALUES (?,?,?,?,?,?,?,?,?)",
                        (proposal_id, session_id, vehicle_id, message_id, encode(payload), utc(now).isoformat(),
                         utc(expires_at).isoformat(), "PENDING", None))

    def proposal(self, proposal_id, session_id):
        row = self.db.execute("SELECT * FROM commands WHERE id=? AND session_id=?", (proposal_id, session_id)).fetchone()
        return dict(row) if row else None

    def proposal_for_message(self, session_id, message_id):
        row = self.db.execute("SELECT * FROM commands WHERE session_id=? AND message_id=?", (session_id, message_id)).fetchone()
        return dict(row) if row else None

    def finish_proposal(self, proposal_id, result):
        # Keep structured fact arguments/audit, but release the retained chat quote.
        row = self.db.execute("SELECT payload FROM commands WHERE id=?", (proposal_id,)).fetchone()
        payload = json.loads(row[0])
        payload.pop("owner_quote", None)
        self.db.execute("UPDATE commands SET state=?, result=?, payload=? WHERE id=?",
                        ("APPLIED" if result.applied else "REJECTED", encode(asdict(result)), encode(payload), proposal_id))

    def expire_proposals(self, now):
        for row in self.db.execute("SELECT id,payload FROM commands WHERE state='PENDING' AND expires_at<?", (utc(now).isoformat(),)).fetchall():
            payload = json.loads(row["payload"])
            payload.pop("owner_quote", None)
            self.db.execute("UPDATE commands SET state='EXPIRED',payload=? WHERE id=?", (encode(payload), row["id"]))

    def remember_turn(self, session_id, vehicle_id, message, response, status, summary=None):
        self.db.execute("INSERT INTO turns VALUES (?,?,?,?,?,?,?,?)",
                        (session_id, message.message_id, vehicle_id, message.text[:2000], utc(message.timestamp).isoformat(),
                         response[:4000], status, encode(summary) if summary else None))
        self.db.execute("DELETE FROM turns WHERE session_id=? AND message_id NOT IN (SELECT message_id FROM turns WHERE session_id=? ORDER BY rowid DESC LIMIT 6)", (session_id, session_id))

    def recent_turns(self, session_id, vehicle_id):
        return [dict(row) for row in self.db.execute("SELECT * FROM turns WHERE session_id=? AND vehicle_id=? ORDER BY rowid", (session_id, vehicle_id))]

    def remembered_turn(self, session_id, message_id):
        row = self.db.execute("SELECT * FROM turns WHERE session_id=? AND message_id=?", (session_id, message_id)).fetchone()
        return dict(row) if row else None

    def reminders(self, vehicle_id, *, active_only=False):
        sql = "SELECT * FROM reminders WHERE vehicle_id=?"
        if active_only:
            sql += " AND lifecycle!='COMPLETED'"
        return [{**dict(row), "facts": json.loads(row["facts"])} for row in self.db.execute(sql + " ORDER BY id", (vehicle_id,))]

    def save_reminder(self, reminder_id, vehicle_id, facts, lifecycle, reason, now):
        stamp = utc(now).isoformat()
        self.db.execute("INSERT INTO reminders VALUES (?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET lifecycle=excluded.lifecycle,facts=excluded.facts,reason=excluded.reason,updated_at=excluded.updated_at",
                        (reminder_id, vehicle_id, lifecycle, encode(facts), reason, stamp, stamp))

    def save_stop(self, vehicle_id, decision, now):
        self.db.execute("INSERT INTO safety_constraints VALUES (?,?,?) ON CONFLICT(vehicle_id) DO UPDATE SET decision=excluded.decision, assessed_at=excluded.assessed_at",
                        (vehicle_id, encode(asdict(decision)), utc(now).isoformat()))

    def previous_stop(self, vehicle_id):
        row = self.db.execute("SELECT * FROM safety_constraints WHERE vehicle_id=?", (vehicle_id,)).fetchone()
        return dict(row) if row else None

    def vehicles(self, owner_id, now):
        return [self.vehicle(owner_id, row["id"], now) for row in self.db.execute(
            "SELECT id FROM vehicles WHERE owner_id=? ORDER BY created_at,id", (owner_id,))]

    def binding(self, channel, external_user_id):
        row = self.db.execute("SELECT * FROM channel_bindings WHERE channel=? AND external_user_id=?",
                              (channel, external_user_id)).fetchone()
        return dict(row) if row else None

    def bind_channel(self, channel, external_user_id, owner_id, session_id):
        self.session(session_id, owner_id)
        self.db.execute("INSERT INTO channel_bindings VALUES (?,?,?,?)",
                        (channel, external_user_id, owner_id, session_id))

    def receipt(self, channel, external_user_id, external_message_id):
        row = self.db.execute("SELECT response FROM external_receipts WHERE channel=? AND external_user_id=? AND external_message_id=?",
                              (channel, external_user_id, external_message_id)).fetchone()
        return json.loads(row[0]) if row else None

    def save_receipt(self, channel, external_user_id, external_message_id, response):
        self.db.execute("INSERT INTO external_receipts VALUES (?,?,?,?)",
                        (channel, external_user_id, external_message_id, encode(response)))

    def vehicle_draft_for_message(self, session_id, message_id):
        row = self.db.execute("SELECT * FROM vehicle_drafts WHERE session_id=? AND message_id=?",
                              (session_id, message_id)).fetchone()
        return dict(row) if row else None

    def vehicle_draft(self, draft_id, session_id):
        row = self.db.execute("SELECT * FROM vehicle_drafts WHERE id=? AND session_id=?",
                              (draft_id, session_id)).fetchone()
        return dict(row) if row else None

    def save_vehicle_draft(self, draft_id, owner_id, session_id, message_id, payload, expires_at):
        self.session(session_id, owner_id)
        self.db.execute("INSERT INTO vehicle_drafts VALUES (?,?,?,?,?,?,?,?)",
                        (draft_id, owner_id, session_id, message_id, encode(payload),
                         utc(expires_at).isoformat(), "PENDING", None))

    def finish_vehicle_draft(self, draft_id, vehicle_id):
        self.db.execute("UPDATE vehicle_drafts SET state='APPLIED',result_vehicle_id=? WHERE id=?",
                        (vehicle_id, draft_id))

    def cancel_pending_proposal(self, session_id, proposal_id):
        """Dismiss an exact, session-owned pending change without applying it."""
        table = "vehicle_drafts" if proposal_id.startswith("vehicle-") else "commands"
        changed = self.db.execute(
            f"UPDATE {table} SET state='CANCELLED' WHERE id=? AND session_id=? AND state='PENDING'",
            (proposal_id, session_id)).rowcount
        return changed == 1

    def pending_for_session(self, session_id, now):
        stamp = utc(now).isoformat()
        commands = [row[0] for row in self.db.execute(
            "SELECT id FROM commands WHERE session_id=? AND state='PENDING' AND expires_at>=?",
            (session_id, stamp))]
        drafts = [row[0] for row in self.db.execute(
            "SELECT id FROM vehicle_drafts WHERE session_id=? AND state='PENDING' AND expires_at>=?",
            (session_id, stamp))]
        return tuple(commands + drafts)

    def notification_preferences(self, owner_id):
        row = self.db.execute("SELECT * FROM notification_preferences WHERE owner_id=?", (owner_id,)).fetchone()
        return dict(row) if row else None

    def save_notification_preferences(self, owner_id, values, now):
        self.db.execute("""INSERT INTO notification_preferences VALUES (?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(owner_id) DO UPDATE SET enabled=excluded.enabled,
            preferred_channel=excluded.preferred_channel,timezone=excluded.timezone,
            language=excluded.language,quiet_enabled=excluded.quiet_enabled,
            quiet_start=excluded.quiet_start,quiet_end=excluded.quiet_end,
            odometer_followups_enabled=excluded.odometer_followups_enabled,updated_at=excluded.updated_at""",
            (owner_id, int(values["enabled"]), values["preferred_channel"], values["timezone"],
             values["language"], int(values["quiet_enabled"]), values["quiet_start"],
             values["quiet_end"], int(values["odometer_followups_enabled"]), utc(now).isoformat()))

    def save_owner_reminder(self, reminder_id, owner_id, vehicle_id, item, due_km, due_date, now):
        self.db.execute("INSERT INTO owner_reminders VALUES (?,?,?,?,?,?,?,?,?)",
                        (reminder_id, owner_id, vehicle_id, item, due_km,
                         due_date.isoformat() if due_date else None, utc(now).isoformat(), "ACTIVE", None))

    def owner_reminders(self, owner_id, vehicle_id):
        return [dict(row) for row in self.db.execute(
            "SELECT * FROM owner_reminders WHERE owner_id=? AND vehicle_id=? AND status!='CANCELLED' ORDER BY id",
            (owner_id, vehicle_id))]

    def set_owner_reminder_resolution(self, reminder_id, service_id):
        self.db.execute("UPDATE owner_reminders SET status=?,resolved_by_service_id=? WHERE id=?",
                        ("RESOLVED" if service_id else "ACTIVE", service_id, reminder_id))

    def proactive_events(self, *, owner_id=None, vehicle_id=None):
        clauses, args = [], []
        if owner_id is not None:
            clauses.append("owner_id=?")
            args.append(owner_id)
        if vehicle_id is not None:
            clauses.append("vehicle_id=?")
            args.append(vehicle_id)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        return [{**dict(row), "evidence": json.loads(row["evidence"])} for row in self.db.execute(
            "SELECT * FROM proactive_events" + where + " ORDER BY created_at,id", args)]

    def proactive_event(self, event_id, owner_id, vehicle_id):
        row = self.db.execute("SELECT * FROM proactive_events WHERE id=? AND owner_id=? AND vehicle_id=?",
                              (event_id, owner_id, vehicle_id)).fetchone()
        return {**dict(row), "evidence": json.loads(row["evidence"])} if row else None

    def save_proactive_event(self, event):
        self.db.execute("""INSERT INTO proactive_events VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(id) DO UPDATE SET event_type=excluded.event_type,status=excluded.status,
            priority=excluded.priority,current_mileage=excluded.current_mileage,
            reason=excluded.reason,evidence=excluded.evidence,effective_at=excluded.effective_at,
            updated_at=excluded.updated_at""",
            (event["id"], event["owner_id"], event["vehicle_id"], event["event_type"],
             event["source_type"], event["status"], event["priority"], event.get("maintenance_item"),
             event.get("source_id"), event.get("rule_id"), event.get("source_page"),
             event.get("due_km"), event.get("due_date"), event.get("current_mileage"),
             event["reason"], encode(event["evidence"]), event["dedupe_key"], event["created_at"],
             event["effective_at"], event["updated_at"]))

    def set_proactive_event_status(self, event_id, status, now):
        self.db.execute("UPDATE proactive_events SET status=?,updated_at=? WHERE id=?",
                        (status, utc(now).isoformat(), event_id))

    def notifications(self, *, owner_id=None, vehicle_id=None, event_id=None, statuses=None):
        clauses, args = [], []
        if owner_id is not None:
            clauses.append("owner_id=?")
            args.append(owner_id)
        if vehicle_id is not None:
            clauses.append("vehicle_id=?")
            args.append(vehicle_id)
        if event_id is not None:
            clauses.append("event_id=?")
            args.append(event_id)
        if statuses:
            clauses.append("status IN (" + ",".join("?" for _ in statuses) + ")")
            args.extend(statuses)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        return [dict(row) for row in self.db.execute(
            "SELECT * FROM notification_outbox" + where + " ORDER BY created_at,id", args)]

    def save_notification(self, notification):
        self.db.execute("""INSERT INTO notification_outbox
            (id,event_id,owner_id,vehicle_id,channel,event_type,created_at,scheduled_for,status,attempt_count,last_attempt_at,sequence)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (notification["id"], notification["event_id"], notification["owner_id"],
             notification["vehicle_id"], notification["channel"], notification["event_type"],
             notification["created_at"], notification["scheduled_for"], notification["status"],
             notification["attempt_count"], notification.get("last_attempt_at"), notification["sequence"]))

    def set_notification_status(self, notification_id, status, *, scheduled_for=None, attempted_at=None):
        self.db.execute("""UPDATE notification_outbox SET status=?,scheduled_for=COALESCE(?,scheduled_for),
            attempt_count=attempt_count+?,last_attempt_at=COALESCE(?,last_attempt_at) WHERE id=?""",
            (status, scheduled_for, 1 if attempted_at else 0, attempted_at, notification_id))

    def latest_sent_event_id(self, owner_id, vehicle_id, since):
        row = self.db.execute("""SELECT n.event_id FROM notification_outbox n
            JOIN proactive_events e ON e.id=n.event_id WHERE n.owner_id=? AND n.vehicle_id=?
            AND n.status='SENT' AND n.last_attempt_at>=? AND e.status!='RESOLVED'
            ORDER BY n.last_attempt_at DESC,n.id DESC LIMIT 1""",
            (owner_id, vehicle_id, utc(since).isoformat())).fetchone()
        return row[0] if row else None

    def channel_binding_for_owner(self, owner_id, channel):
        row = self.db.execute("SELECT external_user_id FROM channel_bindings WHERE owner_id=? AND channel=? ORDER BY external_user_id LIMIT 1",
                              (owner_id, channel)).fetchone()
        return row[0] if row else None

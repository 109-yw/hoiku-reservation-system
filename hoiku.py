from __future__ import annotations

import csv
from email.message import EmailMessage
import hashlib
import io
import os
import secrets
import sqlite3
import smtplib
import ssl
import subprocess
import tempfile
import threading
import time
import zipfile
from contextlib import closing
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from flask import (
    Flask,
    Response,
    flash,
    g,
    jsonify,
    redirect,
    render_template,
    request,
    send_from_directory,
    session,
)
from jinja2 import FileSystemLoader
from werkzeug.security import check_password_hash, generate_password_hash


ROOT = Path(__file__).resolve().parent

SCHEMA_PATH = ROOT / "SQL" / "childcare_v2.sql"
CSV_ARCHIVE_PATH = ROOT / "SQL" / "csv_export_all.zip"
CSV_DATASET_VERSION = 1
CSV_TABLE_IMPORT_ORDER = (
    "settings",
    "guardians",
    "age_classes",
    "operating_days",
    "children",
    "reservations",
    "waitlist_offers",
    "notifications",
    "reservation_hours",
    "cancellations",
    "attendance",
    "payments",
    "point_ledger",
    "admin_users",
    "cancel_policies",
)
COURSES = {
    "morning": (9, 13),
    "afternoon": (13, 17),
    "full_day": (9, 17),
}


class ApiError(Exception):
    def __init__(self, message: str, status: int) -> None:
        super().__init__(message)
        self.status = status


def initialize_database(database_path: str | Path) -> None:
    path = Path(database_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if not SCHEMA_PATH.is_file():
        raise FileNotFoundError(f"SQLite schema not found: {SCHEMA_PATH}")
    if not CSV_ARCHIVE_PATH.is_file():
        raise FileNotFoundError(f"CSV archive not found: {CSV_ARCHIVE_PATH}")

    if path.is_file():
        with closing(sqlite3.connect(path)) as connection:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version == CSV_DATASET_VERSION:
                return
            existing_tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master "
                    "WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
                )
            }
            required_tables = {
                "guardians",
                "children",
                "operating_days",
                "reservations",
                "settings",
            }
            if existing_tables and not required_tables.issubset(existing_tables):
                raise RuntimeError(
                    f"Database contains unrelated tables and will not be replaced: {path}"
                )

    with tempfile.NamedTemporaryFile(
        prefix=f"{path.stem}.",
        suffix=".tmp",
        dir=path.parent,
        delete=False,
    ) as temporary:
        staged_path = Path(temporary.name)

    try:
        with closing(sqlite3.connect(staged_path)) as connection:
            connection.execute("PRAGMA foreign_keys = ON")
            connection.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
            connection.execute("PRAGMA foreign_keys = OFF")

            table_names = [
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master "
                    "WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
                )
            ]
            for table in table_names:
                connection.execute(f'DELETE FROM "{table}"')
            connection.execute("DELETE FROM sqlite_sequence")
            connection.execute("PRAGMA foreign_keys = ON")

            with zipfile.ZipFile(CSV_ARCHIVE_PATH) as archive:
                if set(CSV_TABLE_IMPORT_ORDER) != set(table_names):
                    raise ValueError(
                        "CSV import order does not cover the SQL schema tables"
                    )
                csv_members = {
                    Path(member.filename).stem: member
                    for member in archive.infolist()
                    if member.filename.lower().endswith(".csv")
                }
                if set(csv_members) != set(table_names):
                    raise ValueError(
                        "CSV archive tables do not match the SQL schema: "
                        f"expected {sorted(table_names)}, got {sorted(csv_members)}"
                    )

                for table in CSV_TABLE_IMPORT_ORDER:
                    member = csv_members[table]
                    text = archive.read(member).decode("utf-8-sig")
                    reader = csv.DictReader(io.StringIO(text, newline=""))
                    columns = [
                        row[1]
                        for row in connection.execute(
                            f'PRAGMA table_info("{table}")'
                        )
                    ]
                    if reader.fieldnames != columns:
                        raise ValueError(
                            f"CSV columns do not match the {table} table schema"
                        )
                    not_null_columns = {
                        row[1]
                        for row in connection.execute(
                            f'PRAGMA table_info("{table}")'
                        )
                        if row[3] or row[5]
                    }
                    placeholders = ", ".join("?" for _ in columns)
                    column_list = ", ".join(f'"{column}"' for column in columns)
                    insert_sql = (
                        f'INSERT INTO "{table}" ({column_list}) '
                        f"VALUES ({placeholders})"
                    )
                    rows = [
                        tuple(
                            value if value != "" or column in not_null_columns else None
                            for column, value in row.items()
                        )
                        for row in reader
                    ]
                    connection.executemany(insert_sql, rows)
                    imported_count = connection.execute(
                        f'SELECT COUNT(*) FROM "{table}"'
                    ).fetchone()[0]
                    if imported_count != len(rows):
                        raise RuntimeError(
                            f"Imported {imported_count} of {len(rows)} "
                            f"CSV rows for table {table}"
                        )

            violations = connection.execute("PRAGMA foreign_key_check").fetchall()
            if violations:
                raise RuntimeError(f"CSV data violates foreign keys: {violations[:5]}")
            connection.execute(f"PRAGMA user_version = {CSV_DATASET_VERSION}")
            connection.commit()

        os.replace(staged_path, path)
    except Exception:
        if staged_path.exists():
            staged_path.unlink()
        raise


def sync_guardian_passwords(database_path: str | Path) -> None:
    csv_path = ROOT / "SQL" / "guardians.csv"
    if not csv_path.is_file():
        raise FileNotFoundError(f"Required CSV file not found: {csv_path}")

    csv_contents = csv_path.read_bytes()
    csv_fingerprint = hashlib.sha256(csv_contents).hexdigest()

    with closing(sqlite3.connect(database_path, timeout=10)) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("BEGIN IMMEDIATE")
        try:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS app_metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
                """
            )
            previous_fingerprint = connection.execute(
                "SELECT value FROM app_metadata WHERE key = 'guardians_csv_sha256'"
            ).fetchone()
            if previous_fingerprint is not None and previous_fingerprint[0] == csv_fingerprint:
                connection.commit()
                return

            reader = csv.DictReader(
                io.StringIO(csv_contents.decode("utf-8-sig"), newline="")
            )
            required_columns = {"guardian_id", "email", "password"}
            if reader.fieldnames is None or not required_columns.issubset(
                reader.fieldnames
            ):
                raise ValueError(f"CSV has missing password columns: {csv_path}")

            updates: list[tuple[int, str, str, str]] = []
            seen_ids: set[int] = set()
            seen_emails: set[str] = set()
            for row in reader:
                try:
                    guardian_id = int(row["guardian_id"])
                except (TypeError, ValueError) as error:
                    raise ValueError(
                        "guardians.csv contains an invalid guardian_id"
                    ) from error
                email = (row["email"] or "").strip().casefold()
                password = row["password"] or ""
                if not email or not password:
                    raise ValueError(
                        "guardians.csv contains an empty email or password"
                    )
                if guardian_id in seen_ids or email in seen_emails:
                    raise ValueError("guardians.csv contains duplicate IDs or emails")
                seen_ids.add(guardian_id)
                seen_emails.add(email)

                if password.startswith(("scrypt:", "pbkdf2:")):
                    password_hash = password
                else:
                    password_hash = generate_password_hash(password)
                updates.append((guardian_id, email, password, password_hash))

            for guardian_id, email, password, password_hash in updates:
                guardian = connection.execute(
                    """
                    SELECT password_hash FROM guardians
                    WHERE guardian_id = ? AND email = ?
                    """,
                    (guardian_id, email),
                ).fetchone()
                if guardian is None:
                    raise ValueError(
                        "guardians.csv ID/email does not match the database "
                        f"for guardian_id {guardian_id}"
                    )

                current_hash = guardian[0]
                if current_hash == password_hash:
                    continue
                if (
                    not password.startswith(("scrypt:", "pbkdf2:"))
                    and current_hash.startswith(("scrypt:", "pbkdf2:"))
                    and check_password_hash(current_hash, password)
                ):
                    continue
                connection.execute(
                    """
                    UPDATE guardians SET password_hash = ?
                    WHERE guardian_id = ? AND email = ?
                    """,
                    (password_hash, guardian_id, email),
                )
            connection.execute(
                """
                INSERT INTO app_metadata (key, value)
                VALUES ('guardians_csv_sha256', ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (csv_fingerprint,),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise


def sync_child_karte_statuses(database_path: str | Path) -> None:
    csv_path = ROOT / "SQL" / "children.csv"
    if not csv_path.is_file():
        raise FileNotFoundError(f"Required CSV file not found: {csv_path}")

    updates: list[tuple[int, int, str, str | None]] = []
    seen_child_ids: set[int] = set()
    with csv_path.open(encoding="utf-8-sig", newline="") as source:
        reader = csv.DictReader(source)
        required_columns = {
            "child_id",
            "guardian_id",
            "karte_status",
            "karte_reviewed_at",
        }
        if reader.fieldnames is None or not required_columns.issubset(
            reader.fieldnames
        ):
            raise ValueError(f"CSV has missing child-karte columns: {csv_path}")

        for row in reader:
            try:
                child_id = int(row["child_id"])
                guardian_id = int(row["guardian_id"])
            except (TypeError, ValueError) as error:
                raise ValueError("children.csv contains an invalid child or guardian ID") from error
            status = row["karte_status"] or ""
            if status not in {"draft", "submitted", "approved", "rejected"}:
                raise ValueError(f"children.csv contains an invalid karte_status: {status}")
            if child_id in seen_child_ids:
                raise ValueError(f"children.csv contains duplicate child_id: {child_id}")
            seen_child_ids.add(child_id)
            updates.append(
                (child_id, guardian_id, status, row["karte_reviewed_at"] or None)
            )

    with closing(sqlite3.connect(database_path, timeout=10)) as connection:
        connection.execute("BEGIN IMMEDIATE")
        try:
            for child_id, guardian_id, status, reviewed_at in updates:
                cursor = connection.execute(
                    """
                    UPDATE children
                    SET karte_status = ?, karte_reviewed_at = ?
                    WHERE child_id = ? AND guardian_id = ?
                    """,
                    (status, reviewed_at, child_id, guardian_id),
                )
                if cursor.rowcount != 1:
                    raise ValueError(
                        "children.csv child/guardian IDs do not match the database "
                        f"for child_id {child_id}"
                    )
            connection.commit()
        except Exception:
            connection.rollback()
            raise


def initialize_reservation_attempts(database_path: str | Path) -> None:
    with closing(sqlite3.connect(database_path, timeout=10)) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS reservation_attempts (
                reservation_id INTEGER PRIMARY KEY,
                attempted_at_ms INTEGER NOT NULL,
                FOREIGN KEY (reservation_id)
                    REFERENCES reservations(reservation_id) ON DELETE CASCADE
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS waitlist_dispatches (
                dispatch_id INTEGER PRIMARY KEY AUTOINCREMENT,
                cancelled_reservation_id INTEGER NOT NULL UNIQUE,
                care_date TEXT NOT NULL,
                start_hour INTEGER NOT NULL,
                end_hour INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending', 'filled', 'exhausted')),
                created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
                resolved_at TEXT,
                FOREIGN KEY (cancelled_reservation_id)
                    REFERENCES reservations(reservation_id)
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS waitlist_dispatch_participants (
                dispatch_id INTEGER NOT NULL,
                reservation_id INTEGER NOT NULL,
                offered INTEGER NOT NULL DEFAULT 0 CHECK (offered IN (0, 1)),
                PRIMARY KEY (dispatch_id, reservation_id),
                FOREIGN KEY (dispatch_id)
                    REFERENCES waitlist_dispatches(dispatch_id) ON DELETE CASCADE,
                FOREIGN KEY (reservation_id)
                    REFERENCES reservations(reservation_id) ON DELETE CASCADE
            )
            """
        )
        offer_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(waitlist_offers)")
        }
        if "dispatch_id" not in offer_columns:
            connection.execute(
                """
                ALTER TABLE waitlist_offers
                ADD COLUMN dispatch_id INTEGER REFERENCES waitlist_dispatches(dispatch_id)
                """
            )
        connection.commit()


def create_app(database_path: str | Path | None = None) -> Flask:
    app = Flask(__name__)
    app.secret_key = os.environ.get("HOIKU_SECRET_KEY") or secrets.token_hex(32)
    app.config.update(
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
    )
    app.jinja_loader = FileSystemLoader(ROOT / "html")
    app.config["DATABASE"] = str(
        database_path or os.environ.get("HOIKU_DATABASE", ROOT / "childcare.db")
    )
    initialize_database(app.config["DATABASE"])
    initialize_reservation_attempts(app.config["DATABASE"])
    sync_guardian_passwords(app.config["DATABASE"])
    sync_child_karte_statuses(app.config["DATABASE"])

    @app.get("/")
    def home() -> Response:
        return redirect("/html/hoiku.html")

    @app.get("/html/<path:filename>")
    def html_page(filename: str) -> Response:
        if filename == "login.html":
            return redirect("/login")
        if filename in {"reservation.html", "waiting.html"} and not isinstance(
            session.get("guardian_id"), int
        ):
            return redirect("/login")
        return send_from_directory(ROOT / "html", filename)

    @app.route("/login", methods=["GET", "POST"])
    def login() -> Response | str:
        if request.method == "GET":
            if session.get("guardian_id") is not None:
                return redirect("/html/user_home.html")
            return render_template("login.html")

        session.clear()
        email = request.form.get("email", "").strip().casefold()
        password = request.form.get("password", "")
        guardian = get_database().execute(
            "SELECT guardian_id, password_hash FROM guardians WHERE email = ?",
            (email,),
        ).fetchone()
        password_hash = guardian["password_hash"] if guardian is not None else ""
        password_matches = (
            password_hash.startswith(("scrypt:", "pbkdf2:"))
            and check_password_hash(password_hash, password)
        )
        if not password_matches:
            flash("メールアドレスまたはパスワードが正しくありません。", "error")
            return redirect("/login")

        session.clear()
        session["guardian_id"] = guardian["guardian_id"]
        return redirect("/html/user_home.html")

    @app.post("/register")
    def register() -> Response:
        name = request.form.get("name", "").strip()
        phone = request.form.get("phone", "").strip()
        email = request.form.get("email", "").strip().casefold()
        password = request.form.get("password", "")
        if not name or not phone or not email:
            flash("氏名・電話番号・メールアドレスを入力してください。", "error")
            return redirect("/login")
        if len(password) < 8:
            flash("パスワードは8文字以上で設定してください。", "error")
            return redirect("/login")

        try:
            get_database().execute(
                """
                INSERT INTO guardians (name, phone, email, password_hash)
                VALUES (?, ?, ?, ?)
                """,
                (name, phone, email, generate_password_hash(password)),
            )
        except sqlite3.IntegrityError:
            flash("このメールアドレスはすでに登録されています。", "error")
            return redirect("/login")

        flash("登録しました。登録したメールアドレスとパスワードでログインしてください。", "success")
        return redirect("/login")

    @app.get("/mypage")
    @app.get("/html/user_home.html")
    def mypage() -> Response:
        guardian_id = session.get("guardian_id")
        if not isinstance(guardian_id, int):
            return redirect("/login")

        connection = get_database()
        connection.execute("BEGIN IMMEDIATE")
        try:
            notification_ids = _expire_waitlist_offers(connection)
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        for offer_id in notification_ids:
            _send_waitlist_offer_email(app, app.config["DATABASE"], offer_id)

        guardian = connection.execute(
            "SELECT name, priority_points FROM guardians WHERE guardian_id = ?",
            (guardian_id,),
        ).fetchone()
        if guardian is None:
            session.clear()
            return redirect("/login")

        children = connection.execute(
            """
            SELECT child_id, name, birth_date, karte_status
            FROM children
            WHERE guardian_id = ?
            ORDER BY child_id
            """,
            (guardian_id,),
        ).fetchall()
        rows = connection.execute(
            """
            SELECT r.reservation_id, c.name AS child_name,
                   r.care_date, r.start_hour, r.end_hour,
                   r.status
            FROM reservations r
            JOIN children c ON c.child_id = r.child_id
            WHERE c.guardian_id = ?
              AND (r.status = 'confirmed'
                   OR r.status = 'waitlisted'
                   OR (r.status = 'held'
                       AND r.hold_expires_at > datetime('now', 'localtime')))
            ORDER BY r.care_date, r.start_hour
            """,
            (guardian_id,),
        ).fetchall()
        reservations = [
            {
                "id": row["reservation_id"],
                "date": row["care_date"],
                "time": f"{row['start_hour']}:00～{row['end_hour']}:00",
                "child_name": row["child_name"],
                "status": {
                    "held": "仮押さえ中",
                    "confirmed": "予約済み",
                    "waitlisted": "キャンセル待ち",
                }[row["status"]],
            }
            for row in rows
        ]
        pending_offers = connection.execute(
            """
            SELECT o.offer_id, o.respond_by, r.care_date,
                   r.start_hour, r.end_hour, c.name AS child_name
            FROM waitlist_offers o
            JOIN reservations r ON r.reservation_id = o.reservation_id
            JOIN children c ON c.child_id = r.child_id
            WHERE c.guardian_id = ? AND o.status = 'pending'
            ORDER BY o.respond_by, o.offer_id
            """,
            (guardian_id,),
        ).fetchall()
        return render_template(
            "user_home.html",
            name=guardian["name"],
            children=children,
            reservations=reservations,
            points=guardian["priority_points"],
            pending_offers=pending_offers,
        )

    @app.get("/logout")
    def logout() -> Response:
        session.clear()
        return redirect("/login")

    @app.post("/cancel")
    def cancel_reservation() -> Response:
        guardian_id = session.get("guardian_id")
        try:
            reservation_id = int(request.form.get("reservation_id", ""))
        except ValueError:
            flash("予約番号が正しくありません。", "error")
            return redirect("/mypage")

        connection = get_database()
        connection.execute("BEGIN IMMEDIATE")
        try:
            notification_ids = _expire_waitlist_offers(connection)
            reservation = connection.execute(
                """
                SELECT r.status
                FROM reservations r
                JOIN children c ON c.child_id = r.child_id
                WHERE r.reservation_id = ? AND c.guardian_id = ?
                """,
                (reservation_id, guardian_id),
            ).fetchone()
            if reservation is None or reservation["status"] not in {
                "held",
                "confirmed",
                "waitlisted",
            }:
                raise ApiError("キャンセル可能な予約が見つかりません。", 404)

            offer_id = _cancel_reservation_and_dispatch(connection, reservation_id)
            if offer_id is not None:
                notification_ids.append(offer_id)
            connection.commit()
        except ApiError as error:
            connection.rollback()
            flash(str(error), "error")
            return redirect("/mypage")
        except Exception:
            connection.rollback()
            raise
        for notification_offer_id in notification_ids:
            _send_waitlist_offer_email(
                app, app.config["DATABASE"], notification_offer_id
            )
        flash("予約をキャンセルしました。", "success")
        return redirect("/mypage")

    @app.teardown_appcontext
    def close_database(_: BaseException | None = None) -> None:
        connection = g.pop("database", None)
        if connection is not None:
            connection.close()

    def get_database() -> sqlite3.Connection:
        connection = g.get("database")
        if connection is None:
            connection = sqlite3.connect(
                app.config["DATABASE"], timeout=10, isolation_level=None
            )
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            g.database = connection
        return connection

    def error_response(message: str, status: int) -> tuple[Response, int]:
        return jsonify(error=message), status

    @app.get("/api/health")
    def health() -> Response:
        return jsonify(status="ok")

    @app.get("/api/availability")
    def availability() -> Response | tuple[Response, int]:
        care_date = request.args.get("care_date", "")
        try:
            date.fromisoformat(care_date)
        except ValueError:
            return error_response("care_date は YYYY-MM-DD 形式で指定してください。", 400)

        rows = get_database().execute(
            """
            SELECT hour, children_count, staff_used, children_remaining, load_status
            FROM v_hourly_load
            WHERE care_date = ?
            ORDER BY hour
            """,
            (care_date,),
        ).fetchall()
        if not rows:
            return error_response("指定日の開所情報がありません。", 404)
        return jsonify(
            care_date=care_date,
            slots=[dict(row) for row in rows],
        )

    @app.get("/api/waitlist/order")
    def waitlist_order() -> Response:
        connection = get_database()
        connection.execute("BEGIN IMMEDIATE")
        try:
            notification_ids = _expire_waitlist_offers(connection)
            rows = connection.execute(
                """
                SELECT r.reservation_id
                FROM reservations r
                JOIN reservation_attempts a ON a.reservation_id = r.reservation_id
                JOIN children c ON c.child_id = r.child_id
                JOIN guardians g ON g.guardian_id = c.guardian_id
                WHERE r.status = 'waitlisted'
                ORDER BY g.priority_points DESC, a.attempted_at_ms ASC,
                         r.reservation_id ASC
                """
            ).fetchall()
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        for offer_id in notification_ids:
            _send_waitlist_offer_email(app, app.config["DATABASE"], offer_id)
        return jsonify(reservation_ids=[row["reservation_id"] for row in rows])

    @app.get("/api/children")
    def guardian_children() -> tuple[Response, int] | Response:
        guardian_id = session.get("guardian_id")
        if not isinstance(guardian_id, int):
            return error_response("ログインが必要です。", 401)
        rows = get_database().execute(
            """
            SELECT child_id, name
            FROM children
            WHERE guardian_id = ? AND karte_status = 'approved'
            ORDER BY child_id
            """,
            (guardian_id,),
        ).fetchall()
        return jsonify(children=[dict(row) for row in rows])

    @app.post("/api/waitlist/offers/<int:offer_id>/accept")
    def accept_waitlist_offer(offer_id: int) -> tuple[Response, int] | Response:
        guardian_id = session.get("guardian_id")
        if not isinstance(guardian_id, int):
            return error_response("ログインが必要です。", 401)

        connection = get_database()
        connection.execute("BEGIN IMMEDIATE")
        try:
            offer = connection.execute(
                """
                SELECT o.status, o.respond_by, o.dispatch_id, r.reservation_id
                FROM waitlist_offers o
                JOIN reservations r ON r.reservation_id = o.reservation_id
                JOIN children c ON c.child_id = r.child_id
                WHERE o.offer_id = ? AND c.guardian_id = ?
                """,
                (offer_id, guardian_id),
            ).fetchone()
            if offer is None:
                raise ApiError("有効なキャンセル待ちオファーがありません。", 404)
            notification_ids = _expire_waitlist_offers(connection)
            offer = connection.execute(
                """
                SELECT o.status, o.respond_by, o.dispatch_id, r.reservation_id
                FROM waitlist_offers o
                JOIN reservations r ON r.reservation_id = o.reservation_id
                JOIN children c ON c.child_id = r.child_id
                WHERE o.offer_id = ? AND c.guardian_id = ?
                """,
                (offer_id, guardian_id),
            ).fetchone()
            if offer["status"] == "expired":
                connection.commit()
                for expired_offer_id in notification_ids:
                    _send_waitlist_offer_email(
                        app, app.config["DATABASE"], expired_offer_id
                    )
                return error_response("オファーの回答期限が切れました。", 410)
            if offer["status"] != "pending":
                connection.commit()
                for expired_offer_id in notification_ids:
                    _send_waitlist_offer_email(
                        app, app.config["DATABASE"], expired_offer_id
                    )
                return error_response("有効なキャンセル待ちオファーがありません。", 404)
            if offer["respond_by"] <= datetime.now().strftime("%Y-%m-%d %H:%M:%S"):
                notification_ids.extend(_expire_waitlist_offers(connection))
                connection.commit()
                for expired_offer_id in notification_ids:
                    _send_waitlist_offer_email(
                        app, app.config["DATABASE"], expired_offer_id
                    )
                return error_response("オファーの回答期限が切れました。", 410)

            connection.execute(
                """
                UPDATE waitlist_offers
                SET status = 'accepted', responded_at = datetime('now', 'localtime')
                WHERE offer_id = ? AND status = 'pending'
                """,
                (offer_id,),
            )
            connection.execute(
                """
                UPDATE reservations
                SET status = 'confirmed', hold_expires_at = NULL,
                    updated_at = datetime('now', 'localtime')
                WHERE reservation_id = ?
                """,
                (offer["reservation_id"],),
            )
            if offer["dispatch_id"] is not None:
                _finish_waitlist_dispatch(
                    connection, offer["dispatch_id"], "filled"
                )
            balance = connection.execute(
                "SELECT priority_points FROM guardians WHERE guardian_id = ?",
                (guardian_id,),
            ).fetchone()["priority_points"]
            _record_points(
                connection,
                guardian_id,
                -balance,
                "waitlist_promoted",
                offer["reservation_id"],
            )
            connection.commit()
        except ApiError as error:
            connection.rollback()
            return error_response(str(error), error.status)
        except Exception:
            connection.rollback()
            raise

        for expired_offer_id in notification_ids:
            _send_waitlist_offer_email(app, app.config["DATABASE"], expired_offer_id)
        return jsonify(
            offer_id=offer_id,
            reservation_id=offer["reservation_id"],
            status="confirmed",
            priority_points=0,
        )

    @app.post("/api/waitlist/offers/<int:offer_id>/decline")
    def decline_waitlist_offer(offer_id: int) -> tuple[Response, int] | Response:
        guardian_id = session.get("guardian_id")
        if not isinstance(guardian_id, int):
            return error_response("ログインが必要です。", 401)

        connection = get_database()
        connection.execute("BEGIN IMMEDIATE")
        try:
            offer = connection.execute(
                """
                SELECT o.status, o.dispatch_id, r.reservation_id
                FROM waitlist_offers o
                JOIN reservations r ON r.reservation_id = o.reservation_id
                JOIN children c ON c.child_id = r.child_id
                WHERE o.offer_id = ? AND c.guardian_id = ?
                """,
                (offer_id, guardian_id),
            ).fetchone()
            if offer is None:
                raise ApiError("有効なキャンセル待ちオファーがありません。", 404)
            notification_ids = _expire_waitlist_offers(connection)
            offer = connection.execute(
                """
                SELECT o.status, o.dispatch_id, r.reservation_id
                FROM waitlist_offers o
                JOIN reservations r ON r.reservation_id = o.reservation_id
                JOIN children c ON c.child_id = r.child_id
                WHERE o.offer_id = ? AND c.guardian_id = ?
                """,
                (offer_id, guardian_id),
            ).fetchone()
            if offer["status"] != "pending":
                connection.commit()
                for next_offer_id in notification_ids:
                    _send_waitlist_offer_email(
                        app, app.config["DATABASE"], next_offer_id
                    )
                return error_response(
                    "有効なキャンセル待ちオファーがありません。", 404
                )

            connection.execute(
                """
                UPDATE waitlist_offers
                SET status = 'declined', responded_at = datetime('now', 'localtime')
                WHERE offer_id = ? AND status = 'pending'
                """,
                (offer_id,),
            )
            connection.execute(
                """
                UPDATE reservations
                SET status = 'waitlisted', hold_expires_at = NULL,
                    updated_at = datetime('now', 'localtime')
                WHERE reservation_id = ?
                """,
                (offer["reservation_id"],),
            )
            connection.execute(
                "DELETE FROM reservation_hours WHERE reservation_id = ?",
                (offer["reservation_id"],),
            )
            if offer["dispatch_id"] is not None:
                next_offer_id = _advance_waitlist_dispatch(
                    connection, offer["dispatch_id"]
                )
                if next_offer_id is not None:
                    notification_ids.append(next_offer_id)
            connection.commit()
        except ApiError as error:
            connection.rollback()
            return error_response(str(error), error.status)
        except Exception:
            connection.rollback()
            raise
        for next_offer_id in notification_ids:
            _send_waitlist_offer_email(app, app.config["DATABASE"], next_offer_id)
        return jsonify(offer_id=offer_id, status="declined")

    @app.get("/api/availability/month")
    def monthly_availability() -> Response | tuple[Response, int]:
        try:
            year = int(request.args.get("year", ""))
            month = int(request.args.get("month", ""))
            month_start = date(year, month, 1)
            if month == 12:
                next_month = date(year + 1, 1, 1)
            else:
                next_month = date(year, month + 1, 1)
        except ValueError:
            return error_response("year と month は有効な年月で指定してください。", 400)

        rows = get_database().execute(
            """
            SELECT od.care_date, od.is_open,
                   MIN(v.children_remaining) AS min_remaining,
                   MAX(v.children_remaining) AS max_remaining
            FROM operating_days od
            LEFT JOIN v_hourly_load v ON v.care_date = od.care_date
            WHERE od.care_date >= ? AND od.care_date < ?
            GROUP BY od.care_date, od.is_open
            ORDER BY od.care_date
            """,
            (month_start.isoformat(), next_month.isoformat()),
        ).fetchall()

        days = []
        for row in rows:
            if row["is_open"] != 1:
                status = "closed"
            elif row["max_remaining"] <= 0:
                status = "full"
            elif row["min_remaining"] <= 3:
                status = "few"
            else:
                status = "open"
            days.append(
                {
                    "care_date": row["care_date"],
                    "status": status,
                    "remaining_min": row["min_remaining"],
                    "remaining_max": row["max_remaining"],
                }
            )
        return jsonify(year=year, month=month, days=days)

    @app.post("/api/holds")
    def create_hold() -> tuple[Response, int]:
        payload: Any = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return error_response("JSON形式のリクエストが必要です。", 400)

        child_id = _integer(payload.get("child_id"))
        course = payload.get("course")
        care_date = payload.get("care_date")
        if child_id is None:
            return error_response("child_id は整数で指定してください。", 400)
        if not isinstance(course, str) or course not in COURSES:
            return error_response(
                "course は morning、afternoon、full_day のいずれかを指定してください。",
                400,
            )
        start_hour, end_hour = COURSES[course]
        if not isinstance(care_date, str):
            return error_response("care_date は YYYY-MM-DD 形式で指定してください。", 400)
        try:
            booking_date = date.fromisoformat(care_date)
        except ValueError:
            return error_response("care_date は YYYY-MM-DD 形式で指定してください。", 400)
        if booking_date.isoformat() != care_date:
            return error_response("care_date は YYYY-MM-DD 形式で指定してください。", 400)
        connection = get_database()
        connection.execute("BEGIN IMMEDIATE")
        try:
            expired_offer_ids = _expire_waitlist_offers(connection)
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        for expired_offer_id in expired_offer_ids:
            _send_waitlist_offer_email(
                app, app.config["DATABASE"], expired_offer_id
            )
        connection.execute("BEGIN IMMEDIATE")
        try:
            day_row = connection.execute(
                "SELECT is_open FROM operating_days WHERE care_date = ?", (care_date,)
            ).fetchone()
            if day_row is None or day_row["is_open"] != 1:
                raise ApiError("指定日は開所日ではありません。", 400)

            child = connection.execute(
                """
                SELECT birth_date, karte_status, guardian_id
                FROM children WHERE child_id = ?
                """,
                (child_id,),
            ).fetchone()
            if child is None:
                raise ApiError("児童が見つかりません。", 404)
            guardian_id = session.get("guardian_id")
            if not isinstance(guardian_id, int):
                raise ApiError("ログインが必要です。", 401)
            if child["guardian_id"] != guardian_id:
                raise ApiError("自分の世帯に登録されている児童を選択してください。", 403)
            if child["karte_status"] != "approved":
                raise ApiError("承認済みの児童カルテが必要です。", 409)

            connection.execute(
                """
                UPDATE reservations
                SET status = 'expired', hold_expires_at = NULL,
                    updated_at = datetime('now', 'localtime')
                WHERE child_id = ? AND care_date = ?
                  AND status = 'held'
                  AND hold_expires_at <= datetime('now', 'localtime')
                """,
                (child_id, care_date),
            )
            connection.execute(
                """
                DELETE FROM reservation_hours
                WHERE reservation_id IN (
                    SELECT reservation_id FROM reservations
                    WHERE child_id = ? AND care_date = ? AND status = 'expired'
                )
                """,
                (child_id, care_date),
            )

            birth_date = date.fromisoformat(child["birth_date"])
            age_months = (
                (booking_date.year - birth_date.year) * 12
                + booking_date.month
                - birth_date.month
                - (booking_date.day < birth_date.day)
            )
            age_class = connection.execute(
                """
                SELECT age_class_id, staff_cost, hourly_fee_yen
                FROM age_classes
                WHERE min_age_months <= ? AND max_age_months > ?
                """,
                (age_months, age_months),
            ).fetchone()
            if age_class is None:
                raise ApiError("予約日の年齢に対応する年齢区分がありません。", 400)

            existing = connection.execute(
                """
                SELECT reservation_id
                FROM reservations
                WHERE child_id = ? AND care_date = ?
                  AND start_hour < ? AND end_hour > ?
                  AND (status IN ('confirmed', 'waitlisted')
                       OR (status = 'held'
                           AND hold_expires_at > datetime('now', 'localtime')))
                LIMIT 1
                """,
                (child_id, care_date, end_hour, start_hour),
            ).fetchone()
            if existing is not None:
                raise ApiError(
                    "同じ時間帯に有効な予約またはキャンセル待ちがあります。",
                    409,
                )

            hours = end_hour - start_hour
            fee_yen = age_class["hourly_fee_yen"] * hours

            # Python側の書き込みロックを解除してから、Cに新規予約を任せる。
            connection.rollback()

            try:
                result = subprocess.run(
                    [
                        str(ROOT / "hoiku_cli.exe"),
                        str(Path(app.config["DATABASE"]).resolve()),
                        str(child_id),
                        care_date,
                        str(start_hour),
                        str(end_hour),
                        str(time.time_ns() // 1_000_000),
                    ],
                    capture_output=True,
                    encoding="utf-8",
                    errors="replace",
                )
            except OSError:
                app.logger.exception("hoiku_cli.exe の起動に失敗しました")
                return error_response("予約プログラムを起動できませんでした。", 500)

            if result.returncode != 0:
                app.logger.error("Cの予約処理に失敗しました: %s", result.stderr.strip())
                return error_response("予約枠を確保できませんでした。", 409)

            status, reservation_id_text = result.stdout.strip().split()
            reservation_id = int(reservation_id_text)

            reservation = connection.execute(
                """
                SELECT status, hold_expires_at, fee_yen
                FROM reservations
                WHERE reservation_id = ?
                """,
                (reservation_id,),
            ).fetchone()
            if reservation is None or reservation["status"] != status:
                app.logger.error(
                    "Cの予約結果とデータベースが一致しません: reservation_id=%s",
                    reservation_id,
                )
                return error_response("予約結果をデータベースから取得できません。", 500)

            waitlisted = status == "waitlisted"
        except ApiError as error:
            connection.rollback()
            return error_response(str(error), error.status)
        except sqlite3.IntegrityError as error:
            connection.rollback()
            return error_response(f"予約枠を確保できませんでした: {error}", 409)
        except Exception:
            connection.rollback()
            raise

        if waitlisted:
            return (
                jsonify(
                    reservation_id=reservation_id,
                    status="waitlisted",
                    course=course,
                    start_hour=start_hour,
                    end_hour=end_hour,
                    fee_yen=fee_yen,
                ),
                202,
            )

        return (
            jsonify(
                reservation_id=reservation_id,
                status="held",
                course=course,
                start_hour=start_hour,
                end_hour=end_hour,
                hold_expires_at=reservation["hold_expires_at"],
                fee_yen=reservation["fee_yen"],
            ),
            201,
        )

    @app.post("/api/holds/<int:reservation_id>/confirm")
    def confirm_hold(reservation_id: int) -> tuple[Response, int] | Response:
        guardian_id = session.get("guardian_id")
        if not isinstance(guardian_id, int):
            return error_response("ログインが必要です。", 401)
        connection = get_database()
        connection.execute("BEGIN IMMEDIATE")
        try:
            reservation = connection.execute(
                """
                SELECT r.status, r.hold_expires_at
                FROM reservations r
                JOIN children c ON c.child_id = r.child_id
                WHERE r.reservation_id = ? AND c.guardian_id = ?
                """,
                (reservation_id, guardian_id),
            ).fetchone()
            if reservation is None:
                raise ApiError("予約が見つかりません。", 404)
            if reservation["status"] != "held":
                raise ApiError("仮押さえ中の予約ではありません。", 409)
            if reservation["hold_expires_at"] <= datetime.now().strftime(
                "%Y-%m-%d %H:%M:%S"
            ):
                connection.execute(
                    "UPDATE reservations SET status = 'expired' WHERE reservation_id = ?",
                    (reservation_id,),
                )
                connection.execute(
                    "DELETE FROM reservation_hours WHERE reservation_id = ?",
                    (reservation_id,),
                )
                connection.commit()
                return error_response("仮押さえの期限が切れました。", 410)

            connection.execute(
                """
                UPDATE reservations
                SET status = 'confirmed', hold_expires_at = NULL,
                    updated_at = datetime('now', 'localtime')
                WHERE reservation_id = ?
                """,
                (reservation_id,),
            )
            connection.commit()
        except ApiError as error:
            connection.rollback()
            return error_response(str(error), error.status)
        return jsonify(reservation_id=reservation_id, status="confirmed")

    @app.delete("/api/holds/<int:reservation_id>")
    def cancel_hold(reservation_id: int) -> tuple[Response, int] | Response:
        guardian_id = session.get("guardian_id")
        if not isinstance(guardian_id, int):
            return error_response("ログインが必要です。", 401)
        connection = get_database()
        connection.execute("BEGIN IMMEDIATE")
        try:
            notification_ids = _expire_waitlist_offers(connection)
            reservation = connection.execute(
                """
                SELECT r.status FROM reservations r
                JOIN children c ON c.child_id = r.child_id
                WHERE r.reservation_id = ? AND c.guardian_id = ?
                  AND r.status IN ('held', 'confirmed', 'waitlisted')
                """,
                (reservation_id, guardian_id),
            ).fetchone()
            if reservation is None:
                raise ApiError("キャンセル可能な予約が見つかりません。", 404)

            offer_id = _cancel_reservation_and_dispatch(connection, reservation_id)
            if offer_id is not None:
                notification_ids.append(offer_id)
            connection.commit()
        except ApiError as error:
            connection.rollback()
            return error_response(str(error), error.status)
        except Exception:
            connection.rollback()
            raise
        for notification_offer_id in notification_ids:
            _send_waitlist_offer_email(
                app, app.config["DATABASE"], notification_offer_id
            )
        return jsonify(reservation_id=reservation_id, status="cancelled")

    return app


def _integer(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    return None


def _record_points(
    connection: sqlite3.Connection,
    guardian_id: int,
    delta: int,
    reason: str,
    reservation_id: int,
) -> int:
    if delta == 0:
        row = connection.execute(
            "SELECT priority_points FROM guardians WHERE guardian_id = ?",
            (guardian_id,),
        ).fetchone()
        if row is None:
            raise RuntimeError(f"Guardian not found: {guardian_id}")
        return row["priority_points"]

    cursor = connection.execute(
        "UPDATE guardians SET priority_points = priority_points + ? "
        "WHERE guardian_id = ?",
        (delta, guardian_id),
    )
    if cursor.rowcount != 1:
        raise RuntimeError(f"Guardian not found while updating points: {guardian_id}")
    balance = connection.execute(
        "SELECT priority_points FROM guardians WHERE guardian_id = ?",
        (guardian_id,),
    ).fetchone()["priority_points"]
    connection.execute(
        """
        INSERT INTO point_ledger (
            guardian_id, delta, balance_after, reason, reservation_id
        ) VALUES (?, ?, ?, ?, ?)
        """,
        (guardian_id, delta, balance, reason, reservation_id),
    )
    return balance


def _award_waitlist_points(
    connection: sqlite3.Connection, reservation_id: int
) -> None:
    setting = connection.execute(
        "SELECT value FROM settings WHERE key = 'waitlist_lose_points'"
    ).fetchone()
    if setting is None:
        raise RuntimeError("waitlist_lose_points is missing from settings")
    try:
        points = int(setting["value"])
    except (TypeError, ValueError) as error:
        raise RuntimeError("waitlist_lose_points must be an integer") from error
    if points < 0:
        raise RuntimeError("waitlist_lose_points cannot be negative")

    reservation = connection.execute(
        """
        SELECT c.guardian_id
        FROM reservations r
        JOIN children c ON c.child_id = r.child_id
        WHERE r.reservation_id = ?
        """,
        (reservation_id,),
    ).fetchone()
    if reservation is None:
        raise RuntimeError(
            f"Reservation not found while awarding waitlist points: {reservation_id}"
        )
    _record_points(
        connection,
        reservation["guardian_id"],
        points,
        "conflict_lose",
        reservation_id,
    )


def _finish_waitlist_dispatch(
    connection: sqlite3.Connection, dispatch_id: int, status: str
) -> None:
    cursor = connection.execute(
        """
        UPDATE waitlist_dispatches
        SET status = ?, resolved_at = datetime('now', 'localtime')
        WHERE dispatch_id = ? AND status = 'pending'
        """,
        (status, dispatch_id),
    )
    if cursor.rowcount != 1:
        return
    unoffered = connection.execute(
        """
        SELECT p.reservation_id
        FROM waitlist_dispatch_participants p
        JOIN reservations r ON r.reservation_id = p.reservation_id
        WHERE p.dispatch_id = ? AND p.offered = 0 AND r.status = 'waitlisted'
        """,
        (dispatch_id,),
    ).fetchall()
    for participant in unoffered:
        _award_waitlist_points(connection, participant["reservation_id"])


def _reservation_fits(connection: sqlite3.Connection, reservation: sqlite3.Row) -> bool:
    settings = {
        row["key"]: row["value"]
        for row in connection.execute(
            """
            SELECT key, value FROM settings
            WHERE key IN ('max_children', 'staff_count')
            """
        ).fetchall()
    }
    if set(settings) != {"max_children", "staff_count"}:
        raise RuntimeError("Reservation capacity settings are incomplete")
    try:
        max_children = float(settings["max_children"])
        staff_count = min(float(settings["staff_count"]), 5.0)
    except ValueError as error:
        raise RuntimeError("Reservation capacity settings must be numeric") from error
    if max_children < 0 or staff_count < 0:
        raise RuntimeError("Reservation capacity settings cannot be negative")

    for hour in range(reservation["start_hour"], reservation["end_hour"]):
        load = connection.execute(
            """
            SELECT COUNT(*) AS children_count,
                   COALESCE(SUM(r.staff_cost), 0.0) AS staff_used
            FROM reservation_hours h
            JOIN reservations r ON r.reservation_id = h.reservation_id
            WHERE h.care_date = ? AND h.hour = ?
              AND (r.status = 'confirmed'
                   OR (r.status = 'held'
                       AND r.hold_expires_at > datetime('now', 'localtime')))
            """,
            (reservation["care_date"], hour),
        ).fetchone()
        if (
            load["children_count"] >= max_children
            or load["staff_used"] + reservation["staff_cost"] > staff_count + 0.001
        ):
            return False
    return True


def _advance_waitlist_dispatch(
    connection: sqlite3.Connection, dispatch_id: int
) -> int | None:
    dispatch = connection.execute(
        """
        SELECT * FROM waitlist_dispatches
        WHERE dispatch_id = ? AND status = 'pending'
        """,
        (dispatch_id,),
    ).fetchone()
    if dispatch is None:
        return None

    connection.execute(
        """
        INSERT OR IGNORE INTO waitlist_dispatch_participants (
            dispatch_id, reservation_id
        )
        SELECT ?, r.reservation_id
        FROM reservations r
        WHERE r.status = 'waitlisted' AND r.care_date = ?
          AND r.start_hour < ? AND r.end_hour > ?
        """,
        (
            dispatch_id,
            dispatch["care_date"],
            dispatch["end_hour"],
            dispatch["start_hour"],
        ),
    )
    candidates = connection.execute(
        """
        SELECT r.reservation_id, r.care_date, r.start_hour, r.end_hour,
               r.staff_cost, r.status
        FROM waitlist_dispatch_participants p
        JOIN reservations r ON r.reservation_id = p.reservation_id
        JOIN reservation_attempts a ON a.reservation_id = r.reservation_id
        JOIN children c ON c.child_id = r.child_id
        JOIN guardians g ON g.guardian_id = c.guardian_id
        WHERE p.dispatch_id = ? AND p.offered = 0
          AND r.status = 'waitlisted'
          AND NOT EXISTS (
              SELECT 1 FROM waitlist_offers o
              WHERE o.reservation_id = r.reservation_id AND o.status = 'pending'
          )
        ORDER BY g.priority_points DESC, a.attempted_at_ms ASC,
                 r.reservation_id ASC
        """,
        (dispatch_id,),
    ).fetchall()

    response_setting = connection.execute(
        "SELECT value FROM settings WHERE key = 'offer_response_minutes'"
    ).fetchone()
    if response_setting is None:
        raise RuntimeError("offer_response_minutes is missing from settings")
    try:
        response_minutes = int(response_setting["value"])
    except (TypeError, ValueError) as error:
        raise RuntimeError("offer_response_minutes must be an integer") from error
    if response_minutes <= 0:
        raise RuntimeError("offer_response_minutes must be positive")

    for candidate in candidates:
        if not _reservation_fits(connection, candidate):
            continue
        respond_by = datetime.now() + timedelta(minutes=response_minutes)
        respond_by_text = respond_by.strftime("%Y-%m-%d %H:%M:%S")
        connection.execute("SAVEPOINT waitlist_offer_candidate")
        try:
            connection.execute(
                """
                UPDATE reservations
                SET status = 'held', hold_expires_at = ?,
                    updated_at = datetime('now', 'localtime')
                WHERE reservation_id = ? AND status = 'waitlisted'
                """,
                (respond_by_text, candidate["reservation_id"]),
            )
            connection.executemany(
                """
                INSERT INTO reservation_hours (reservation_id, care_date, hour)
                VALUES (?, ?, ?)
                """,
                (
                    (candidate["reservation_id"], candidate["care_date"], hour)
                    for hour in range(candidate["start_hour"], candidate["end_hour"])
                ),
            )
        except sqlite3.IntegrityError:
            connection.execute("ROLLBACK TO waitlist_offer_candidate")
            connection.execute("RELEASE waitlist_offer_candidate")
            continue
        connection.execute("RELEASE waitlist_offer_candidate")

        cursor = connection.execute(
            """
            INSERT INTO waitlist_offers (
                reservation_id, offered_at, respond_by, status, dispatch_id
            ) VALUES (?, datetime('now', 'localtime'), ?, 'pending', ?)
            """,
            (candidate["reservation_id"], respond_by_text, dispatch_id),
        )
        connection.execute(
            """
            UPDATE waitlist_dispatch_participants
            SET offered = 1
            WHERE dispatch_id = ? AND reservation_id = ?
            """,
            (dispatch_id, candidate["reservation_id"]),
        )
        offer_id = cursor.lastrowid
        guardian = connection.execute(
            """
            SELECT g.guardian_id, g.email, c.name AS child_name
            FROM reservations r
            JOIN children c ON c.child_id = r.child_id
            JOIN guardians g ON g.guardian_id = c.guardian_id
            WHERE r.reservation_id = ?
            """,
            (candidate["reservation_id"],),
        ).fetchone()
        body = (
            f"{guardian['child_name']}さんのキャンセル待ち予約に空き枠のオファーが届きました。"
            f"{respond_by_text}までにマイページから受諾または辞退してください。"
        )
        connection.execute(
            """
            INSERT INTO notifications (
                guardian_id, channel, type, body, related_offer_id
            ) VALUES (?, 'email', 'waitlist_offer', ?, ?)
            """,
            (guardian["guardian_id"], body, offer_id),
        )
        return offer_id

    _finish_waitlist_dispatch(connection, dispatch_id, "exhausted")
    return None


def _start_waitlist_dispatch(
    connection: sqlite3.Connection, cancelled_reservation_id: int
) -> int | None:
    cancelled = connection.execute(
        """
        SELECT care_date, start_hour, end_hour
        FROM reservations WHERE reservation_id = ?
        """,
        (cancelled_reservation_id,),
    ).fetchone()
    if cancelled is None:
        raise RuntimeError(
            f"Cancelled reservation not found: {cancelled_reservation_id}"
        )
    cursor = connection.execute(
        """
        INSERT INTO waitlist_dispatches (
            cancelled_reservation_id, care_date, start_hour, end_hour
        ) VALUES (?, ?, ?, ?)
        """,
        (
            cancelled_reservation_id,
            cancelled["care_date"],
            cancelled["start_hour"],
            cancelled["end_hour"],
        ),
    )
    dispatch_id = cursor.lastrowid
    participants = connection.execute(
        """
        SELECT reservation_id
        FROM reservations
        WHERE status = 'waitlisted' AND care_date = ?
          AND start_hour < ? AND end_hour > ?
        """,
        (
            cancelled["care_date"],
            cancelled["end_hour"],
            cancelled["start_hour"],
        ),
    ).fetchall()
    connection.executemany(
        """
        INSERT INTO waitlist_dispatch_participants (dispatch_id, reservation_id)
        VALUES (?, ?)
        """,
        ((dispatch_id, row["reservation_id"]) for row in participants),
    )
    return _advance_waitlist_dispatch(connection, dispatch_id)


def _cancel_reservation_and_dispatch(
    connection: sqlite3.Connection, reservation_id: int
) -> int | None:
    reservation = connection.execute(
        """
        SELECT care_date, start_hour, end_hour, status
        FROM reservations WHERE reservation_id = ?
        """,
        (reservation_id,),
    ).fetchone()
    if reservation is None or reservation["status"] not in {
        "held",
        "confirmed",
        "waitlisted",
    }:
        raise ApiError("キャンセル可能な予約が見つかりません。", 404)

    pending_offer = connection.execute(
        """
        SELECT offer_id, dispatch_id FROM waitlist_offers
        WHERE reservation_id = ? AND status = 'pending'
        """,
        (reservation_id,),
    ).fetchone()
    connection.execute(
        """
        UPDATE reservations
        SET status = 'cancelled', hold_expires_at = NULL,
            updated_at = datetime('now', 'localtime')
        WHERE reservation_id = ?
        """,
        (reservation_id,),
    )
    connection.execute(
        "DELETE FROM reservation_hours WHERE reservation_id = ?",
        (reservation_id,),
    )

    if pending_offer is not None:
        connection.execute(
            """
            UPDATE waitlist_offers
            SET status = 'declined', responded_at = datetime('now', 'localtime')
            WHERE offer_id = ?
            """,
            (pending_offer["offer_id"],),
        )
        if pending_offer["dispatch_id"] is not None:
            return _advance_waitlist_dispatch(
                connection, pending_offer["dispatch_id"]
            )
        return None

    if reservation["status"] == "waitlisted":
        return None
    return _start_waitlist_dispatch(connection, reservation_id)


def _expire_waitlist_offers(connection: sqlite3.Connection) -> list[int]:
    notifications: list[int] = []
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    expired = connection.execute(
        """
        SELECT offer_id, reservation_id, dispatch_id
        FROM waitlist_offers
        WHERE status = 'pending' AND respond_by <= ?
        ORDER BY respond_by, offer_id
        """,
        (now,),
    ).fetchall()
    for offer in expired:
        connection.execute(
            """
            UPDATE waitlist_offers
            SET status = 'expired', responded_at = datetime('now', 'localtime')
            WHERE offer_id = ? AND status = 'pending'
            """,
            (offer["offer_id"],),
        )
        connection.execute(
            """
            UPDATE reservations
            SET status = 'waitlisted', hold_expires_at = NULL,
                updated_at = datetime('now', 'localtime')
            WHERE reservation_id = ? AND status = 'held'
            """,
            (offer["reservation_id"],),
        )
        connection.execute(
            "DELETE FROM reservation_hours WHERE reservation_id = ?",
            (offer["reservation_id"],),
        )
        if offer["dispatch_id"] is not None:
            notification_id = _advance_waitlist_dispatch(
                connection, offer["dispatch_id"]
            )
            if notification_id is not None:
                notifications.append(notification_id)
    return notifications


def _send_waitlist_offer_email(
    app: Flask, database_path: str, offer_id: int
) -> None:
    host = os.environ.get("HOIKU_SMTP_HOST")
    sender = os.environ.get("HOIKU_SMTP_FROM")
    if not host or not sender:
        app.logger.error(
            "キャンセル待ちオファーのメールを送信できません。"
            "HOIKU_SMTP_HOST と HOIKU_SMTP_FROM を設定してください。"
        )
        return

    with closing(sqlite3.connect(database_path, timeout=10)) as connection:
        connection.row_factory = sqlite3.Row
        notification = connection.execute(
            """
            SELECT n.notification_id, n.body, g.email, o.respond_by
            FROM notifications n
            JOIN guardians g ON g.guardian_id = n.guardian_id
            JOIN waitlist_offers o ON o.offer_id = n.related_offer_id
            WHERE o.offer_id = ? AND n.type = 'waitlist_offer'
            ORDER BY n.notification_id DESC LIMIT 1
            """,
            (offer_id,),
        ).fetchone()
    if notification is None:
        raise RuntimeError(f"Email notification not found for offer {offer_id}")

    message = EmailMessage()
    message["Subject"] = "キャンセル待ちオファーのお知らせ"
    message["From"] = sender
    message["To"] = notification["email"]
    base_url = os.environ.get("HOIKU_BASE_URL", "http://127.0.0.1:5000").rstrip("/")
    message.set_content(
        f"{notification['body']}\n\n"
        f"回答期限: {notification['respond_by']}\n"
        f"マイページ: {base_url}/mypage\n"
    )

    try:
        port = int(os.environ.get("HOIKU_SMTP_PORT", "587"))
        username = os.environ.get("HOIKU_SMTP_USER")
        password = os.environ.get("HOIKU_SMTP_PASSWORD")
        use_starttls = os.environ.get("HOIKU_SMTP_STARTTLS", "1") == "1"
        with smtplib.SMTP(host, port, timeout=10) as smtp:
            if use_starttls:
                smtp.starttls(context=ssl.create_default_context())
            if username:
                smtp.login(username, password or "")
            smtp.send_message(message)
    except (OSError, smtplib.SMTPException, ValueError):
        app.logger.exception("キャンセル待ちオファーメールの送信に失敗しました")
        return
    with closing(sqlite3.connect(database_path, timeout=10)) as connection:
        connection.execute(
            """
            UPDATE notifications SET sent_at = datetime('now', 'localtime')
            WHERE notification_id = ?
            """,
            (notification["notification_id"],),
        )
        connection.commit()


def _waitlist_expiry_worker(app: Flask) -> None:
    while True:
        time.sleep(15)
        with closing(
            sqlite3.connect(
                app.config["DATABASE"], timeout=10, isolation_level=None
            )
        ) as connection:
            connection.row_factory = sqlite3.Row
            connection.execute("BEGIN IMMEDIATE")
            try:
                notification_ids = _expire_waitlist_offers(connection)
                connection.commit()
            except (sqlite3.Error, RuntimeError):
                connection.rollback()
                app.logger.exception("キャンセル待ちオファーの期限処理に失敗しました")
                continue
        for offer_id in notification_ids:
            try:
                _send_waitlist_offer_email(app, app.config["DATABASE"], offer_id)
            except (sqlite3.Error, RuntimeError):
                app.logger.exception("次のキャンセル待ちオファーを通知できませんでした")


if __name__ == "__main__":
    app = create_app()
    threading.Thread(
        target=_waitlist_expiry_worker,
        args=(app,),
        name="waitlist-expiry-worker",
        daemon=True,
    ).start()
    app.run(host="0.0.0.0", port=5000, debug=False)

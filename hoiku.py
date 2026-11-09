from __future__ import annotations

import csv
import io
import os
import secrets
import sqlite3
import tempfile
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

    @app.get("/")
    def home() -> Response:
        return redirect("/html/hoiku.html")

    @app.get("/html/<path:filename>")
    def html_page(filename: str) -> Response:
        if filename == "login.html":
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
                "status": "仮押さえ中" if row["status"] == "held" else "予約済み",
            }
            for row in rows
        ]
        return render_template(
            "user_home.html",
            name=guardian["name"],
            children=children,
            reservations=reservations,
            points=guardian["priority_points"],
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
        reservation = connection.execute(
            """
            SELECT r.status
            FROM reservations r
            JOIN children c ON c.child_id = r.child_id
            WHERE r.reservation_id = ? AND c.guardian_id = ?
            """,
            (reservation_id, guardian_id),
        ).fetchone()
        if reservation is None or reservation["status"] != "held":
            connection.rollback()
            flash("取り消せる仮押さえが見つかりません。", "error")
            return redirect("/mypage")

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
        connection.commit()
        flash("仮押さえを取り消しました。", "success")
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
            day_row = connection.execute(
                "SELECT is_open FROM operating_days WHERE care_date = ?", (care_date,)
            ).fetchone()
            if day_row is None or day_row["is_open"] != 1:
                raise ApiError("指定日は開所日ではありません。", 400)

            child = connection.execute(
                "SELECT birth_date, karte_status FROM children WHERE child_id = ?",
                (child_id,),
            ).fetchone()
            if child is None:
                raise ApiError("児童が見つかりません。", 404)
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
                SELECT reservation_id, start_hour, end_hour, status, hold_expires_at
                FROM reservations
                WHERE child_id = ? AND care_date = ?
                  AND (status = 'confirmed'
                       OR (status = 'held'
                           AND hold_expires_at > datetime('now', 'localtime')))
                ORDER BY reservation_id
                LIMIT 1
                """,
                (child_id, care_date),
            ).fetchone()
            hours = end_hour - start_hour
            fee_yen = age_class["hourly_fee_yen"] * hours
            if existing is not None:
                if existing["status"] == "confirmed":
                    raise ApiError(
                        "同日の予約はすでに確定しています。確定後のコース変更はできません。",
                        409,
                    )

                existing_course = next(
                    (
                        name
                        for name, interval in COURSES.items()
                        if interval
                        == (existing["start_hour"], existing["end_hour"])
                    ),
                    None,
                )
                if existing_course is None or existing_course == "full_day":
                    raise ApiError("同日の有効な仮押さえがあります。", 409)
                if course == existing_course:
                    raise ApiError("同じコースがすでに仮押さえされています。", 409)

                reservation_id = existing["reservation_id"]
                connection.execute(
                    "DELETE FROM reservation_hours WHERE reservation_id = ?",
                    (reservation_id,),
                )
                connection.execute(
                    """
                    UPDATE reservations
                    SET start_hour = 9, end_hour = 17, age_class_id = ?,
                        staff_cost = ?, fee_yen = ?,
                        updated_at = datetime('now', 'localtime')
                    WHERE reservation_id = ?
                    """,
                    (
                        age_class["age_class_id"],
                        age_class["staff_cost"],
                        age_class["hourly_fee_yen"] * 8,
                        reservation_id,
                    ),
                )
                connection.executemany(
                    """
                    INSERT INTO reservation_hours (reservation_id, care_date, hour)
                    VALUES (?, ?, ?)
                    """,
                    ((reservation_id, care_date, hour) for hour in range(9, 17)),
                )
                connection.commit()
                return (
                    jsonify(
                        reservation_id=reservation_id,
                        status="held",
                        course="full_day",
                        start_hour=9,
                        end_hour=17,
                        hold_expires_at=existing["hold_expires_at"],
                        fee_yen=age_class["hourly_fee_yen"] * 8,
                    ),
                    200,
                )

            timeout_row = connection.execute(
                "SELECT value FROM settings WHERE key = 'hold_timeout_sec'"
            ).fetchone()
            if timeout_row is None:
                raise RuntimeError("hold_timeout_sec is missing from settings")
            hold_expires_at = (
                datetime.now() + timedelta(seconds=int(timeout_row["value"]))
            ).strftime("%Y-%m-%d %H:%M:%S")
            cursor = connection.execute(
                """
                INSERT INTO reservations (
                    child_id, care_date, start_hour, end_hour, age_class_id,
                    staff_cost, fee_yen, status, hold_expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'held', ?)
                """,
                (
                    child_id,
                    care_date,
                    start_hour,
                    end_hour,
                    age_class["age_class_id"],
                    age_class["staff_cost"],
                    fee_yen,
                    hold_expires_at,
                ),
            )
            reservation_id = cursor.lastrowid
            connection.executemany(
                """
                INSERT INTO reservation_hours (reservation_id, care_date, hour)
                VALUES (?, ?, ?)
                """,
                (
                    (reservation_id, care_date, hour)
                    for hour in range(start_hour, end_hour)
                ),
            )
            connection.commit()
        except ApiError as error:
            connection.rollback()
            return error_response(str(error), error.status)
        except sqlite3.IntegrityError as error:
            connection.rollback()
            return error_response(f"予約枠を確保できませんでした: {error}", 409)
        except Exception:
            connection.rollback()
            raise

        return (
            jsonify(
                reservation_id=reservation_id,
                status="held",
                course=course,
                start_hour=start_hour,
                end_hour=end_hour,
                hold_expires_at=hold_expires_at,
                fee_yen=fee_yen,
            ),
            201,
        )

    @app.post("/api/holds/<int:reservation_id>/confirm")
    def confirm_hold(reservation_id: int) -> tuple[Response, int] | Response:
        connection = get_database()
        connection.execute("BEGIN IMMEDIATE")
        try:
            reservation = connection.execute(
                """
                SELECT status, hold_expires_at FROM reservations
                WHERE reservation_id = ?
                """,
                (reservation_id,),
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
        connection = get_database()
        connection.execute("BEGIN IMMEDIATE")
        reservation = connection.execute(
            "SELECT status FROM reservations WHERE reservation_id = ?",
            (reservation_id,),
        ).fetchone()
        if reservation is None:
            connection.rollback()
            return error_response("予約が見つかりません。", 404)
        if reservation["status"] != "held":
            connection.rollback()
            return error_response("仮押さえ中の予約ではありません。", 409)

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
        connection.commit()
        return jsonify(reservation_id=reservation_id, status="cancelled")

    return app


def _integer(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    return None


if __name__ == "__main__":
    app = create_app()
    app.run(host="127.0.0.1", port=5000, debug=False)

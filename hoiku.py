from __future__ import annotations

import csv
import os
import secrets
import sqlite3
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
SCHEMA_PATH = ROOT / "SQL" / "childcare_v2 (1).sql"
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

    with closing(sqlite3.connect(path)) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        existing_tables = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
        if any(name == "settings" for (name,) in existing_tables):
            pass
        elif existing_tables:
            raise RuntimeError(f"Database already contains unrelated tables: {path}")
        else:
            connection.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))


def seed_test_accounts(database_path: str | Path) -> None:
    accounts = (
        ("佐藤 敏夫", "090-1111-1111", "hogo1@example.com", "satou",
         "佐藤 太郎", "サトウ タロウ", "2020-04-15"),
        ("鈴木 一郎", "090-2222-2222", "hogo2@example.com", "suzuki",
         "鈴木 愛", "スズキ アイ", "2021-06-20"),
    )
    with closing(sqlite3.connect(database_path)) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        for (
            guardian_name,
            phone,
            email,
            password,
            child_name,
            child_name_kana,
            birth_date,
        ) in accounts:
            connection.execute(
                """
                INSERT INTO guardians (name, phone, email, password_hash)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(email) DO UPDATE SET
                    name = excluded.name,
                    phone = excluded.phone,
                    password_hash = excluded.password_hash
                """,
                (guardian_name, phone, email, generate_password_hash(password)),
            )
            guardian_id = connection.execute(
                "SELECT guardian_id FROM guardians WHERE email = ?", (email,)
            ).fetchone()[0]
            child = connection.execute(
                """
                SELECT child_id FROM children
                WHERE guardian_id = ? AND name = ?
                """,
                (guardian_id, child_name),
            ).fetchone()
            if child is None:
                connection.execute(
                    """
                    INSERT INTO children (
                        guardian_id, name, name_kana, birth_date, karte_status
                    ) VALUES (?, ?, ?, ?, 'approved')
                    """,
                    (guardian_id, child_name, child_name_kana, birth_date),
                )
            else:
                connection.execute(
                    """
                    UPDATE children
                    SET name_kana = ?, birth_date = ?, karte_status = 'approved'
                    WHERE child_id = ?
                    """,
                    (child_name_kana, birth_date, child[0]),
                )
        connection.commit()


def import_csv_data(database_path: str | Path) -> None:
    csv_directory = ROOT / "SQL"
    csv_files = {
        "guardians": csv_directory / "guardians.csv",
        "children": csv_directory / "children.csv",
        "operating_days": csv_directory / "operating_days.csv",
    }
    required_columns = {
        "guardians": {
            "guardian_id", "name", "name_kana", "phone", "email",
            "password_hash", "priority_points", "created_at",
        },
        "children": {
            "child_id", "guardian_id", "name", "name_kana", "birth_date",
            "karte_status", "karte_reviewed_at", "created_at",
        },
        "operating_days": {"care_date", "is_open"},
    }
    rows_by_table: dict[str, list[dict[str, str]]] = {}
    for table, path in csv_files.items():
        if not path.is_file():
            raise FileNotFoundError(f"Required CSV file not found: {path}")
        with path.open(encoding="utf-8-sig", newline="") as source:
            reader = csv.DictReader(source)
            if reader.fieldnames is None or not required_columns[table].issubset(
                reader.fieldnames
            ):
                raise ValueError(f"CSV has missing columns: {path}")
            rows_by_table[table] = list(reader)

    with closing(sqlite3.connect(database_path)) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.row_factory = sqlite3.Row
        connection.execute("BEGIN IMMEDIATE")
        try:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS csv_import_ids (
                    source_table TEXT NOT NULL,
                    source_id TEXT NOT NULL,
                    target_id INTEGER NOT NULL,
                    PRIMARY KEY (source_table, source_id)
                )
                """
            )
            guardian_ids: dict[str, int] = {}
            for row in rows_by_table["guardians"]:
                email = row["email"].strip().casefold()
                if not email:
                    raise ValueError("guardians.csv contains an empty email")
                connection.execute(
                    """
                    INSERT INTO guardians (
                        name, name_kana, phone, email, password_hash,
                        priority_points, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(email) DO UPDATE SET
                        name = excluded.name,
                        name_kana = excluded.name_kana,
                        phone = excluded.phone
                    """,
                    (
                        row["name"],
                        row["name_kana"] or None,
                        row["phone"],
                        email,
                        row["password_hash"] or "dummy_hash",
                        int(row["priority_points"] or 0),
                        row["created_at"],
                    ),
                )
                guardian = connection.execute(
                    "SELECT guardian_id FROM guardians WHERE email = ?", (email,)
                ).fetchone()
                guardian_ids[row["guardian_id"]] = guardian["guardian_id"]

            for row in rows_by_table["children"]:
                guardian_id = guardian_ids.get(row["guardian_id"])
                if guardian_id is None:
                    raise ValueError(
                        f"children.csv references unknown guardian_id: {row['guardian_id']}"
                    )
                imported_child = connection.execute(
                    """
                    SELECT target_id FROM csv_import_ids
                    WHERE source_table = 'children' AND source_id = ?
                    """,
                    (row["child_id"],),
                ).fetchone()
                child = None
                if imported_child is not None:
                    child = connection.execute(
                        "SELECT child_id FROM children WHERE child_id = ?",
                        (imported_child["target_id"],),
                    ).fetchone()
                if child is None:
                    child = connection.execute(
                        """
                        SELECT child_id FROM children
                        WHERE guardian_id = ? AND name = ? AND birth_date = ?
                          AND child_id NOT IN (
                              SELECT target_id FROM csv_import_ids
                              WHERE source_table = 'children'
                          )
                        ORDER BY child_id
                        LIMIT 1
                        """,
                        (guardian_id, row["name"], row["birth_date"]),
                    ).fetchone()
                values = (
                    row["karte_status"],
                    row["karte_reviewed_at"] or None,
                    row["created_at"],
                )
                if child is None:
                    connection.execute(
                        """
                        INSERT INTO children (
                            guardian_id, name, name_kana, birth_date,
                            karte_status, karte_reviewed_at, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            guardian_id,
                            row["name"],
                            row["name_kana"] or None,
                            row["birth_date"],
                            row["karte_status"],
                            row["karte_reviewed_at"] or None,
                            row["created_at"],
                        ),
                    )
                    child_id = connection.execute(
                        "SELECT last_insert_rowid()"
                    ).fetchone()[0]
                else:
                    connection.execute(
                        """
                        UPDATE children
                        SET guardian_id = ?, name = ?, name_kana = ?, birth_date = ?,
                            karte_status = ?,
                            karte_reviewed_at = ?, created_at = ?
                        WHERE child_id = ?
                        """,
                        (
                            guardian_id,
                            row["name"],
                            row["name_kana"] or None,
                            row["birth_date"],
                            *values,
                            child["child_id"],
                        ),
                    )
                    child_id = child["child_id"]
                connection.execute(
                    """
                    INSERT INTO csv_import_ids (source_table, source_id, target_id)
                    VALUES ('children', ?, ?)
                    ON CONFLICT(source_table, source_id)
                    DO UPDATE SET target_id = excluded.target_id
                    """,
                    (row["child_id"], child_id),
                )

            for row in rows_by_table["operating_days"]:
                connection.execute(
                    """
                    INSERT INTO operating_days (care_date, is_open)
                    VALUES (?, ?)
                    ON CONFLICT(care_date) DO UPDATE SET is_open = excluded.is_open
                    """,
                    (row["care_date"], int(row["is_open"])),
                )
            connection.commit()
        except Exception:
            connection.rollback()
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
    import_csv_data(app.config["DATABASE"])
    seed_test_accounts(app.config["DATABASE"])

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

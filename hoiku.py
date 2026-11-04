from __future__ import annotations

import os
import sqlite3
from contextlib import closing
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from flask import Flask, Response, g, jsonify, request


ROOT = Path(__file__).resolve().parent
SCHEMA_PATH = ROOT / "SQL" / "childcare_v2 (1).sql"


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
            return
        if existing_tables:
            raise RuntimeError(f"Database already contains unrelated tables: {path}")
        connection.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))


def create_app(database_path: str | Path | None = None) -> Flask:
    app = Flask(__name__)
    app.config["DATABASE"] = str(
        database_path or os.environ.get("HOIKU_DATABASE", ROOT / "childcare.db")
    )
    initialize_database(app.config["DATABASE"])

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

    @app.post("/api/holds")
    def create_hold() -> tuple[Response, int]:
        payload: Any = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return error_response("JSON形式のリクエストが必要です。", 400)

        child_id = _integer(payload.get("child_id"))
        start_hour = _integer(payload.get("start_hour"))
        end_hour = _integer(payload.get("end_hour"))
        care_date = payload.get("care_date")
        if child_id is None or start_hour is None or end_hour is None:
            return error_response("child_id、start_hour、end_hour は整数で指定してください。", 400)
        if not isinstance(care_date, str):
            return error_response("care_date は YYYY-MM-DD 形式で指定してください。", 400)
        try:
            booking_date = date.fromisoformat(care_date)
        except ValueError:
            return error_response("care_date は YYYY-MM-DD 形式で指定してください。", 400)
        if booking_date.isoformat() != care_date:
            return error_response("care_date は YYYY-MM-DD 形式で指定してください。", 400)
        if not (9 <= start_hour <= 15 and 11 <= end_hour <= 17):
            return error_response("保育時間は9時から17時の範囲で指定してください。", 400)
        if end_hour - start_hour < 2:
            return error_response("予約時間は2時間以上で指定してください。", 400)

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

            duplicate = connection.execute(
                """
                SELECT 1 FROM reservations
                WHERE child_id = ? AND care_date = ?
                  AND (status = 'confirmed'
                       OR (status = 'held'
                           AND hold_expires_at > datetime('now', 'localtime')))
                LIMIT 1
                """,
                (child_id, care_date),
            ).fetchone()
            if duplicate:
                raise ApiError("この児童には同日の有効な予約があります。", 409)

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

            timeout_row = connection.execute(
                "SELECT value FROM settings WHERE key = 'hold_timeout_sec'"
            ).fetchone()
            if timeout_row is None:
                raise RuntimeError("hold_timeout_sec is missing from settings")
            hold_expires_at = (
                datetime.now() + timedelta(seconds=int(timeout_row["value"]))
            ).strftime("%Y-%m-%d %H:%M:%S")
            hours = end_hour - start_hour
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
                    age_class["hourly_fee_yen"] * hours,
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
                hold_expires_at=hold_expires_at,
                fee_yen=age_class["hourly_fee_yen"] * hours,
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

from contextlib import closing
import sqlite3
import tempfile
import unittest
from pathlib import Path

from hoiku import create_app
from werkzeug.security import check_password_hash


class ReservationApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        database_path = Path(self.temp_dir.name) / "test.db"
        self.app = create_app(database_path)
        self.client = self.app.test_client()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_health_and_availability(self) -> None:
        self.assertEqual(self.client.get("/api/health").status_code, 200)

        response = self.client.get(
            "/api/availability?care_date=2026-11-10"
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json["slots"]), 8)

    def test_calendar_page_is_served_by_the_api_server(self) -> None:
        self.assertEqual(self.client.get("/").status_code, 302)
        response = self.client.get("/html/reservation.html")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"/api/availability/month", response.data)
        response.close()

    def test_monthly_availability_contains_operating_days(self) -> None:
        response = self.client.get("/api/availability/month?year=2026&month=11")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json["days"]), 30)
        days = {day["care_date"]: day for day in response.json["days"]}
        self.assertEqual(days["2026-11-01"]["status"], "closed")
        self.assertEqual(days["2026-11-10"]["status"], "open")
        self.assertEqual(days["2026-11-10"]["remaining_min"], 14)

    def test_monthly_availability_rejects_invalid_month(self) -> None:
        response = self.client.get("/api/availability/month?year=2026&month=13")

        self.assertEqual(response.status_code, 400)

    def test_csv_data_is_imported_without_duplicates(self) -> None:
        with closing(sqlite3.connect(self.app.config["DATABASE"])) as connection:
            initial_counts = (
                connection.execute("SELECT COUNT(*) FROM guardians").fetchone()[0],
                connection.execute("SELECT COUNT(*) FROM children").fetchone()[0],
                connection.execute("SELECT COUNT(*) FROM operating_days").fetchone()[0],
            )
        self.assertEqual(initial_counts, (39, 53, 30))

        create_app(self.app.config["DATABASE"])

        with closing(sqlite3.connect(self.app.config["DATABASE"])) as connection:
            repeated_counts = (
                connection.execute("SELECT COUNT(*) FROM guardians").fetchone()[0],
                connection.execute("SELECT COUNT(*) FROM children").fetchone()[0],
                connection.execute("SELECT COUNT(*) FROM operating_days").fetchone()[0],
            )
            account_children = connection.execute(
                """
                SELECT g.email, c.name
                FROM guardians g
                JOIN children c ON c.guardian_id = g.guardian_id
                WHERE g.email IN ('hogo1@example.com', 'hogo2@example.com')
                ORDER BY g.email
                """
            ).fetchall()
        self.assertEqual(repeated_counts, initial_counts)
        self.assertEqual(
            account_children,
            [
                ("hogo1@example.com", "佐藤 太郎"),
                ("hogo2@example.com", "鈴木 愛"),
            ],
        )
        with closing(sqlite3.connect(self.app.config["DATABASE"])) as connection:
            siblings = connection.execute(
                """
                SELECT c.name, c.guardian_id, g.email, g.guardian_id
                FROM children c
                JOIN guardians g ON g.guardian_id = c.guardian_id
                WHERE g.email = 'guardian03@example.com'
                ORDER BY c.name
                """
            ).fetchall()
        self.assertEqual(
            [row[0] for row in siblings], ["高橋 湊", "高橋 碧人"]
        )
        self.assertEqual(len({row[1] for row in siblings}), 1)
        self.assertEqual({row[2] for row in siblings}, {"guardian03@example.com"})
        with self.client.session_transaction() as browser_session:
            browser_session["guardian_id"] = siblings[0][3]
        mypage = self.client.get("/mypage")
        self.assertIn("高橋 湊".encode(), mypage.data)
        self.assertIn("高橋 碧人".encode(), mypage.data)
        mypage.close()

    def test_register_login_and_logout(self) -> None:
        login_page = self.client.get("/login")
        self.assertEqual(login_page.status_code, 200)
        self.assertIn(b'action="/login"', login_page.data)
        self.assertIn(b'action="/register"', login_page.data)
        login_page.close()
        self.assertEqual(
            self.client.get("/html/user_home.html").headers["Location"],
            "/login",
        )

        registered = self.client.post(
            "/register",
            data={
                "name": "テスト保護者",
                "phone": "090-1234-5678",
                "email": "TEST@example.com",
                "password": "test-password-123",
            },
        )
        self.assertEqual(registered.status_code, 302)

        login = self.client.post(
            "/login",
            data={
                "email": "test@example.com",
                "password": "test-password-123",
            },
        )
        self.assertEqual(login.status_code, 302)
        self.assertEqual(login.headers["Location"], "/html/user_home.html")

        mypage = self.client.get("/html/user_home.html")
        self.assertEqual(mypage.status_code, 200)
        self.assertIn("テスト保護者".encode(), mypage.data)
        self.assertIn(b"0 \xe3\x83\x9d\xe3\x82\xa4\xe3\x83\xb3\xe3\x83\x88", mypage.data)
        mypage.close()

        logout = self.client.get("/logout")
        self.assertEqual(logout.headers["Location"], "/login")
        self.assertEqual(self.client.get("/mypage").headers["Location"], "/login")

    def test_login_rejects_invalid_password(self) -> None:
        response = self.client.post(
            "/login",
            data={
                "email": "hanako@example.com",
                "password": "wrong-password",
            },
            follow_redirects=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn("正しくありません".encode(), response.data)
        response.close()

    def test_seeded_test_accounts_can_log_in(self) -> None:
        accounts = (
            ("hogo1@example.com", "satou", "佐藤 敏夫", "佐藤 太郎"),
            ("hogo2@example.com", "suzuki", "鈴木 一郎", "鈴木 愛"),
        )
        for email, password, guardian_name, child_name in accounts:
            with self.subTest(email=email):
                response = self.client.post(
                    "/login",
                    data={"email": email, "password": password},
                )
                self.assertEqual(
                    response.headers["Location"], "/html/user_home.html"
                )
                mypage = self.client.get("/html/user_home.html")
                self.assertIn(guardian_name.encode(), mypage.data)
                self.assertIn(child_name.encode(), mypage.data)
                other_child = "鈴木 愛" if email == "hogo1@example.com" else "佐藤 太郎"
                self.assertNotIn(other_child.encode(), mypage.data)
                mypage.close()
                self.client.get("/logout")

        with closing(sqlite3.connect(self.app.config["DATABASE"])) as connection:
            for email, password, _, child_name in accounts:
                password_hash = connection.execute(
                    "SELECT password_hash FROM guardians WHERE email = ?",
                    (email,),
                ).fetchone()[0]
                self.assertNotEqual(password_hash, password)
                self.assertTrue(check_password_hash(password_hash, password))
                child = connection.execute(
                    """
                    SELECT c.name
                    FROM children c
                    JOIN guardians g ON g.guardian_id = c.guardian_id
                    WHERE g.email = ?
                    """,
                    (email,),
                ).fetchone()
                self.assertEqual(child[0], child_name)

    def test_registration_rejects_duplicate_email(self) -> None:
        response = self.client.post(
            "/register",
            data={
                "name": "重複テスト",
                "phone": "090-1111-2222",
                "email": "HANAKO@example.com",
                "password": "test-password-123",
            },
            follow_redirects=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn("すでに登録".encode(), response.data)
        response.close()

    def test_create_and_confirm_hold(self) -> None:
        response = self.client.post(
            "/api/holds",
            json={
                "child_id": 1,
                "care_date": "2026-11-11",
                "course": "morning",
            },
        )
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json["fee_yen"], 4800)
        self.assertEqual(response.json["course"], "morning")

        reservation_id = response.json["reservation_id"]
        confirmed = self.client.post(
            f"/api/holds/{reservation_id}/confirm"
        )
        self.assertEqual(confirmed.status_code, 200)
        self.assertEqual(confirmed.json["status"], "confirmed")

    def test_afternoon_course_uses_fixed_hours(self) -> None:
        response = self.client.post(
            "/api/holds",
            json={
                "child_id": 1,
                "care_date": "2026-11-11",
                "course": "afternoon",
            },
        )
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json["fee_yen"], 4800)
        self.assertEqual(response.json["start_hour"], 13)
        self.assertEqual(response.json["end_hour"], 17)

    def test_full_day_course_uses_eight_hour_fee(self) -> None:
        response = self.client.post(
            "/api/holds",
            json={
                "child_id": 1,
                "care_date": "2026-11-11",
                "course": "full_day",
            },
        )

        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json["fee_yen"], 9600)
        self.assertEqual(response.json["start_hour"], 9)
        self.assertEqual(response.json["end_hour"], 17)

    def test_morning_and_afternoon_holds_become_one_full_day_hold(self) -> None:
        payload = {
            "child_id": 1,
            "care_date": "2026-11-11",
            "course": "morning",
        }
        morning = self.client.post("/api/holds", json=payload)
        afternoon = self.client.post(
            "/api/holds",
            json={**payload, "course": "afternoon"},
        )

        self.assertEqual(morning.status_code, 201)
        self.assertEqual(afternoon.status_code, 200)
        self.assertEqual(
            afternoon.json["reservation_id"],
            morning.json["reservation_id"],
        )
        self.assertEqual(afternoon.json["course"], "full_day")
        self.assertEqual(afternoon.json["fee_yen"], 9600)

    def test_rejects_duplicate_or_invalid_course(self) -> None:
        payload = {
            "child_id": 1,
            "care_date": "2026-11-11",
            "course": "morning",
        }
        self.assertEqual(self.client.post("/api/holds", json=payload).status_code, 201)
        self.assertEqual(self.client.post("/api/holds", json=payload).status_code, 409)
        self.assertEqual(
            self.client.post(
                "/api/holds",
                json={**payload, "course": ["morning"]},
            ).status_code,
            400,
        )

    def test_confirmed_half_day_cannot_be_extended_automatically(self) -> None:
        morning = self.client.post(
            "/api/holds",
            json={
                "child_id": 1,
                "care_date": "2026-11-11",
                "course": "morning",
            },
        )
        self.client.post(
            f"/api/holds/{morning.json['reservation_id']}/confirm"
        )
        afternoon = self.client.post(
            "/api/holds",
            json={
                "child_id": 1,
                "care_date": "2026-11-11",
                "course": "afternoon",
            },
        )

        self.assertEqual(afternoon.status_code, 409)

    def test_cancelled_hold_can_be_booked_again(self) -> None:
        payload = {
            "child_id": 1,
            "care_date": "2026-11-11",
            "course": "morning",
        }
        first = self.client.post("/api/holds", json=payload)
        cancelled = self.client.delete(
            f"/api/holds/{first.json['reservation_id']}"
        )
        second = self.client.post("/api/holds", json=payload)

        self.assertEqual(cancelled.status_code, 200)
        self.assertEqual(second.status_code, 201)


if __name__ == "__main__":
    unittest.main()

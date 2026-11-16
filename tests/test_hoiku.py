from contextlib import closing
import csv
import sqlite3
import shutil
import tempfile
import unittest
from pathlib import Path

from hoiku import create_app


class ReservationApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.base_dir = tempfile.TemporaryDirectory()
        cls.base_database = Path(cls.base_dir.name) / "base.db"
        create_app(cls.base_database)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.base_dir.cleanup()

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        database_path = Path(self.temp_dir.name) / "test.db"
        shutil.copy2(self.base_database, database_path)
        self.app = create_app(database_path)
        self.client = self.app.test_client()
        with closing(sqlite3.connect(database_path)) as connection:
            guardian_id = connection.execute(
                "SELECT guardian_id FROM children WHERE child_id = 1"
            ).fetchone()[0]
        with self.client.session_transaction() as browser_session:
            browser_session["guardian_id"] = guardian_id

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
        self.assertIn(b"/api/availability?care_date=", response.data)
        response.close()

    def test_waiting_page_allows_nearly_full_days(self) -> None:
        response = self.client.get("/html/waiting.html")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b'["full", "few"].includes(status)', response.data)
        response.close()

    def test_monthly_availability_contains_operating_days(self) -> None:
        response = self.client.get("/api/availability/month?year=2026&month=11")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json["days"]), 30)
        days = {day["care_date"]: day for day in response.json["days"]}
        self.assertEqual(days["2026-11-01"]["status"], "closed")
        self.assertEqual(days["2026-11-10"]["status"], "open")
        self.assertEqual(days["2026-11-10"]["remaining_min"], 15)

    def test_monthly_availability_rejects_invalid_month(self) -> None:
        response = self.client.get("/api/availability/month?year=2026&month=13")

        self.assertEqual(response.status_code, 400)

    def test_database_matches_csv_and_keeps_runtime_changes(self) -> None:
        with closing(sqlite3.connect(self.app.config["DATABASE"])) as connection:
            initial_counts = (
                connection.execute("SELECT COUNT(*) FROM guardians").fetchone()[0],
                connection.execute("SELECT COUNT(*) FROM children").fetchone()[0],
                connection.execute("SELECT COUNT(*) FROM operating_days").fetchone()[0],
            )
            csv_only_accounts = connection.execute(
                """
                SELECT COUNT(*) FROM guardians
                WHERE email IN ('hogo1@example.com', 'hogo2@example.com')
                """
            ).fetchone()[0]
            approved_status_count = connection.execute(
                "SELECT COUNT(*) FROM children WHERE karte_status = 'approved'"
            ).fetchone()[0]
        self.assertEqual(initial_counts, (36, 50, 30))
        self.assertEqual(csv_only_accounts, 0)
        self.assertEqual(approved_status_count, 50)

        create_app(self.app.config["DATABASE"])

        with closing(sqlite3.connect(self.app.config["DATABASE"])) as connection:
            repeated_counts = (
                connection.execute("SELECT COUNT(*) FROM guardians").fetchone()[0],
                connection.execute("SELECT COUNT(*) FROM children").fetchone()[0],
                connection.execute("SELECT COUNT(*) FROM operating_days").fetchone()[0],
            )
        self.assertEqual(repeated_counts, initial_counts)
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
        with self.client.session_transaction() as browser_session:
            browser_session.clear()
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

    def test_csv_guardian_password_is_hashed_and_login_works(self) -> None:
        csv_path = Path(__file__).resolve().parents[1] / "SQL" / "guardians.csv"
        with csv_path.open(encoding="utf-8-sig", newline="") as source:
            csv_guardian = next(
                row
                for row in csv.DictReader(source)
                if row["email"] == "guardian01@example.com"
            )

        with closing(sqlite3.connect(self.app.config["DATABASE"])) as connection:
            stored_hash = connection.execute(
                "SELECT password_hash FROM guardians WHERE email = ?",
                (csv_guardian["email"],),
            ).fetchone()[0]
        self.assertNotEqual(stored_hash, csv_guardian["password"])
        self.assertTrue(stored_hash.startswith(("scrypt:", "pbkdf2:")))

        response = self.client.post(
            "/login",
            data={
                "email": csv_guardian["email"],
                "password": csv_guardian["password"],
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], "/html/user_home.html")
        mypage = self.client.get("/html/user_home.html")
        self.assertEqual(mypage.status_code, 200)
        self.assertIn("佐藤 花子".encode(), mypage.data)
        mypage.close()
        response.close()

    def test_registration_rejects_duplicate_email(self) -> None:
        with self.client.session_transaction() as browser_session:
            browser_session.clear()
        response = self.client.post(
            "/register",
            data={
                "name": "重複テスト",
                "phone": "090-1111-2222",
                "email": "GUARDIAN01@example.com",
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
        self.assertEqual(response.json["fee_yen"], 4000)
        self.assertEqual(response.json["course"], "morning")

        reservation_id = response.json["reservation_id"]
        confirmed = self.client.post(
            f"/api/holds/{reservation_id}/confirm"
        )
        self.assertEqual(confirmed.status_code, 200)
        self.assertEqual(confirmed.json["status"], "confirmed")

    def test_two_siblings_can_be_reserved_for_the_same_course(self) -> None:
        with closing(sqlite3.connect(self.app.config["DATABASE"])) as connection:
            guardian_id, child_ids = connection.execute(
                """
                SELECT guardian_id, GROUP_CONCAT(child_id)
                FROM children
                WHERE karte_status = 'approved'
                GROUP BY guardian_id
                HAVING COUNT(*) >= 2
                ORDER BY guardian_id
                LIMIT 1
                """
            ).fetchone()
        with self.client.session_transaction() as browser_session:
            browser_session["guardian_id"] = guardian_id

        reservation_ids = []
        for child_id in map(int, child_ids.split(",")):
            response = self.client.post(
                "/api/holds",
                json={
                    "child_id": child_id,
                    "care_date": "2026-11-11",
                    "course": "morning",
                },
            )
            self.assertEqual(response.status_code, 201)
            reservation_ids.append(response.json["reservation_id"])

        for reservation_id in reservation_ids:
            confirmed = self.client.post(
                f"/api/holds/{reservation_id}/confirm"
            )
            self.assertEqual(confirmed.status_code, 200)

        with closing(sqlite3.connect(self.app.config["DATABASE"])) as connection:
            statuses = connection.execute(
                """
                SELECT status FROM reservations
                WHERE reservation_id IN (?, ?)
                ORDER BY reservation_id
                """,
                reservation_ids,
            ).fetchall()
        self.assertEqual([row[0] for row in statuses], ["confirmed", "confirmed"])

    def test_sibling_exceeding_remaining_capacity_joins_waitlist(self) -> None:
        with closing(sqlite3.connect(self.app.config["DATABASE"])) as connection:
            guardian_id, child_ids = connection.execute(
                """
                SELECT guardian_id, GROUP_CONCAT(child_id)
                FROM children
                WHERE karte_status = 'approved'
                GROUP BY guardian_id
                HAVING COUNT(*) >= 2
                ORDER BY guardian_id
                LIMIT 1
                """
            ).fetchone()
            connection.execute(
                "UPDATE settings SET value = '1' WHERE key = 'max_children'"
            )
            connection.commit()
        with self.client.session_transaction() as browser_session:
            browser_session["guardian_id"] = guardian_id

        results = []
        for child_id in map(int, child_ids.split(",")):
            results.append(
                self.client.post(
                    "/api/holds",
                    json={
                        "child_id": child_id,
                        "care_date": "2026-11-11",
                        "course": "morning",
                    },
                )
            )

        self.assertEqual([response.status_code for response in results], [201, 202])
        self.assertEqual(
            [response.json["status"] for response in results],
            ["held", "waitlisted"],
        )

    def test_cancelled_slot_offers_to_highest_points_and_rewards_unoffered(self) -> None:
        with closing(sqlite3.connect(self.app.config["DATABASE"])) as connection:
            source_guardian_id = connection.execute(
                "SELECT guardian_id FROM children WHERE child_id = 1"
            ).fetchone()[0]
            waitlisted_children = connection.execute(
                """
                SELECT child_id, guardian_id FROM children
                WHERE guardian_id != ?
                GROUP BY guardian_id
                ORDER BY child_id LIMIT 2
                """,
                (source_guardian_id,),
            ).fetchall()
            self.assertEqual(len(waitlisted_children), 2)
            high_child_id, high_guardian_id = waitlisted_children[0]
            unoffered_child_id, unoffered_guardian_id = waitlisted_children[1]
            connection.execute(
                "UPDATE settings SET value = '1' WHERE key = 'max_children'"
            )
            connection.execute(
                "UPDATE guardians SET priority_points = 3 WHERE guardian_id = ?",
                (high_guardian_id,),
            )
            connection.commit()

        source = self.client.post(
            "/api/holds",
            json={
                "child_id": 1,
                "care_date": "2026-11-11",
                "course": "morning",
            },
        )
        self.assertEqual(source.status_code, 201)

        with self.client.session_transaction() as browser_session:
            browser_session["guardian_id"] = high_guardian_id
        high_waitlist = self.client.post(
            "/api/holds",
            json={
                "child_id": high_child_id,
                "care_date": "2026-11-11",
                "course": "morning",
            },
        )
        with self.client.session_transaction() as browser_session:
            browser_session["guardian_id"] = unoffered_guardian_id
        unoffered_waitlist = self.client.post(
            "/api/holds",
            json={
                "child_id": unoffered_child_id,
                "care_date": "2026-11-11",
                "course": "morning",
            },
        )

        self.assertEqual(high_waitlist.status_code, 202)
        self.assertEqual(unoffered_waitlist.status_code, 202)
        self.assertEqual(high_waitlist.json["status"], "waitlisted")
        self.assertEqual(unoffered_waitlist.json["status"], "waitlisted")

        with self.client.session_transaction() as browser_session:
            browser_session["guardian_id"] = source_guardian_id
        cancelled = self.client.post(
            "/cancel",
            data={"reservation_id": source.json["reservation_id"]},
        )
        self.assertEqual(cancelled.status_code, 302)
        self.assertEqual(cancelled.headers["Location"], "/mypage")

        with closing(sqlite3.connect(self.app.config["DATABASE"])) as connection:
            offer = connection.execute(
                """
                SELECT offer_id, reservation_id, status
                FROM waitlist_offers
                WHERE reservation_id = ?
                """,
                (high_waitlist.json["reservation_id"],),
            ).fetchone()
            self.assertEqual(
                offer[1:],
                (high_waitlist.json["reservation_id"], "pending"),
            )
            self.assertEqual(
                connection.execute(
                    "SELECT status FROM reservations WHERE reservation_id = ?",
                    (high_waitlist.json["reservation_id"],),
                ).fetchone()[0],
                "held",
            )
            self.assertEqual(
                connection.execute(
                    "SELECT status FROM reservations WHERE reservation_id = ?",
                    (unoffered_waitlist.json["reservation_id"],),
                ).fetchone()[0],
                "waitlisted",
            )

        order = self.client.get("/api/waitlist/order")
        self.assertEqual(
            order.json["reservation_ids"],
            [unoffered_waitlist.json["reservation_id"]],
        )

        with self.client.session_transaction() as browser_session:
            browser_session["guardian_id"] = high_guardian_id
        mypage = self.client.get("/mypage")
        self.assertIn("キャンセル待ちオファー".encode(), mypage.data)
        self.assertIn(
            f"respondToOffer({offer[0]},".encode(),
            mypage.data,
        )
        accepted = self.client.post(f"/api/waitlist/offers/{offer[0]}/accept")
        self.assertEqual(accepted.status_code, 200)
        self.assertEqual(accepted.json["priority_points"], 0)

        with closing(sqlite3.connect(self.app.config["DATABASE"])) as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT priority_points FROM guardians WHERE guardian_id = ?",
                    (high_guardian_id,),
                ).fetchone()[0],
                0,
            )
            self.assertEqual(
                connection.execute(
                    "SELECT priority_points FROM guardians WHERE guardian_id = ?",
                    (unoffered_guardian_id,),
                ).fetchone()[0],
                1,
            )
            self.assertEqual(
                connection.execute(
                    """
                    SELECT delta, reason FROM point_ledger
                    WHERE guardian_id = ? ORDER BY ledger_id DESC LIMIT 1
                    """,
                    (high_guardian_id,),
                ).fetchone(),
                (-3, "waitlist_promoted"),
            )
            self.assertEqual(
                connection.execute(
                    """
                    SELECT delta, reason FROM point_ledger
                    WHERE guardian_id = ? ORDER BY ledger_id DESC LIMIT 1
                    """,
                    (unoffered_guardian_id,),
                ).fetchone(),
                (1, "conflict_lose"),
            )
        self.assertEqual(
            self.client.get("/api/waitlist/order").json["reservation_ids"],
            [unoffered_waitlist.json["reservation_id"]],
        )
        mypage.close()

    def test_declined_and_expired_offers_advance_to_next_waitlisted_child(self) -> None:
        with closing(sqlite3.connect(self.app.config["DATABASE"])) as connection:
            source_guardian_id = connection.execute(
                "SELECT guardian_id FROM children WHERE child_id = 1"
            ).fetchone()[0]
            waitlisted_children = connection.execute(
                """
                SELECT MIN(child_id), guardian_id FROM children
                WHERE guardian_id != ?
                GROUP BY guardian_id
                ORDER BY MIN(child_id) LIMIT 4
                """,
                (source_guardian_id,),
            ).fetchall()
            connection.execute(
                "UPDATE settings SET value = '1' WHERE key = 'max_children'"
            )
            connection.commit()

        source = self.client.post(
            "/api/holds",
            json={
                "child_id": 1,
                "care_date": "2026-11-11",
                "course": "morning",
            },
        )
        self.assertEqual(source.status_code, 201)

        waitlist_responses = []
        for child_id, guardian_id in waitlisted_children:
            with self.client.session_transaction() as browser_session:
                browser_session["guardian_id"] = guardian_id
            response = self.client.post(
                "/api/holds",
                json={
                    "child_id": child_id,
                    "care_date": "2026-11-11",
                    "course": "morning",
                },
            )
            self.assertEqual(response.status_code, 202)
            waitlist_responses.append((response, guardian_id))

        with self.client.session_transaction() as browser_session:
            browser_session["guardian_id"] = source_guardian_id
        cancelled = self.client.delete(f"/api/holds/{source.json['reservation_id']}")
        self.assertEqual(cancelled.status_code, 200)

        with closing(sqlite3.connect(self.app.config["DATABASE"])) as connection:
            first_offer = connection.execute(
                "SELECT offer_id FROM waitlist_offers WHERE reservation_id = ?",
                (waitlist_responses[0][0].json["reservation_id"],),
            ).fetchone()[0]
        with self.client.session_transaction() as browser_session:
            browser_session["guardian_id"] = waitlist_responses[0][1]
        declined = self.client.post(f"/api/waitlist/offers/{first_offer}/decline")
        self.assertEqual(declined.status_code, 200)

        with closing(sqlite3.connect(self.app.config["DATABASE"])) as connection:
            second_offer = connection.execute(
                "SELECT offer_id FROM waitlist_offers WHERE reservation_id = ?",
                (waitlist_responses[1][0].json["reservation_id"],),
            ).fetchone()[0]
            connection.execute(
                "UPDATE waitlist_offers SET respond_by = '2000-01-01 00:00:00' "
                "WHERE offer_id = ?",
                (second_offer,),
            )
            connection.commit()

        with self.client.session_transaction() as browser_session:
            browser_session["guardian_id"] = waitlist_responses[1][1]
        expired_response = self.client.post(
            f"/api/waitlist/offers/{second_offer}/accept"
        )
        self.assertEqual(expired_response.status_code, 410)

        with closing(sqlite3.connect(self.app.config["DATABASE"])) as connection:
            third_offer = connection.execute(
                """
                SELECT offer_id, status FROM waitlist_offers
                WHERE reservation_id = ?
                """,
                (waitlist_responses[2][0].json["reservation_id"],),
            ).fetchone()
            second_status = connection.execute(
                "SELECT status FROM waitlist_offers WHERE offer_id = ?",
                (second_offer,),
            ).fetchone()[0]
        self.assertEqual(third_offer[1], "pending")
        self.assertEqual(second_status, "expired")

        with self.client.session_transaction() as browser_session:
            browser_session["guardian_id"] = waitlist_responses[2][1]
        accepted = self.client.post(
            f"/api/waitlist/offers/{third_offer[0]}/accept"
        )
        self.assertEqual(accepted.status_code, 200)

        with closing(sqlite3.connect(self.app.config["DATABASE"])) as connection:
            unoffered_points = connection.execute(
                "SELECT priority_points FROM guardians WHERE guardian_id = ?",
                (waitlist_responses[3][1],),
            ).fetchone()[0]
            offered_points = connection.execute(
                "SELECT priority_points FROM guardians WHERE guardian_id = ?",
                (waitlist_responses[0][1],),
            ).fetchone()[0]
        self.assertEqual(unoffered_points, 1)
        self.assertEqual(offered_points, 0)

    def test_waitlist_order_uses_points_before_attempt_time(self) -> None:
        with closing(sqlite3.connect(self.app.config["DATABASE"])) as connection:
            first_child_id = 1
            first_guardian_id = connection.execute(
                "SELECT guardian_id FROM children WHERE child_id = ?",
                (first_child_id,),
            ).fetchone()[0]
            second_child_id, second_guardian_id = connection.execute(
                """
                SELECT child_id, guardian_id FROM children
                WHERE guardian_id != ?
                ORDER BY child_id LIMIT 1
                """,
                (first_guardian_id,),
            ).fetchone()
            connection.execute(
                "UPDATE settings SET value = '0' WHERE key = 'max_children'"
            )
            connection.execute(
                "UPDATE guardians SET priority_points = 5 WHERE guardian_id = ?",
                (second_guardian_id,),
            )
            connection.commit()

        first = self.client.post(
            "/api/holds",
            json={
                "child_id": first_child_id,
                "care_date": "2026-11-11",
                "course": "morning",
            },
        )
        with self.client.session_transaction() as browser_session:
            browser_session["guardian_id"] = second_guardian_id
        second = self.client.post(
            "/api/holds",
            json={
                "child_id": second_child_id,
                "care_date": "2026-11-11",
                "course": "morning",
            },
        )

        self.assertEqual(first.status_code, 202)
        self.assertEqual(second.status_code, 202)
        order = self.client.get("/api/waitlist/order")
        self.assertEqual(
            order.json["reservation_ids"],
            [second.json["reservation_id"], first.json["reservation_id"]],
        )

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
        self.assertEqual(response.json["fee_yen"], 4000)
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
        self.assertEqual(response.json["fee_yen"], 8000)
        self.assertEqual(response.json["start_hour"], 9)
        self.assertEqual(response.json["end_hour"], 17)

    def test_morning_and_afternoon_holds_are_separate_non_overlapping_reservations(self) -> None:
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
        self.assertEqual(afternoon.status_code, 201)
        self.assertNotEqual(
            afternoon.json["reservation_id"],
            morning.json["reservation_id"],
        )
        self.assertEqual(afternoon.json["course"], "afternoon")
        self.assertEqual(afternoon.json["fee_yen"], 4000)

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

    def test_confirmed_half_day_allows_non_overlapping_reservation(self) -> None:
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

        self.assertEqual(afternoon.status_code, 201)

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

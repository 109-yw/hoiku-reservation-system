import tempfile
import unittest
from pathlib import Path

from hoiku import create_app


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

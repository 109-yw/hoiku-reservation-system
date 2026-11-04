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
                "start_hour": 9,
                "end_hour": 12,
            },
        )
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json["fee_yen"], 3600)

        reservation_id = response.json["reservation_id"]
        confirmed = self.client.post(
            f"/api/holds/{reservation_id}/confirm"
        )
        self.assertEqual(confirmed.status_code, 200)
        self.assertEqual(confirmed.json["status"], "confirmed")

    def test_rejects_booking_under_two_hours(self) -> None:
        response = self.client.post(
            "/api/holds",
            json={
                "child_id": 1,
                "care_date": "2026-11-11",
                "start_hour": 9,
                "end_hour": 10,
            },
        )
        self.assertEqual(response.status_code, 400)

    def test_rejects_second_active_booking_for_same_child_and_day(self) -> None:
        payload = {
            "child_id": 1,
            "care_date": "2026-11-11",
            "start_hour": 9,
            "end_hour": 11,
        }
        self.assertEqual(self.client.post("/api/holds", json=payload).status_code, 201)
        self.assertEqual(self.client.post("/api/holds", json=payload).status_code, 409)

    def test_cancelled_hold_can_be_booked_again(self) -> None:
        payload = {
            "child_id": 1,
            "care_date": "2026-11-11",
            "start_hour": 9,
            "end_hour": 11,
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

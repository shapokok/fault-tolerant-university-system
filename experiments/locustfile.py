"""High load for the high_load scenario: 200 users (see inject.py)."""
import random
import uuid

from locust import HttpUser, between, task


class Student(HttpUser):
    wait_time = between(0.5, 1.5)

    @task(4)
    def timetable(self):
        self.client.get(f"/timetable/{random.randint(1, 50)}", name="/timetable")

    @task(2)
    def student(self):
        self.client.get(f"/students/{random.randint(1, 50)}", name="/students")

    @task(2)
    def payment(self):
        self.client.post("/payments", json={"student_id": random.randint(1, 50), "amount": 100},
                         headers={"Idempotency-Key": str(uuid.uuid4())}, name="/payments")

    @task(1)
    def transcript(self):
        self.client.get(f"/transcripts/{random.randint(1, 50)}", name="/transcripts")

"""High load: 200 users (see inject.py). With SECURITY_MODE=on each user logs in as one student."""
import os
import random
import time
import uuid

from locust import HttpUser, between, task

SECURITY = os.getenv("SECURITY_MODE", "off") == "on"
TOKENS = {}  # student id -> token, shared by all simulated users of that student (at most 50 logins)


class Student(HttpUser):
    wait_time = between(0.5, 1.5)

    def on_start(self):
        self.sid = random.randint(1, 50)
        self.headers = {}
        if not SECURITY:
            return
        if self.sid in TOKENS:
            self.headers = {"Authorization": "Bearer " + TOKENS[self.sid]}
            return
        for _ in range(120):  # the login limit (5/s per IP) answers 429 while many users start
            r = self.client.post("/auth/login", json={"username": f"student{self.sid}", "password": os.environ["STUDENT_PASSWORD"]},
                                 name="/auth/login")
            if r.status_code == 200:
                TOKENS[self.sid] = r.json()["access_token"]
                self.headers = {"Authorization": "Bearer " + TOKENS[self.sid]}
                return
            time.sleep(1 + random.random())

    @task(4)
    def timetable(self):
        self.client.get(f"/timetable/{self.sid}", headers=self.headers, name="/timetable")

    @task(2)
    def student(self):
        self.client.get(f"/students/{self.sid}", headers=self.headers, name="/students")

    @task(2)
    def payment(self):
        self.client.post("/payments", json={"student_id": self.sid, "amount": 100},
                         headers={**self.headers, "Idempotency-Key": str(uuid.uuid4())}, name="/payments")

    @task(1)
    def transcript(self):
        self.client.get(f"/transcripts/{self.sid}", headers=self.headers, name="/transcripts")

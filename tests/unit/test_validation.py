"""Input models used when SECURITY_MODE=on."""
import pytest
from pydantic import ValidationError

import payment
import security
import student


def valid(model, data):
    try:
        model(**data)
        return True
    except ValidationError:
        return False


def test_security_mode_uses_the_strict_models():
    """With SECURITY_MODE=on the endpoints use the strict models."""
    assert payment.PaymentBody is payment.StrictPaymentIn
    assert student.RegistrationBody is student.StrictRegistration


@pytest.mark.parametrize("data, ok", [
    ({"student_id": 1, "amount": 100}, True),
    ({"student_id": 50, "amount": 1}, True),
    ({"student_id": 1, "amount": 1_000_000}, True),
    ({"student_id": 1, "amount": 99.99}, True),
    ({"student_id": 1, "amount": 0}, False),
    ({"student_id": 1, "amount": -5}, False),
    ({"student_id": 1, "amount": 1_000_001}, False),
    ({"student_id": 1, "amount": 10.123}, False),
    ({"student_id": 1, "amount": "abc"}, False),
    ({"student_id": "1", "amount": 10}, False),
    ({"student_id": 0, "amount": 10}, False),
    ({"student_id": 51, "amount": 10}, False),
    ({"student_id": 2 ** 63, "amount": 10}, False),
    ({"student_id": 1}, False),
    ({"student_id": 1, "amount": 10, "status": "completed"}, False),
], ids=["ok", "id 50 amount 1", "amount max", "2 decimals", "amount 0", "negative", "amount too big", "3 decimals",
        "amount text", "id as text", "id 0", "id 51", "id 2^63", "missing amount", "extra field"])
def test_payment_model(data, ok):
    """Payment: student_id an integer 1 to 50 (no text), amount 1 to 1,000,000 with at most 2 decimals, no other fields."""
    assert valid(payment.StrictPaymentIn, data) is ok


@pytest.mark.parametrize("data, ok", [
    ({"student_id": 1, "course_id": 10}, True),
    ({"student_id": 1, "course_id": 11}, False),
    ({"student_id": 51, "course_id": 1}, False),
    ({"student_id": 1, "course_id": "2"}, False),
    ({"student_id": 1, "course_id": 2, "grade": "A"}, False),
], ids=["ok", "course 11", "student 51", "course as text", "extra field"])
def test_registration_model(data, ok):
    """Registration: student 1 to 50, course 1 to 10, integers only, no other fields."""
    assert valid(student.StrictRegistration, data) is ok


@pytest.mark.parametrize("data, ok", [
    ({"username": "student1", "password": "secret"}, True),
    ({"username": "admin' --", "password": "x"}, False),
    ({"username": "Student1", "password": "x"}, False),
    ({"username": "", "password": "x"}, False),
    ({"username": "s" * 33, "password": "x"}, False),
    ({"username": "student1", "password": ""}, False),
    ({"username": "student1", "password": "x" * 73}, False),
    ({"username": "student1", "password": "x", "role": "admin"}, False),
], ids=["ok", "SQL in username", "upper case", "empty username", "username 33", "empty password", "password 73", "extra field"])
def test_login_model(data, ok):
    """Login: username 1 to 32 of [a-z0-9_], password 1 to 72 characters (bcrypt limit), no other fields."""
    assert valid(security.LoginIn, data) is ok

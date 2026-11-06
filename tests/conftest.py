"""Shared by unit and integration tests: the docstring of every test is its acceptance
criterion. It is stored in the JUnit XML, and tests/report.py turns it into docs/test_plan.md."""


def pytest_collection_modifyitems(items):
    for item in items:
        doc = getattr(getattr(item, "obj", None), "__doc__", None) or ""
        item.user_properties.append(("criterion", " ".join(doc.split())))

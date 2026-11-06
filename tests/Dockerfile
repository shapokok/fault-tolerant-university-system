# Image for the unit tests: the service libraries (Python 3.11) plus pytest.
# Build context: ./services (only requirements.txt is used); the repo is mounted at /repo when running.
FROM python:3.11-slim
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt pytest==8.3.3

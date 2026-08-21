FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY pyproject.toml ./
COPY src ./src
RUN pip install --no-cache-dir .

# Cloud Run supplies PORT. Keep min instances at zero so the service costs
# nothing while idle.
ENV PORT=8080
EXPOSE 8080

CMD ["sh", "-c", "uvicorn fieldpilot.api.main:app --host 0.0.0.0 --port ${PORT}"]

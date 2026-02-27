# Use a lightweight Python base image
FROM python:3.11-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

RUN useradd --create-home appuser

COPY --chown=appuser:appuser requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY --chown=appuser:appuser . .
USER appuser

EXPOSE 8000

# Start Gunicorn with Uvicorn workers
# -w 4: Number of worker processes (usually 2 x cores + 1)
# -k uvicorn.workers.UvicornWorker: Tells Gunicorn to use Uvicorn's worker class
CMD ["gunicorn", "-w", "4", "-k", "uvicorn.workers.UvicornWorker", "--timeout", "300", "main:app", "--bind", "0.0.0.0:8000"]
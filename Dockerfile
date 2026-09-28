# Officiële Playwright-image: Python + Chromium + alle systeembibliotheken.
# Versie moet gelijk lopen met playwright in requirements.txt.
FROM mcr.microsoft.com/playwright/python:v1.63.0-noble

ENV PYTHONUNBUFFERED=1 TZ=Europe/Amsterdam PIP_BREAK_SYSTEM_PACKAGES=1
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]

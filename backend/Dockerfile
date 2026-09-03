FROM docker.m.daocloud.io/library/python:3.10-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir --timeout 180 --retries 5 -r requirements.txt

COPY app ./app
COPY scripts ./scripts
COPY tests/data ./tests/data

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]

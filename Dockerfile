FROM python:3.12-slim

RUN apt-get update && apt-get install -y \
    python3 \
    python3-pip \
    ghostscript \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /code
COPY requirements.txt .
RUN pip3 install --no-cache-dir -r requirements.txt
COPY . .

RUN useradd -m appuser
USER appuser

ENV PYTHONUNBUFFERED=1
CMD ["sh", "-c", "python3 manage.py migrate && python3 manage.py collectstatic --noinput && daphne -b 0.0.0.0 -p $PORT codebuddy.asgi:application & python3 manage.py runworker"]

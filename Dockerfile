FROM python:3.12-slim

RUN apt-get update && apt-get install -y ghostscript && apt-get clean && rm -rf /var/lib/apt/lists/*
WORKDIR /code
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
ENV PYTHONUNBUFFERED=1
CMD ["sh", "-c", "python manage.py migrate && python manage.py collectstatic --noinput && daphne -b 0.0.0.0 -p $PORT codebuddy.asgi:application & python manage.py runworker"]

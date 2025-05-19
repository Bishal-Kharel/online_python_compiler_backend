FROM python:3.12-slim

# Install ghostscript and clean up
RUN apt-get update && apt-get install -y ghostscript && apt-get clean && rm -rf /var/lib/apt/lists/*

WORKDIR /code
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .

# Ensure logs are unbuffered
ENV PYTHONUNBUFFERED=1

# Run migrations, Daphne, and Channels worker
CMD ["sh", "-c", "python manage.py migrate && daphne -b 0.0.0.0 -p $PORT codebuddy.asgi:application & python manage.py runworker"]

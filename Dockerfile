FROM python:3.12-slim

# Install ghostscript, Docker CLI, and dependencies
RUN apt-get update && apt-get install -y \
    ghostscript \
    docker.io \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

WORKDIR /code
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .

# Ensure logs are unbuffered
ENV PYTHONUNBUFFERED=1

# Start Docker daemon, run migrations, collect static files, and start Daphne + worker
CMD ["sh", "-c", "service docker start && python manage.py migrate && python manage.py collectstatic --noinput && daphne -b 0.0.0.0 -p $PORT codebuddy.asgi:application & python manage.py runworker"]

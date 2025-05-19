FROM python:3.12
RUN apt-get update && apt-get install -y ghostscript && apt-get clean
WORKDIR /code
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
CMD ["daphne", "-b", "0.0.0.0", "-p", "8000", "codebuddy.asgi:application"]
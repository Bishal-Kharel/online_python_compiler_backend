# CodeBuddy Backend

This is the backend service for CodeBuddy, a real-time collaborative coding platform. It is built using Django, Django Channels for WebSocket support, and Redis as the message broker. The backend is containerized using Docker and orchestrated with Docker Compose.

---

## Features

- Real-time WebSocket communication with Django Channels
- Asynchronous task handling and message brokering using Redis
- Containerized with Docker for easy setup and deployment
- Secure environment configuration with `.env` file support
- Designed to work with a React frontend (hosted separately)

---

## Technologies Used

- Python 3.12
- Django 4.x
- Django Channels
- Redis (for WebSocket message broker)
- Docker & Docker Compose
- Daphne (ASGI server)

---

## Prerequisites

Before you begin, ensure you have the following installed on your local machine:

- Docker (https://docs.docker.com/get-docker/)
- Docker Compose (usually bundled with Docker Desktop)
- Git (for cloning the repo)

---

## Getting Started (Local Development)

Follow these steps to get the backend up and running locally:

1. **Clone the repository**

```bash
git clone https://github.com/Bishal-Kharel/online_python_compiler_backend.git
cd codebuddy-backend
```

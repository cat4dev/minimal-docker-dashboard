FROM python:3.12-slim

WORKDIR /app

# Install dependencies
RUN pip install --no-cache-dir fastapi uvicorn docker jinja2 python-multipart

COPY app.py .
COPY templates/ ./templates/
COPY static/ ./static/

# Root for docker.sock access; prefer host docker group + non-root for prod.
EXPOSE 8080
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8080"]
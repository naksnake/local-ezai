FROM python:3.11-slim

WORKDIR /app

RUN pip install --no-cache-dir \
    fastapi==0.111.0 \
    uvicorn==0.29.0 \
    httpx==0.27.0 \
    python-multipart==0.0.9 \
    pypdf==4.3.1

COPY monitor/monitor.py .

EXPOSE 8888

CMD ["python3", "monitor.py"]

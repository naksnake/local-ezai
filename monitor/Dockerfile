FROM python:3.11-slim

WORKDIR /app

RUN pip install --no-cache-dir \
    fastapi==0.111.0 \
    uvicorn==0.29.0 \
    httpx==0.27.0

COPY monitor/monitor.py .

EXPOSE 8888

CMD ["python3", "monitor.py"]

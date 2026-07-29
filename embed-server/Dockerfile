FROM python:3.11-slim

WORKDIR /app

# Install Python dependencies
RUN pip install --no-cache-dir \
    fastapi==0.111.0 \
    uvicorn==0.29.0 \
    sentence-transformers==3.0.1

# Required by nomic-bert's trust_remote_code modeling file
# (separate layer so adding it doesn't re-download the torch stack)
RUN pip install --no-cache-dir einops==0.8.0

# Copy the server script
COPY embed-server/server.py .

EXPOSE 8001

CMD ["python3", "server.py"]

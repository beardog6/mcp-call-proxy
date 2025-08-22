FROM python:3.12-slim-bookworm

# Set the working directory
WORKDIR /app

RUN apt-get update && \
    apt-get install -y curl && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir "httpx>=0.28.1" "mcp>=1.1.2" "omegaconf>=2.3.0" "pip>=24.3.1" "python-dotenv>=1.0.1" "requests" openai fastapi

# Copy the source files to the container
COPY *.py *.yaml /app/

# Set the entrypoint
ENTRYPOINT ["python", "/app/call_mcp_remote.py"]

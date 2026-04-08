FROM python:3.12-slim

WORKDIR /app

# Install deps
COPY pyproject.toml .
RUN pip install --no-cache-dir -e ".[alerts]"

# Copy source
COPY nexagent/ nexagent/

EXPOSE 7070

CMD ["uvicorn", "nexagent.server:app", "--host", "0.0.0.0", "--port", "7070"]

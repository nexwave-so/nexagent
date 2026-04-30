FROM python:3.12-slim

WORKDIR /app

# Copy source and metadata together (hatchling needs the package dir to build)
COPY pyproject.toml README.md ./
COPY nexagent/ nexagent/

# Install (non-editable — correct for container deployments)
RUN pip install --no-cache-dir ".[alerts]"

EXPOSE 7070

CMD ["uvicorn", "nexagent.server:app", "--host", "0.0.0.0", "--port", "7070"]

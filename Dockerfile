FROM python:3.12-slim

WORKDIR /app

# Install uv for fast package management
RUN pip install uv

# Copy project files
COPY pyproject.toml README.md ./
COPY src/ ./src/

# Install dependencies
RUN uv pip install --system -e .

# Create data directory for SQLite
RUN mkdir -p /data

# Environment defaults
ENV HOST=0.0.0.0
ENV PORT=8000
ENV DATABASE_URL=sqlite:///data/mcp_relay.db
ENV OTEL_SERVICE_NAME=relay-mcp

EXPOSE 8000

CMD ["python", "-m", "mcp_relay.main"]

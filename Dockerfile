FROM python:3.11-slim

WORKDIR /app

COPY pyproject.toml .
RUN pip install --no-cache-dir ".[dev]" 2>/dev/null || pip install --no-cache-dir .

COPY . .
RUN pip install --no-cache-dir -e .

EXPOSE 8000

CMD ["uvicorn", "memory_engine.main:app", "--host", "0.0.0.0", "--port", "8000"]

FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml README.md ./
COPY repopilot ./repopilot
RUN pip install --no-cache-dir --retries 5 --timeout 120 .
ENV PYTHONUNBUFFERED=1
RUN useradd --create-home --uid 10001 repopilot
USER repopilot
CMD ["uvicorn", "repopilot.api:app", "--host", "0.0.0.0", "--port", "8000"]

FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update \
    && apt-get install --no-install-recommends -y ca-certificates git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY src ./src

RUN python -m pip install .

EXPOSE 8080
# Required at runtime: GITLAB_WEBHOOK_SECRET and REVIEW_AGENT_FLOW_PROJECT_ID.
CMD ["uvicorn", "review_agent_flow.web:create_app", "--factory", "--host", "0.0.0.0", "--port", "8080"]

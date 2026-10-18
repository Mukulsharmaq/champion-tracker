# One container = the Slack app process: buttons, /pipeline and the weekly run. Each run is a child process.
# No secrets in the image: tokens come from the environment at run time, config.json is mounted, and out/ and
# feedback/ are volumes so state survives a redeploy. Operating it: docs/runbook.md.
#
#   docker build -t gtm-signal-system .
#   docker run -d --name gtm-signals --restart unless-stopped --env-file .env -e PIPELINE_MODE=live \
#     -v "$PWD/config.json:/app/config.json:ro" -v gtm-out:/app/out -v gtm-feedback:/app/feedback gtm-signal-system
#   docker exec gtm-signals python3 -m pg_system --check          # preflight with the same env
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src \
    PIPELINE_OUT=/app/out
WORKDIR /app

# Named folders only, never "COPY . .": that would pull .env, config.json, out/ and feedback/ (real names) into the
# image. No fixtures/: the demo cannot run here, so it can never overwrite the live out/replay/LATEST.
COPY pyproject.toml config.example.json ./
COPY src ./src
COPY contracts ./contracts
COPY sql ./sql
COPY slack_app ./slack_app

RUN pip install --no-cache-dir ".[live]" \
 && useradd --create-home --uid 10001 app \
 && mkdir -p /app/out /app/feedback \
 && chown -R app /app/out /app/feedback
USER app
VOLUME ["/app/out", "/app/feedback"]

# Healthy = the scheduler wrote out/heartbeat.json in the last 5 minutes and its last tick had no error.
HEALTHCHECK --interval=2m --timeout=10s --start-period=3m \
  CMD python3 -c "import json,sys,time; h=json.load(open('/app/out/heartbeat.json')); sys.exit(0 if h.get('ok') and time.time()-h['at_epoch']<300 else 1)"

CMD ["python3", "slack_app/app.py"]

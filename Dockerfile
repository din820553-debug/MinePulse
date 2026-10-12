FROM python:3.13-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
COPY minepulse /app/minepulse
RUN mkdir -p /app/data && useradd --system --uid 10001 --home /app minepulse && chown -R minepulse:minepulse /app/data
USER minepulse
CMD ["python", "-m", "minepulse"]

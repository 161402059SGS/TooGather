# TooGather application image. The same image runs the web app and the worker;
# docker-compose.yml chooses which command to start.
FROM python:3.12-slim

# Do not write .pyc files; send logs straight to the console.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Install dependencies first so Docker can cache this layer between code changes.
COPY pyproject.toml README.md LICENSE ./
COPY toogather ./toogather
RUN pip install .

# Run as an unprivileged user, never as root.
RUN useradd --create-home --uid 10001 toogather
USER toogather

EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/health').status == 200 else 1)"

CMD ["uvicorn", "toogather.web.app:app", "--host", "0.0.0.0", "--port", "8080", "--proxy-headers"]

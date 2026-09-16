FROM python:3.13-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
COPY requirements.lock ./
RUN pip install -r requirements.lock
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-deps . && useradd --uid 10001 --create-home app && mkdir /data && chown app:app /data
USER 10001:10001
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=3)"]
CMD ["kis-mcp"]

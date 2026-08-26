# Multi-stage: build deps in one layer, ship a slim runtime without a compiler.
FROM python:3.12-slim AS builder
WORKDIR /build
RUN pip install --no-cache-dir --upgrade pip
COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

FROM python:3.12-slim
# Run as a non-root user. A container process that does not need root should
# not have it -- it turns a code execution bug into a contained one.
RUN useradd --create-home --uid 1001 app
WORKDIR /app
COPY --from=builder /install /usr/local
COPY --chown=app:app . .
USER app
EXPOSE 8000
# /ready, not /health. Compose uses this to gate `depends_on: service_healthy`,
# so what it needs to know is "can this container serve a request yet", which
# includes the database being up. /health answers a different question — is the
# process alive — and would report healthy while every request 500s.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8000/ready').status==200 else 1)"
CMD ["sh", "-c", "alembic upgrade head && uvicorn app.main:app --host 0.0.0.0 --port 8000"]

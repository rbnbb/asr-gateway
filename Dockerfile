FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml ./
COPY asr_gateway ./asr_gateway
RUN pip install --no-cache-dir . && useradd --uid 10001 --create-home gateway && mkdir /data && chown gateway /data
USER 10001:10001
EXPOSE 8080
CMD ["gunicorn", "--bind", "0.0.0.0:8080", "--workers", "1", "--worker-class", "gthread", "--threads", "16", "--timeout", "0", "asr_gateway.serve:application"]

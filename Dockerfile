FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml ./
COPY asr_gateway ./asr_gateway
RUN pip install --no-cache-dir . && useradd --uid 10001 --create-home gateway && mkdir /data && chown gateway /data
USER 10001:10001
ENV PORT=8080
EXPOSE ${PORT}
CMD ["sh", "-c", "exec gunicorn --bind \"0.0.0.0:${PORT}\" --workers 1 --worker-class gthread --threads 16 --timeout 0 asr_gateway.serve:application"]

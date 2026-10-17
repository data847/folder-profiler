# Folder Profiler - hardened image. Standard library only; nothing is pip-installed.
FROM python:3.12-alpine

RUN adduser -D -u 10001 profiler
WORKDIR /app
COPY file_profiler.py ./
COPY tests ./tests

ENV PROFILER_IN_DOCKER=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

USER profiler
# /data = the folder to measure (mount it read-only)   /out = where the metrics zip is written
ENTRYPOINT ["python", "/app/file_profiler.py", "/data", "--out", "/out"]

# Folder Profiler - hardened image. Standard library only; nothing is pip-installed.
FROM python:3.12-alpine

# bsdtar (libarchive) lets the profiler LIST 7z and rar archives; zip and tar need nothing extra
RUN apk add --no-cache libarchive-tools && adduser -D -u 10001 profiler
WORKDIR /app
COPY file_profiler.py cad_links.py archives.py sysmetrics.py ui.py ./
COPY tests ./tests

ENV PROFILER_IN_DOCKER=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

USER profiler
# /data = the folder to measure (mount it read-only)   /out = where the metrics zip is written
ENTRYPOINT ["python", "/app/file_profiler.py", "/data", "--out", "/out"]

# Job Analyser — public demo container (Google Cloud Run).
# Local Windows use is unchanged: start.bat / python server.py.
FROM python:3.12-slim

RUN useradd -m -u 1000 user
USER user
ENV HOME=/home/user \
    PATH=/home/user/.local/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PUBLIC_MODE=1 \
    JOBANALYSER_DATA_DIR=/home/user/data

WORKDIR /home/user/app
COPY --chown=user requirements.txt .
RUN pip install --no-cache-dir --user -r requirements.txt gunicorn

COPY --chown=user server.py db.py paths.py job-analyser.html ./

# Cloud Run sets $PORT (8080). One worker (rate limits + caches live in memory),
# many threads (live search streams). --timeout 0: streamed searches can run a few minutes.
CMD exec gunicorn server:app --bind 0.0.0.0:${PORT:-8080} --workers 1 --threads 16 --timeout 0 --access-logfile -

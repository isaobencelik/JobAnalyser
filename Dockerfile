# Job Analyser — public demo container (Hugging Face Spaces, Docker SDK).
# Local Windows use is unchanged: start.bat / python server.py.
FROM python:3.12-slim

# Hugging Face runs Spaces as user 1000
RUN useradd -m -u 1000 user
USER user
ENV HOME=/home/user \
    PATH=/home/user/.local/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PUBLIC_MODE=1 \
    PORT=7860 \
    JOBANALYSER_DATA_DIR=/home/user/data

WORKDIR /home/user/app
COPY --chown=user requirements.txt .
RUN pip install --no-cache-dir --user -r requirements.txt gunicorn

COPY --chown=user server.py db.py paths.py job-analyser.html ./

EXPOSE 7860
# One worker (rate limits + caches live in memory), many threads (live search uses streaming).
# --timeout 0: streamed searches can legitimately run for a few minutes.
CMD ["gunicorn", "server:app", "--bind", "0.0.0.0:7860", "--workers", "1", "--threads", "16", "--timeout", "0", "--access-logfile", "-"]

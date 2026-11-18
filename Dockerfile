FROM python:3.14-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
# curl : diagnostic de la route conteneur → VPN → agent (voir README)
RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
EXPOSE 5000
# --timeout > QUERY_TIMEOUT_S de server.py : une lecture trop longue est
# interrompue proprement avant que gunicorn ne tue le worker.
# --max-requests recycle les workers, borne une éventuelle fuite mémoire.
CMD ["gunicorn", "-w", "16", "-b", "0.0.0.0:5000", \
     "--timeout", "30", "--graceful-timeout", "10", "--keep-alive", "5", \
     "--max-requests", "2000", "--max-requests-jitter", "200", \
     "--limit-request-line", "4094", "--limit-request-fields", "50", \
     "server:app"]

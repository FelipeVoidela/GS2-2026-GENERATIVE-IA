FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONPATH=/app/src
WORKDIR /app

# só o que a API precisa em produção (sem Jupyter e sem as bibliotecas de coleta)
RUN pip install --no-cache-dir "numpy>=1.26" "requests>=2.31" "fastapi>=0.110" "uvicorn[standard]>=0.29" "pydantic>=2.6"

COPY src ./src
COPY base/fontes.json base/trechos.jsonl ./base/
COPY base/indice ./base/indice
COPY avaliacao/limiar.json ./avaliacao/limiar.json
COPY dados ./dados
COPY web ./web

# processo sem privilégio de root
RUN useradd --create-home app && mkdir -p /app/logs && chown -R app:app /app
USER app

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/saude', timeout=4).status == 200 else 1)"

# --no-access-log: o log de acesso do servidor traria o IP de quem pergunta (LGPD)
CMD ["uvicorn", "boca_de_lobo_ia.api:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 TZ=Europe/Madrid
RUN pip install --no-cache-dir "cryptography==43.0.3" "websocket-client==1.8.0" "paho-mqtt==2.1.0" \
    && apt-get update && apt-get install -y --no-install-recommends tzdata ffmpeg \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --uid 1000 --create-home puerta

WORKDIR /app
COPY alertas.py bateria.py bot_puerta.py camara.py copias.py cuentas.py dispositivos.py extras.py ha.py latido.py nuki_codigos.py nuki_extra.py telegram.py waha.py panel.py panel.html ./
RUN mkdir -p /app/datos && chown -R puerta:puerta /app/datos

USER puerta
ENV PANEL_ESCUCHAR=0.0.0.0 PANEL_PUERTO=8765
EXPOSE 8765
HEALTHCHECK --interval=60s --timeout=5s CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8765/api/yo',timeout=4)" || exit 1
CMD ["python", "panel.py"]

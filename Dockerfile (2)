# Imagen slim para minimizar footprint de memoria/disco en Render free (512MB)
FROM python:3.11-slim

# ffmpeg es obligatorio: yt-dlp lo usa para mergear/cortar los segmentos
RUN apt-get update && \
    apt-get install -y --no-install-recommends ffmpeg && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Cache de capas: primero requirements, así rebuilds de código no reinstalan deps
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py .

# Directorio donde Render monta "Secret Files" (ej. cookies.txt)
RUN mkdir -p /app/secrets

# Usuario no-root por seguridad
RUN useradd -m appuser && chown -R appuser:appuser /app
USER appuser

# Render inyecta $PORT dinámicamente; con shell form se expande la env var
ENV PORT=10000
EXPOSE 10000

CMD uvicorn main:app --host 0.0.0.0 --port ${PORT} --workers 1

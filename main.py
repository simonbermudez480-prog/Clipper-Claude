"""
Microservicio FastAPI para recortar clips de YouTube con yt-dlp.
Pensado para correr en Render Free (512 MB RAM) y ser llamado desde n8n.

Endpoints:
  GET  /health          -> healthcheck simple
  POST /clip            -> recorta un segmento y devuelve el archivo mp4

Diseño para RAM baja:
  - Se descarga SOLO el segmento pedido (--download-sections), nunca el video completo.
  - Se limita la resolución máxima descargable (por defecto 720p) vía env MAX_HEIGHT.
  - Un semáforo global limita a 1 (configurable) job concurrente, para no
    disparar picos de RAM/CPU en la instancia free.
  - Cada job usa un directorio temporal exclusivo que se borra SIEMPRE
    (éxito o error) al terminar, vía BackgroundTask + try/finally.
  - El archivo se devuelve en streaming (FileResponse) y se elimina
    justo después de que la respuesta termina de enviarse.
"""

import asyncio
import base64
import logging
import os
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path
from typing import Optional

from fastapi import BackgroundTasks, FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field, field_validator

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("yt-clip-service")

app = FastAPI(title="YouTube Clip Service", version="1.0.0")

# ---------------------------------------------------------------------------
# Configuración vía variables de entorno (así no hay que tocar código para
# ajustar límites, proxy o cookies en Render).
# ---------------------------------------------------------------------------
MAX_CONCURRENT_JOBS = int(os.getenv("MAX_CONCURRENT_JOBS", "1"))
MAX_HEIGHT = os.getenv("MAX_HEIGHT", "720")          # resolución máx. a descargar
MAX_CLIP_SECONDS = int(os.getenv("MAX_CLIP_SECONDS", "600"))  # tope de duración por clip
YTDLP_TIMEOUT = int(os.getenv("YTDLP_TIMEOUT", "180"))  # segundos, corta procesos colgados
YTDLP_PROXY = os.getenv("YTDLP_PROXY")                # ej: http://user:pass@host:port
COOKIES_FILE_PATH = os.getenv("COOKIES_FILE_PATH", "/app/secrets/cookies.txt")
COOKIES_B64 = os.getenv("YTDLP_COOKIES_B64")          # alternativa: cookies.txt en base64

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

_job_semaphore = asyncio.Semaphore(MAX_CONCURRENT_JOBS)


# ---------------------------------------------------------------------------
# Modelos de entrada
# ---------------------------------------------------------------------------
class ClipRequest(BaseModel):
    url: str = Field(..., description="URL del video de YouTube")
    start_time: str = Field(..., description="Inicio, formato HH:MM:SS o segundos")
    end_time: str = Field(..., description="Fin, formato HH:MM:SS o segundos")

    @field_validator("url")
    @classmethod
    def validate_url(cls, v: str) -> str:
        if "youtube.com" not in v and "youtu.be" not in v:
            raise ValueError("La URL debe ser de youtube.com o youtu.be")
        return v


def _to_seconds(value: str) -> int:
    """Acepta 'HH:MM:SS', 'MM:SS' o segundos puros y devuelve segundos int."""
    if value.isdigit():
        return int(value)
    parts = [int(p) for p in value.split(":")]
    while len(parts) < 3:
        parts.insert(0, 0)
    h, m, s = parts
    return h * 3600 + m * 60 + s


# ---------------------------------------------------------------------------
# Cookies: se resuelven UNA vez al iniciar el proceso, no en cada request.
# ---------------------------------------------------------------------------
def _resolve_cookies_file() -> Optional[str]:
    """
    Devuelve la ruta a un cookies.txt utilizable, o None si no hay ninguna
    configurada. Prioridad:
      1. COOKIES_FILE_PATH (Render "Secret File" montado en disco)
      2. YTDLP_COOKIES_B64 (contenido del cookies.txt en base64, guardado
         como variable de entorno normal)
    """
    if os.path.isfile(COOKIES_FILE_PATH):
        log.info("Usando cookies desde secret file: %s", COOKIES_FILE_PATH)
        return COOKIES_FILE_PATH

    if COOKIES_B64:
        try:
            decoded = base64.b64decode(COOKIES_B64)
            tmp_path = Path(tempfile.gettempdir()) / "cookies_from_env.txt"
            tmp_path.write_bytes(decoded)
            log.info("Cookies decodificadas desde YTDLP_COOKIES_B64")
            return str(tmp_path)
        except Exception as exc:  # noqa: BLE001
            log.warning("No se pudo decodificar YTDLP_COOKIES_B64: %s", exc)

    log.warning("Sin cookies configuradas: algunas descargas pueden fallar o degradarse.")
    return None


COOKIES_PATH = _resolve_cookies_file()


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------
@app.get("/health")
async def health():
    return {"status": "ok", "cookies_loaded": COOKIES_PATH is not None}


@app.post("/clip")
async def clip_video(payload: ClipRequest, background_tasks: BackgroundTasks):
    start_s = _to_seconds(payload.start_time)
    end_s = _to_seconds(payload.end_time)

    if end_s <= start_s:
        raise HTTPException(400, "end_time debe ser mayor que start_time")
    if end_s - start_s > MAX_CLIP_SECONDS:
        raise HTTPException(
            400, f"El clip no puede superar {MAX_CLIP_SECONDS} segundos"
        )

    job_id = uuid.uuid4().hex[:10]
    work_dir = Path(tempfile.mkdtemp(prefix=f"clip_{job_id}_"))
    output_template = str(work_dir / f"{job_id}.%(ext)s")

    section = f"*{start_s}-{end_s}"

    cmd = [
        "yt-dlp",
        payload.url,
        "--download-sections", section,
        "--force-keyframes-at-cuts",
        "-f", f"bv*[height<={MAX_HEIGHT}]+ba/b[height<={MAX_HEIGHT}]",
        "--merge-output-format", "mp4",
        "--no-playlist",
        "--no-progress",
        "--newline",
        "--retries", "3",
        "--socket-timeout", "30",
        "--user-agent", USER_AGENT,
        "--add-header", "Accept-Language: en-US,en;q=0.9",
        "-o", output_template,
    ]

    if COOKIES_PATH:
        cmd += ["--cookies", COOKIES_PATH]
    if YTDLP_PROXY:
        cmd += ["--proxy", YTDLP_PROXY]

    async with _job_semaphore:
        try:
            await _run_yt_dlp(cmd, work_dir)
        except Exception:
            shutil.rmtree(work_dir, ignore_errors=True)
            raise

    # yt-dlp decide la extensión final tras el merge; buscamos el .mp4 resultante
    result_files = list(work_dir.glob(f"{job_id}.*"))
    if not result_files:
        shutil.rmtree(work_dir, ignore_errors=True)
        raise HTTPException(500, "yt-dlp no generó ningún archivo de salida")

    output_file = result_files[0]

    # Limpieza garantizada DESPUÉS de que la respuesta se haya enviado
    background_tasks.add_task(shutil.rmtree, work_dir, ignore_errors=True)

    return FileResponse(
        path=output_file,
        media_type="video/mp4",
        filename=f"clip_{job_id}.mp4",
        background=background_tasks,
    )


async def _run_yt_dlp(cmd: list[str], work_dir: Path) -> None:
    log.info("Ejecutando: %s", " ".join(cmd))
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=work_dir,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=YTDLP_TIMEOUT)
    except asyncio.TimeoutError as exc:
        proc.kill()
        raise HTTPException(504, "yt-dlp superó el tiempo máximo permitido") from exc

    if proc.returncode != 0:
        err_tail = stderr.decode(errors="ignore")[-1500:]
        log.error("yt-dlp falló (%s): %s", proc.returncode, err_tail)
        raise HTTPException(502, f"yt-dlp falló: {err_tail}")


@app.exception_handler(Exception)
async def unhandled_exception_handler(request, exc):  # noqa: ANN001
    log.exception("Error no controlado")
    return JSONResponse(status_code=500, content={"detail": str(exc)})

# YouTube Clip Service

Microservicio FastAPI + yt-dlp para recortar segmentos de videos de YouTube,
pensado para Render Free (512 MB RAM) y ser consumido desde un workflow de n8n.

## 1. Cómo funciona (resumen técnico)

- Solo se descarga el segmento pedido con `--download-sections`, nunca el video
  completo. Esto reduce drásticamente uso de RAM/disco y tiempo de respuesta.
- La resolución máxima está limitada (`MAX_HEIGHT`, por defecto 720p) para no
  reventar la memoria de la instancia free.
- Un semáforo (`MAX_CONCURRENT_JOBS`, por defecto 1) evita que lleguen varios
  clips a la vez y saturen los 512 MB disponibles.
- Cada request usa un directorio temporal propio que se borra siempre al
  terminar (éxito o error).
- Se envían `User-Agent` y headers de un navegador real para reducir la
  probabilidad de respuestas degradadas, y se soporta `cookies.txt` para usar
  tu sesión autenticada.

**Aviso importante:** ninguna técnica garantiza al 100% evitar bloqueos de
YouTube — sus sistemas cambian constantemente. Lo de aquí son buenas
prácticas para reducir fricción (menos tráfico, headers realistas, sesión
autenticada), no un bypass garantizado. Además, descargar contenido de
YouTube puede violar sus Términos de Servicio dependiendo del uso que le des;
la responsabilidad de cumplir esos términos (por ejemplo, usar solo tu propio
contenido o clips con licencia/permiso) es tuya.

## 2. Cómo extraer tu `cookies.txt` de forma segura

Tu cookies.txt contiene las cookies de sesión de tu cuenta de Google/YouTube.
Cualquiera que lo tenga puede actuar como si fuera tú en esa sesión, así que
trátalo como una contraseña:

1. Instala la extensión **"Get cookies.txt LOCALLY"** (Chrome/Firefox) —
   verifica que sea la versión mantenida por el propio yt-dlp o de fuente
   confiable, hay clones maliciosos.
2. Entra a `youtube.com` ya logueado con la cuenta que quieras usar.
3. Abre la extensión y exporta las cookies del sitio `youtube.com` como
   `cookies.txt` (formato Netscape).
4. **No subas ese archivo a GitHub.** El `.gitignore` de este proyecto ya lo
   excluye, pero verifica siempre con `git status` antes de hacer commit.
5. Sube el archivo directamente a Render como **Secret File** (ver paso 4
   más abajo) — nunca como variable de entorno en texto plano ni en el repo.
6. Considera usar una cuenta de Google secundaria dedicada solo a esto, no tu
   cuenta personal, y revoca/regenera la sesión si sospechas que se filtró.

Alternativa sin extensión (si corres yt-dlp localmente primero):
```bash
yt-dlp --cookies-from-browser chrome --cookies cookies.txt --skip-download "https://youtube.com"
```

## 3. Configurar el repositorio en GitHub

```bash
cd yt-clip-service
git init
git add .
git status          # <- confirma que cookies.txt NO aparece en la lista
git commit -m "Initial commit: YouTube clip microservice"
git branch -M main
git remote add origin https://github.com/TU_USUARIO/yt-clip-service.git
git push -u origin main
```

Si en algún momento haces commit de cookies.txt por error, no basta con
borrarlo después: hay que reescribir el historial (`git filter-repo` o BFG) y
además regenerar esas cookies, porque ya quedaron expuestas en el historial.

## 4. Desplegar en Render

1. En Render: **New > Web Service** y conecta tu repo de GitHub.
2. Environment: **Docker** (Render detecta el `Dockerfile` automáticamente;
   si añadiste `render.yaml`, Render lo usa para preconfigurar variables).
3. Plan: **Free**.
4. En **Environment > Secret Files**, añade:
   - Filename: `secrets/cookies.txt` (debe coincidir con `COOKIES_FILE_PATH`
     que por defecto es `/app/secrets/cookies.txt`)
   - Contents: pega el contenido de tu `cookies.txt`
5. (Opcional) En **Environment Variables** añade `YTDLP_PROXY` si vas a usar
   un proxy dedicado — muy recomendable si necesitas evitar el rango de IPs
   compartidas de Render, ya que Render no ofrece IP estática en el plan free.
6. Deploy. La primera build tarda unos minutos (instala ffmpeg + deps).
7. Probar:
   ```bash
   curl https://TU_SERVICIO.onrender.com/health
   ```

## 5. Uso desde n8n

Nodo **HTTP Request**:
- Method: `POST`
- URL: `https://TU_SERVICIO.onrender.com/clip`
- Body (JSON):
  ```json
  {
    "url": "https://www.youtube.com/watch?v=VIDEO_ID",
    "start_time": "00:01:15",
    "end_time": "00:01:45"
  }
  ```
- Response Format: `File` (para recibir el binario mp4 directamente)

Ten en cuenta que el plan free de Render "duerme" tras inactividad — la
primera llamada tras un rato puede tardar ~30-50s en despertar el servicio.

## 6. Variables de entorno disponibles

| Variable              | Default | Descripción                                          |
|------------------------|---------|-------------------------------------------------------|
| `MAX_CONCURRENT_JOBS`  | `1`     | Clips procesándose en paralelo                        |
| `MAX_HEIGHT`           | `720`   | Resolución máxima a descargar                          |
| `MAX_CLIP_SECONDS`     | `600`   | Duración máxima permitida por clip                     |
| `YTDLP_TIMEOUT`        | `180`   | Timeout en segundos por descarga                       |
| `YTDLP_PROXY`          | -       | Proxy HTTP(S) opcional, formato `http://user:pass@host:port` |
| `COOKIES_FILE_PATH`    | `/app/secrets/cookies.txt` | Ruta del Secret File en Render          |
| `YTDLP_COOKIES_B64`    | -       | Alternativa: cookies.txt codificado en base64 como env var |

## 7. Limitaciones conocidas del plan free de Render

- 512 MB RAM: por eso el límite de concurrencia y de resolución.
- CPU compartida: clips largos o en alta resolución pueden acercarse al
  `YTDLP_TIMEOUT`; ajusta el valor si lo necesitas.
- Disco efímero: correcto para este diseño, ya que no se persiste nada.
- "Sleep" tras inactividad: considera un ping periódico (ej. cron externo)
  si necesitas latencia baja constante.

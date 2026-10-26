# ClipForge

> From long videos to scroll-stopping shorts — locally, with your own AI.

ClipForge is a self-hosted (local-only) tool that turns long videos into many vertical, upload-ready
clips for TikTok, Shorts and Reels. Paste a YouTube / Google Drive URL or upload a file, transcribe it
locally with Whisper, let **your own** OpenAI-compatible model pick the most viral moments, then frame,
subtitle and render the clips with FFmpeg.

**Current milestone: Phase 7 — shot list, hard cuts & dynamic zoom.** The Studio now opens with an
**editor**: pick a clip, drag in/out points, choose a streamer layout (single frame, split screen with a
facecam strip, or a picture-in-picture facecam), add an opening hook line, and watch a **real low-res
render of the result** update beside the sliders. On top of that the clip is cut into **shots** — the
speaker changes get a hard cut with a 0.15 s look-ahead, faces are tracked with MediaPipe on CPU
(models ship in `backend/cascades`, nothing downloaded at runtime), and each shot can be re-framed,
zoomed, split, merged, locked, undone and redone from a strip of timeline blocks. The shot list is the
single source of truth: what the strip shows is what FFmpeg cuts, in **one pass**, with burned karaoke
subtitles into `exports/<project_id>/` (`h264_videotoolbox`, falling back to `libx264`). Earlier phases are unchanged: download (`yt-dlp`) → probe
(`ffprobe`) → audio extraction (`ffmpeg`) → local transcription (`faster-whisper`) → audio-energy heatmap →
highlight scoring through your own OpenAI-compatible model (streamed over SSE), saved as one JSON document
per project in `data/projects/<id>.json` (no database anywhere) and replayable from Projects.

---

## Stack

| Layer     | Choice                                                                                   |
| --------- | ---------------------------------------------------------------------------------------- |
| Frontend  | React 19, Vite, TypeScript (`strict`), React Router, Tailwind CSS 4, Zustand, TanStack Query, i18next |
| Backend   | Python 3.11+, FastAPI, Uvicorn, Pydantic v2, python-dotenv, python-multipart               |
| AI        | Official `openai` Python SDK pointed at any OpenAI-compatible base URL (no closed APIs)    |
| Media     | yt-dlp, faster-whisper (CTranslate2, no PyTorch), numpy, OpenCV, Pillow, FFmpeg            |
| Storage   | Filesystem (`data/`, `temp/`, `exports/`) + `localStorage` — **no database**               |
| Encoders  | `h264_videotoolbox` on Apple silicon, `libx264` fallback (no NVENC/AMF/QSV/CUDA)           |

## Requirements

- Node.js 20+
- Python 3.11+ (3.12 recommended)
- FFmpeg + FFprobe on `PATH` (`brew install ffmpeg`)
- FFmpeg **with libass** for burned-in subtitles. Homebrew's default build has no `ass` filter; install
  `brew install ffmpeg-full` and ClipForge will pick it up automatically (see `FFMPEG_FALLBACK_BINS`).
  The Docker image already ships a libass-enabled FFmpeg plus Liberation fonts.
- `yt-dlp` on `PATH` (`brew install yt-dlp`) — the Python package is also installed, so the CLI is only a fallback
- Optional: Docker Desktop for the containerised build

## Quick start

```bash
npm install                 # frontend dependencies
cp .env.example .env        # optional, every variable has a default
npm run dev                 # frontend :5173 + backend :8000
```

- Frontend → http://localhost:5173
- Backend  → http://localhost:8000 (docs at http://localhost:8000/docs)

`npm run dev` runs both processes through `concurrently`. The backend bootstrap
(`scripts/start-backend.js`) creates `backend/.venv` with the first Python 3.11+ it finds, installs
`backend/requirements.txt` once (re-installing only when the file changes) and then starts Uvicorn with
`--reload`. Override the interpreter with `CLIPFORGE_PYTHON=/path/to/python3.12` and skip installation
with `CLIPFORGE_SKIP_INSTALL=1`.

The first analysis downloads the Whisper model (`base` ≈ 150 MB) into `~/.cache/huggingface`, so expect a
slower first run. Transcripts are cached in `data/transcripts/<hash>.json`, so re-analysing the same
video skips transcription entirely.

### Docker

```bash
docker compose up --build   # everything on http://localhost:8000
```

Stage 1 builds the SPA on `node:20-alpine`; stage 2 is `python:3.11-slim` with a libass-enabled FFmpeg,
Liberation fonts, `libgl1` and the Python dependencies. `data/`, `temp/` and `exports/` are bind-mounted,
and the container healthcheck polls `GET /api/health`. Inside the container the renderer uses `libx264`
(there is no VideoToolbox on Linux).

Inside the container `localhost` is the container itself, so the AI provider running on your Mac is
reached through `host.docker.internal`. Set `DOCKER_AI_BASE_URL` in `.env` to point somewhere else.

## Scripts

| Script              | What it does                                          |
| ------------------- | ----------------------------------------------------- |
| `npm run dev`       | Frontend + backend together                           |
| `npm run dev-frontend` | Vite dev server only                               |
| `npm run dev-backend`  | Python bootstrap + Uvicorn only                    |
| `npm run build`     | `tsc -b` then production bundle into `dist/`           |
| `npm run typecheck` | TypeScript project references, strict mode             |
| `npm run lint`      | ESLint (flat config, TS + React hooks)                 |
| `npm run preview`   | Serve the production bundle                            |
| `npm run test:smoke`| One frontend smoke test (Node's runner + Vite's loader) |

Backend checks live next to the code they check and need no test framework:

| Command | What it checks |
| ------- | -------------- |
| `backend/.venv/bin/python -m backend.scripts.shot_edit_test` | shot-list edit math, undo/redo, framing restore (84 checks) |
| `backend/.venv/bin/python -m backend.scripts.preview_timing_test` | preview crop points are sliced in clip-relative time (16 checks) |
| `backend/.venv/bin/python -m backend.scripts.editor_http_test` | `/undo` `/redo` over HTTP, 409 when empty, history survives a new client |
| `backend/.venv/bin/python -m backend.scripts.editor_live_http_test` | the same cycle against a running server (`BASE`/project at the top of the file), e.g. the Docker image on `:8001` |
| `backend/.venv/bin/python -m backend.scripts.shots_test` | draft rules, `whole_frame` spans, cut budget |
| `backend/.venv/bin/python -m backend.scripts.ai_service_test` | AI scoring: error mapping, retry/backoff, tolerant JSON, chunking, heuristic fallback (offline, no provider needed) |
| `backend/.venv/bin/python -m backend.scripts.vad_compare --src <video> --windows 50:90,600:640` | energy vs Silero against Whisper word spans |

## API

| Method | Endpoint                              | Response                                              |
| ------ | ------------------------------------- | ----------------------------------------------------- |
| GET    | `/api/health`                         | `{"status":"ok","app":"clipforge","version":"0.1.0"}` |
| GET    | `/api/system/version`                 | `{"version":"0.1.0","name":"ClipForge","phase":"5 — face tracking + editor"}` |
| GET    | `/api/system/info`                    | Safe runtime config (encoder, whisper, paths, temp)   |
| GET    | `/api/models?base_url=&api_key=`      | Model list proxied from your OpenAI-compatible provider |
| GET    | `/api/projects`                       | `{projects: [summary…], count, total_size_bytes}`     |
| POST   | `/api/projects`                       | `201` + the full project document                     |
| GET    | `/api/projects/{id}`                  | Full project document (`404` when missing)            |
| PUT    | `/api/projects/{id}`                  | Merge `name` / `clips` / `render_preset` / `exports`  |
| DELETE | `/api/projects/{id}`                  | `204`, removes `data/projects/<id>.json`              |
| POST   | `/api/projects/{id}/duplicate`        | `201` + copy named `<name> (copy)`                    |
| GET    | `/api/projects/{id}/export`           | `application/json` download (`Content-Disposition`)   |
| POST   | `/api/projects/import`                | `201` + imported project (`<name> (imported)`)        |
| POST   | `/api/analyze`                        | `text/event-stream` — analyse a YouTube / Drive / direct URL (JSON body) |
| POST   | `/api/analyze/upload`                 | `text/event-stream` — analyse an uploaded file (multipart) |
| GET    | `/api/hardware-accel`                 | `{encoder, platform, ffmpeg, videotoolbox, libx264, subtitles, overlays, audio_filters, face_tracking}` — what this machine can do |
| POST   | `/api/render-batch`                   | `202` + `{batch_id, project_id, clip_count, progress_url}` |
| GET    | `/api/render-batch/{batch_id}`        | Batch snapshot: per-clip `status`, `percent`, `file`, `size_bytes` |
| POST   | `/api/render-batch/{batch_id}/retry`  | Re-queue failed clips — body is a bare JSON array like `[1,2]` (omit to retry all) |
| POST   | `/api/render-batch/{batch_id}/cancel` | Stop the worker after the current FFmpeg step |
| GET    | `/api/render-progress/{batch_id}`     | `text/event-stream` — live render progress |
| GET    | `/api/download-rendered/{file}?project=` | `video/mp4` download (`404` outside `exports/<project>/`) |
| GET    | `/api/download-batch-zip/{batch_id}`  | `application/zip` with every finished clip of that batch |
| GET    | `/api/exports`                        | Everything on disk under `exports/`, grouped by project |
| GET    | `/api/assets`                         | `{bgm: […], watermarks: […], limits}` — the files in `data/assets/` |
| POST   | `/api/upload-bgm`                     | `201` + stored track (multipart `file`, audio only)   |
| POST   | `/api/upload-watermark`               | `201` + stored logo (multipart `file`, png/jpg/webp)  |
| GET    | `/api/assets/{kind}/{name}`           | Raw asset (`kind` = `bgm` \| `watermarks`), `404` outside those folders |
| DELETE | `/api/assets/{kind}/{name}`           | `204`, removes the asset from disk                    |
| POST   | `/api/editor/trim/{project_id}`       | Persist a clip trim (`{clip_index, trim_start, trim_end}`) → the updated project |
| GET    | `/api/editor/presets`                 | Streamer layouts (`none`, `split_top_cam`, `pip_corner`) with their flags |
| POST   | `/api/preview`                        | Render (or reuse) a 540 px preview of one clip → `{file, url, cached, elapsed_ms, width, height, duration, faces}` |
| GET    | `/api/preview/file/{file}`            | `video/mp4` for a preview (`404` outside `temp/previews/`) |
| GET    | `/api/preview/faces?project_id=&clip_index=` | Face path for one clip: `{samples, hits, coverage, keyframes, detector, cached}` |
| POST   | `/api/preview/cache`                  | Drop every cached preview → `{removed}`               |

### Project documents

One file per project, written atomically (temp file + `os.replace`) under a process-wide lock, so an
interrupted save never leaves half-written JSON:

```
data/projects/prj_20261017_a1b2c3.json
```

| Field           | Notes                                                                 |
| --------------- | --------------------------------------------------------------------- |
| `id`            | `prj_<YYYYMMDD>_<6 hex>`, validated against `^prj_\d{8}_[a-z0-9]{4,12}$` |
| `name`          | user-visible name, ≤ 120 chars (rename / duplicate only change this)   |
| `source`        | `{type: youtube \| gdrive \| upload \| url, url, video_id, title, channel}` |
| `video`         | `{duration, width, height, thumbnail, file, size_bytes}`              |
| `summary`       | AI summary in the video's own language                                |
| `clips`         | scored clips: `start_time`, `end_time`, `hook`, `title_suggestion`, `caption`, `hashtags`, `virality_score`, `quotes` |
| `heatmap`       | `[{start_time, end_time, value}]`                                     |
| `transcript`    | `[{start, end, text}]`                                                |
| `language`      | detected transcript language                                          |
| `ai`            | `{base_url, model}` actually used (never the key)                     |
| `render_preset` | Studio settings saved back into the project                           |
| `exports`       | render history: one entry per finished clip (`file`, `title`, `clip_index`, `batch_id`, `width`, `height`, `duration`, `size_bytes`), capped at 50 |
| `created_at` / `updated_at` | ISO-8601 UTC                                             |

### Analysis stream

Both analyze endpoints stream Server-Sent Events. `event:` names, `data:` is JSON:

| Event        | Payload                                                              |
| ------------ | -------------------------------------------------------------------- |
| `progress`   | `{stage, percent, message}` — `stage` is one of `queued`, `preparing`, `downloading`, `probing`, `extracting`, `heatmap`, `transcribing`, `analyzing` |
| `meta`       | `{video_id, title, channel, duration, thumbnail, video_url, source_type}` (emitted twice: before and after probing) |
| `heatmap`    | `{points: [{start_time, end_time, value}]}` — audience-energy curve    |
| `transcript` | `{language, cached, lines: [{start, end, text, energy?}]}`             |
| `clips`      | `{summary, clips: [...], model}` — partial results                     |
| `complete`   | everything above in one payload (heatmap, transcript, clips, summary)  |
| `error`      | `{stage, message}` — friendly, actionable text                         |
| `cancelled`  | `{stage, message}` — emitted when the client disconnects or cancels    |

A keep-alive comment (`: working`) is sent every 15 s so proxies do not drop long transcriptions.
Closing the request aborts the worker at its next checkpoint.

### Render stream

`POST /api/render-batch` returns immediately with a `batch_id`; the work happens in one worker thread per
batch and `GET /api/render-progress/{batch_id}` streams it:

| Event      | Payload                                                                    |
| ---------- | -------------------------------------------------------------------------- |
| `snapshot` | the full `RenderBatchState` on connect                                     |
| `batch`    | same shape, after every status change                                      |
| `clip`     | `{index, status, percent, file, size_bytes, error}` for one clip            |
| `stage`    | `{label, percent}` while the source video is downloaded/probed (0–1)        |
| `complete` | final `RenderBatchState` (mixed `completed` / `error` clips)                |
| `error`    | `{message}` — batch-level failure (missing project, source unavailable)     |

Per clip the state walks `pending → preparing → rendering → completed | error`, and `percent` comes from
parsing FFmpeg's own `time=` progress lines against the clip duration.

### Renders & exports

- Source videos are cached once in `data/media/<key>.<ext>` at the end of an analysis, so a project can be
  re-rendered after its temp job folder is cleaned up. If the cache is gone the file is downloaded again
  on demand (`media_service.resolve`).
- Output goes to `exports/<project_id>/<project-slug>-<NN>-<clip-slug>.mp4` (max `RENDER_MAX_CLIPS` per
  batch) and each file is recorded in `project.exports` (capped at 50, keyed by filename) with
  `clip_index`, `title`, `batch_id`, `width`, `height`, `duration` and `size_bytes`.
- Subtitles are generated per clip as `.ass` (PlayRes = target size, word-level `\k` karaoke, automatic
  wrapping) and burned in with the `ass` filter; caption lines are split into 2–3 word bursts so the text
  stays large without spilling off the frame.
- The frame is either **letterboxed on black** or **filled with a blurred, zoomed copy** of the source
  (`filter_complex` scale + crop + `boxblur`), then encoded at 30 fps.
- Batch state lives in memory (one process, single user): restarting the backend forgets in-flight batches,
  but the MP4s in `exports/` and the `project.exports` entries stay, so nothing you already rendered is lost.

### Overlays & audio (Phase 4)

- Assets live on disk, never in a database: `data/assets/bgm/<name>` and `data/assets/watermarks/<name>`.
  Uploads are slugged (`My Track.WAV` → `my-track.wav`), the extension is checked against a whitelist
  (audio: mp3/m4a/aac/wav/ogg/opus/flac, logos: png/jpg/jpeg/webp) and capped at `ASSET_MAX_MB`.
  `GET /api/assets` lists both folders with size, mtime and the accepted extensions, so the Studio
  pickers never offer a file the renderer cannot open.
- Presets reference assets **by file name** (`bgmFile`, `watermarkImage`) and the name is re-validated on
  every render (`asset_service.resolve`), so a hand-edited preset cannot point outside those folders. A
  missing asset is logged and skipped instead of failing the batch — the clip still renders.
- Watermark: `drawtext` for text (bold font auto-picked from `backend/fonts`, then Arial / Helvetica /
  Liberation / DejaVu) or `overlay` for an image with alpha. `watermarkPosition` is one of
  `top_left | top_right | bottom_left | bottom_right | center`, `watermarkOpacity` 0–1 and
  `watermarkSize` in % of frame width (0 = the default 3.5%).
- Background music: the source audio is gain-staged with `volume` (`voiceBoost`, default 1.0, max 2.0),
  the track is offset with `-ss`/`atrim` (`bgmOffset`), looped with `stream_loop` when `bgmLoop`, faded
  in/out with `afade` (`BGM_FADE_SECONDS`) and mixed with `amix`, then pushed through `alimiter` so the
  sum never clips.
- `renderMode: "merged"` renders each selected clip as usual (into a temp parts folder) and concatenates
  them into `exports/<project_id>/<compilation-title-slug>.mp4`, recorded in `project.exports` with the
  compilation title. A merged batch is all-or-nothing: retrying re-renders every clip, because the concat
  list has to match the parts on disk.
- `/api/hardware-accel` reports `overlays` (`drawtext`/`overlay` present) and `audio_filters`
  (`amix`/`volume`/`afade`/`alimiter` present); the Studio panel disables those controls and says why
  when a stripped FFmpeg build lacks them.

Security headers (`X-Content-Type-Options: nosniff`, `X-Frame-Options: SAMEORIGIN`,
`X-XSS-Protection: 1; mode=block`, `Referrer-Policy`) are applied to every response, and CORS is limited
to `ALLOWED_ORIGINS`.

## Configuration

Every value is optional; see `.env.example`.

| Variable                   | Default                                       | Purpose                                  |
| -------------------------- | --------------------------------------------- | ---------------------------------------- |
| `AI_BASE_URL`              | `http://localhost:20128/v1`                   | OpenAI-compatible endpoint               |
| `DOCKER_AI_BASE_URL`       | `http://host.docker.internal:20128/v1`        | Same, for the container                  |
| `AI_API_KEY`               | *(empty)*                                     | Provider key (may be empty for local routers) |
| `AI_MODEL`                 | *(empty)*                                     | Default model                            |
| `AI_TIMEOUT_SECONDS`       | `30`                                          | Per-request timeout to the provider      |
| `AI_TEMPERATURE`           | `0.4`                                         | Sampling temperature for scoring         |
| `WHISPER_MODEL`            | `base`                                        | faster-whisper model size                |
| `WHISPER_DEVICE`           | `cpu`                                         | `cpu` on Apple silicon                   |
| `WHISPER_COMPUTE_TYPE`     | `int8`                                        | CTranslate2 compute type                 |
| `WHISPER_BEAM_SIZE`        | `5`                                           | Decoding beam width                      |
| `WHISPER_VAD`              | `true`                                        | Voice-activity filter                    |
| `WHISPER_LANGUAGE`         | *(empty = auto)*                              | Force a transcription language           |
| `YTDLP_MAX_HEIGHT`         | `1080`                                        | Max download resolution                  |
| `YTDLP_SOCKET_TIMEOUT`     | `30`                                          | yt-dlp socket timeout                    |
| `YTDLP_COOKIES_FILE`       | *(empty)*                                     | Netscape `cookies.txt` for private videos |
| `MAX_UPLOAD_MB`            | `2048`                                        | Upload size limit                        |
| `ANALYZE_CHUNK_CHARS`      | `6000`                                        | Transcript chars per AI request          |
| `ANALYZE_MAX_CLIPS`        | `12`                                          | Hard cap on returned clips               |
| `FFMPEG_ENCODER`           | `auto`                                        | `auto \| videotoolbox \| cpu \| browser_wasm` |
| `FFMPEG_FALLBACK_BINS`     | `/opt/homebrew/opt/ffmpeg-full/bin/ffmpeg,…`  | Comma-separated FFmpeg binaries probed for libass |
| `RENDER_FPS`               | `30`                                          | Output frame rate                        |
| `RENDER_CRF`               | `22`                                          | Quality for `libx264`                    |
| `RENDER_PRESET`            | `veryfast`                                    | x264 speed preset                        |
| `RENDER_VIDEO_BITRATE`     | `8M`                                          | Target bitrate for `h264_videotoolbox`   |
| `RENDER_AUDIO_BITRATE`     | `160k`                                        | AAC audio bitrate                        |
| `RENDER_MAX_CLIPS`         | `20`                                          | Hard cap on clips per batch              |
| `ASSETS_DIR`               | `./data/assets`                               | Root for BGM tracks and watermark logos  |
| `ASSET_MAX_MB`             | `64`                                          | Upload cap for one BGM/logo file         |
| `BGM_DEFAULT_VOLUME`       | `0.25`                                        | Music level used by the Studio default   |
| `BGM_FADE_SECONDS`         | `0.8`                                         | Fade in/out applied to the music track   |
| `VOICE_BOOST_MAX`          | `2.0`                                         | Ceiling for the `voiceBoost` gain        |
| `FACES_DIR`                | `./data/faces`                                | Cached face-path analyses                |
| `FACE_SAMPLE_FPS`          | `2.0`                                         | Frames per second sampled for tracking   |
| `FACE_MIN_HIT_RATIO`       | `0.2`                                         | Below this hit rate the clip is not tracked |
| `PREVIEWS_DIR`             | `./temp/previews`                             | Cached editor previews                   |
| `PREVIEW_WIDTH`            | `540`                                         | Preview width (height follows the aspect) |
| `PREVIEW_CRF`              | `30`                                          | Preview quality for `libx264`            |
| `PREVIEW_PRESET`           | `ultrafast`                                   | x264 preset for previews                 |
| `PREVIEW_MAX_SECONDS`      | `20`                                          | Longest preview rendered (rest is skipped) |
| `HOST` / `PORT`            | `127.0.0.1` / `8000`                          | Uvicorn bind address                     |
| `ALLOWED_ORIGINS`          | `http://localhost:5173,http://127.0.0.1:5173` | CORS allow-list                          |
| `ADMIN_API_KEY`            | *(empty = open)*                              | Guards destructive endpoints             |
| `DATA_DIR`                 | `./data`                                      | Projects + transcripts root              |
| `TEMP_MAX_AGE_SECONDS`     | `3600`                                        | Temp file lifetime                       |
| `TEMP_STORAGE_QUOTA_GB`    | `5.0`                                         | Temp quota                               |
| `CLEANUP_INTERVAL_SECONDS` | `1800`                                        | Cleanup worker interval                  |

The AI provider is also editable at runtime in **Settings**; that copy lives in `localStorage` and is
sent to the backend with every analysis request (the backend falls back to `AI_BASE_URL` / `AI_API_KEY`
from `.env` when a field is empty).

## Project structure

```
clipforge/
├── src/
│   ├── app/            providers.tsx, router.tsx
│   ├── components/     Layout, Button, Card, Modal, PillGroup, StatusBadge, Field, HeatmapTimeline,
│   │                   ClipCard, icons
│   ├── features/       analyze/useAnalyze.ts (SSE state machine), system/*, projects/ (REST client + TanStack hooks),
│   │                   render/ (batch + asset client, useRenderBatch SSE hook, OverlaysPanel, useAssets,
│   │                   EditorPanel, editorApi, usePreview)
│   ├── i18n/           index.ts, en.ts, id.ts (id is typed as typeof en)
│   ├── lib/            apiClient.ts (retry 502/503/504), sse.ts (fetch-based SSE), format.ts, projectIndex.ts
│   ├── pages/          AnalyzePage, ProjectsPage, ProjectDetailPage, StudioPage, SettingsPage
│   ├── store/          uiStore.ts (Zustand + persist)
│   └── types/          index.ts (API + analysis contracts)
├── backend/
│   ├── main.py         FastAPI app: CORS, security headers, routers, SPA mount
│   ├── config.py       paths, dotenv, logger, constants
│   ├── routers/        system.py, ai.py, projects.py (CRUD + duplicate/export/import), analyze.py (SSE),
│   │                   render.py (batches, SSE progress, downloads, ZIP, hardware probe),
│   │                   assets.py (BGM/watermark list, upload, download, delete)
│   ├── schemas/        common.py, analyze.py, project.py (project document contract), render.py
│   ├── services/       analyze_service.py (orchestration), download_service.py (yt-dlp/ffprobe/ffmpeg),
│   │                   whisper_service.py (faster-whisper + cache), heatmap_service.py,
│   │                   ai_service.py (OpenAI-compatible client), project_store.py (JSON-per-project store,
│   │                   atomic writes), system_service.py (temp cleanup), media_service.py (source cache in
│   │                   data/media), video_engine.py (FFmpeg probe, .ass subtitles, filter graph, encode),
│   │                   render_service.py (batch queue + SSE fan-out + ZIP + merged concat),
│   │                   asset_service.py (slugged BGM/logo storage under data/assets)
│   ├── utils/          sse.py
│   ├── fonts/          extra fonts for libass (optional)
│   └── cascades/       OpenCV face cascades (Phase 5)
├── scripts/start-backend.js
├── data/projects/  data/transcripts/  data/media/  data/assets/{bgm,watermarks}/
├── temp/{uploads,jobs}/  exports/<project_id>/
├── Dockerfile, docker-compose.yml
└── .env.example, LICENSE (MIT)
```

## Pages

- **Analyze** (`/`) — source picker (YouTube / Drive / upload with drag & drop), clip length, clip count,
  custom focus prompt, analysis range, live pipeline progress, audience-energy heatmap, transcript and
  scored clip cards (hook, quotes, title / caption / hashtag suggestions with copy buttons). When a run
  finishes you can **Save project**, which writes the whole result to `data/projects/` and links straight
  to the new project.
- **Projects** (`/projects`) — every saved project as a card (source badge, duration, thumbnail, clip
  count, size, relative time) with search, *Import JSON*, *New analysis*, and per-card *Open*, *Studio*,
  *Rename*, *Duplicate*, *Export JSON* and *Delete* (with confirmation).
- **Project detail** (`/projects/:id`) — metadata (duration, clips, language, model, created/updated),
  AI summary, replayable heatmap, full clip cards with copy buttons, transcript toggle, render history
  (title, resolution, size, age, **Download**) and the backing file path.
- **Studio** (`/studio`, `/studio/:projectId`) — aspect-aware preview (9:16 / 1:1 / 4:3 / 16:9) with the
  watermark drawn where it will land, the full render settings (background, karaoke subtitles + 8 styles,
  encoder) hydrated from `render_preset` and saved back, an **Overlays & audio** panel (text or logo
  watermark with position/opacity/size, BGM track with upload/volume/offset/loop, voice boost, and the
  separate vs merged output switch with a compilation title), the local engine badge
  (`/api/hardware-accel`), and the **render queue**: pick clips, render, watch per-clip progress over SSE,
  download each file, the whole batch as a ZIP, or the merged compilation, retry failures.
- **Settings** (`/settings`) — base URL / API key / model persisted in `localStorage`, *Test connection*
  against `GET /api/models`, runtime info, language switch, reset.

The language toggle (EN / ID) lives in the navbar, defaults to `navigator.language`, and persists in
`localStorage` under `clipforge_lang`.

## Decisions taken where the brief was open

- Python **3.12** is used for the dev virtualenv (first 3.11+ found); the Docker image pins 3.11.
- `scripts/start-backend.js` owns the virtualenv so `npm run dev` is a single command.
- Model listing is proxied through the backend (`/api/models`) instead of calling the provider from the
  browser, so keys are never exposed to CORS preflights and the browser never talks to the router.
- The API key is stored in `localStorage` (as requested) and only ever sent to the local backend. This is
  a local single-user tool, so that is an accepted trade-off — do not expose the port to the internet.
- Analysis uses **SSE over POST** (JSON or multipart), so the browser needs `fetch`-based streaming
  (`src/lib/sse.ts`) rather than `EventSource`, which cannot POST.
- Per-job scratch space lives in `temp/jobs/<job_id>/` (uploads in `temp/uploads/`), and transcripts are
  cached in `data/transcripts/<sha1>.json` so a second analysis of the same audio is instant.
- The heatmap is derived from **audio energy** (RMS per window via `wave` + numpy) instead of a second
  model, so it costs nothing and never leaves the machine.
- Google Drive links are fetched directly from `drive.usercontent.google.com/download` (the endpoint
  Drive itself uses), because the legacy `uc?export=download` URL now returns a virus-scan page. Only
  single files shared as *anyone with the link* work — folders and private files are rejected with a
  readable message.
- `faster-whisper` is pinned to `av<19`: PyAV 19 breaks faster-whisper 1.2.x at decode time.
- **Project history needs no database**: one JSON document per project in `data/projects/`, written
  atomically (temp file + `os.replace`) under a process-wide lock. Listing reads the directory, so the
  filesystem is the source of truth and a project can be copied or backed up with `cp`.
- Project ids are generated server-side (`prj_<date>_<hex>`) and validated against a strict regex before
  any path is built, which rules out path traversal through `/api/projects/{id}`.
- `localStorage` (`clipforge_projects`, max 120 entries) only caches the project list so the Projects page
  paints instantly on reload; every successful `GET /api/projects` overwrites it.
- Duplicate and import never reuse the original id — the copy gets a fresh `id`, `created_at` and a
  `(copy)` / `(imported)` suffix, so exports can be re-imported safely.
- Zustand holds UI/provider preferences; TanStack Query owns all server state.
- Rendering runs in **one worker thread per batch** (no Celery/Redis), following the same pattern as the
  analyze worker: a single-user local tool does not need a task queue, and FFmpeg already saturates the
  CPU. Progress reaches the browser through an SSE fan-out (`_Subscriber` list per batch).
- Subtitles are written as `.ass` and burned with libass instead of `drawtext`: karaoke needs per-word
  timing and per-word colour, which `.ass` gives for free (`{\k}`), and `WrapStyle: 0` handles wrapping.
- The FFmpeg binary is chosen by probing `-filters` for `ass`: `FFMPEG_BIN`, then `FFMPEG_FALLBACK_BINS`,
  then `PATH`. Capability probes are cached per process, so the first render pays ~200 ms and the rest are
  free. Without libass the renderer still produces clips, just without subtitles, and
  `/api/hardware-accel` says so up front.
- `h264_videotoolbox` is tried first on Apple silicon and `libx264` is the fallback (and the only option in
  Docker); NVENC/AMF/QSV/CUDA are deliberately not offered.
- Export filenames are slugged (`<project>-<NN>-<clip>`) rather than id-based so the ZIP is readable, and
  downloads are resolved through `safe_export_path()` which rejects anything outside `exports/<project>/`.
- `render_preset` is camelCase end to end (frontend `RenderSettings`, backend `RenderSettingsModel`), so no
  translation layer can drift; unknown subtitle styles fall back to `viral_pop` instead of failing.
- BGM tracks and watermark logos are plain files in `data/assets/`, referenced from the preset **by name**
  rather than by id or path: the preset stays readable, a copied project keeps working as long as the
  file is there, and `asset_service` re-validates the name on every use so a preset can never escape
  those two folders.
- Watermarks are drawn with `drawtext` (text) or `overlay` (image) inside the same `filter_complex` as the
  crop and subtitles — one FFmpeg pass, no intermediate file, and the overlay is scaled from a percentage
  of the frame width so it looks identical at 1080×1920 and 1080×1080.
- BGM is mixed with `amix` + per-input `volume` + `afade` and finished with `alimiter` instead of a manual
  `amerge`/`pan` graph: it keeps the voice intelligible at any music level and cannot clip the sum.
- Merged compilations render each clip separately and then `concat` the parts (same encoder, same
  resolution), instead of one giant filter graph: a failure stays isolated to one clip, and the parts can
  be inspected if the join goes wrong. The trade-off is that a merged retry re-renders everything.
- Asset uploads reuse the analyze upload limit machinery (`ASSET_MAX_MB`) and slug the filename, so
  `My Track (final).WAV` becomes a predictable `my-track-final.wav` in the picker and on disk.

### Face tracking, streamer layouts & the editor (Phase 5)

- **Face detection is local and deterministic.** `face_service` samples the clip window at
  `FACE_SAMPLE_FPS` (2 fps) through OpenCV, keeps the **largest** face per frame, rejects runs shorter
  than `FACE_MIN_HIT_RATIO` of the samples, smooths the horizontal path and compresses it to ≤10
  normalised keyframes. Results are cached in `data/faces/` keyed by source file + mtime + window, so
  dragging a slider re-uses the analysis instead of re-scanning the video. *(Phase 6 replaced the
  sampling/smoothing internals with the `tracking/` pipeline below; the cache contract is unchanged.)*
- The detector is `cv2.FaceDetectorYN` with a YuNet ONNX model committed to `backend/cascades`. The
  brief suggested Haar cascades, but this `opencv-python` wheel ships **no** `cv2.data.haarcascades`
  XML payload, so Haar is only a fallback (`backend/cascades/*.xml`) and YuNet is the primary path —
  it is also more stable on faces at an angle and needs no model download at runtime.
- Tracking is a **time-dependent FFmpeg `crop`**: the path becomes a single-quoted x-expression
  (`_piecewise_expr`) — an unquoted expression makes FFmpeg misparse the crop. The crop window keeps
  the source aspect ratio (`_crop_box`), so no stretching, and the path is clamped to the frame so the
  box never leaves it.
- **Streamer layouts** reuse the same chains: `split_top_cam` renders the wide shot through
  `_main_chain(out="main")` in the bottom area and a head-and-shoulders crop (`_facecam_crop`, ≈2.4×
  face height, nudged down so shoulders are included) in the top strip; `pip_corner` overlays that
  facecam box at `facecamPosition` with a white border and a margin of `max(12px, 3.5% of width)`.
- **Hooks** are `drawtext` with `textfile=` (written to the job's work dir as `hook-<hex>.txt`, so no
  escaping bugs from quotes/emoji), `hookPrefix + hookText + hookSuffix`, optional uppercase,
  `top | center | bottom`, on screen for `hookDuration` seconds from the clip start, sized
  `hookSize` % of the frame width (3–16) and clamped to 80 characters.
- **The editor is thin on the server.** The browser owns the working copy of clips + settings; the
  server only persists the trim, renders previews, serves them and exposes the face path. That keeps
  the "no database" rule intact — the trim lives on `ViralClip.trim_start` / `trim_end` inside the
  project JSON, next to the AI cut.
- **Preview == export by construction.** Both paths resolve the clip window through one helper
  (`edit_service.clip_window()`: trim wins over the AI cut, minimum `0.4 s`) and pass the same Phase 5
  arguments into the same `render_clip`, so the preview cannot drift from the final file. The preview
  only differs in cost: 540 px wide, `ultrafast`/CRF 30, capped at `PREVIEW_MAX_SECONDS` (20 s).
- Previews are cached in `temp/previews/` by a hash of (clip window + settings + source mtime), and the
  hash version is part of the key (`"v": 4`) so a filtergraph change invalidates old files instead of
  serving stale frames. The frontend debounces slider moves (700 ms) and only re-renders when the
  request key actually changes.
- Each preview writes a tiny `*.faces.json` sidecar next to the MP4, so a cache hit can still show the
  "yunet · 94% of frames" badge without re-running detection. Hook text is written to a temp `textfile`
  and deleted in a `finally` block, so `exports/` and `temp/previews/` only ever hold finished media.
- The editor's `<video>` plays the rendered file straight from `/api/preview/file/...`, so what you see
  is the encoded result — subtitles, watermark, BGM, hook and facecam all included — not a CSS mockup.

### Smoother tracking & clip types (Phase 6)

- **The pipeline is split per stage** in `backend/services/tracking/`, so each stage can be replaced or
  unit-tested alone: `sampler` (OpenCV `VideoCapture`, real timestamps from `CAP_PROP_POS_MSEC` — no
  assumed constant fps), `detector` (MediaPipe Tasks: BlazeFace short-range + FaceLandmarker with the
  `jawOpen` blendshape; YuNet/Haar stay as fallbacks), `tracker` (IoU + centroid identity, so two
  people do not swap mid-clip), `speaker` (mouth-open ratio with hysteresis), `smoother` (One-Euro →
  dead zone → Savitzky-Golay → speed clamp), `trajectory` (crop window + smoothness metrics) and
  `pipeline` (orchestration + cache). `face_service` is now a 60-line facade, so render/preview/editor
  call sites did not change.
- **Detection runs every `detect_interval`-th sampled frame** (`detect_fps` 10, interval 1 → ~10 Hz),
  and the gaps are filled by the tracker, then smoothed — instead of snapping to whatever the next
  detection says.
- **A lost face never resets the crop.** `hold_frames` keeps the last position, the gap is filled
  forward/backward (`_fill_gaps`), and only after that does the fallback centre apply.
- **The whole path is dense, not 10 keyframes.** `trajectory` emits one point per `expr_hz` step
  (181 points for an 18 s clip) and `_densify` resamples it to the output fps before it is written as
  a `sendcmd` file (`t crop x N;`). `sendcmd` holds a value until the next command, so without the
  resample the pan would visibly stair-step at 10 Hz.
- **Why `sendcmd` and not a giant expression.** The x-expression is still supported (preview fallback
  and tests), but a few hundred points make FFmpeg's recursive expression parser give up ("Failed to
  configure input pad"). `_piecewise_expr` therefore emits a **flat sum** of
  `(v0 + slope*(t-t0)) * between(t,t0,t1)` terms instead of nested `if(lt(...))`, which keeps the
  parse tree shallow and the curve continuous; `sendcmd` removes the limit entirely for the main path.
  The `path-*.cmd` file is deleted in the renderer's `finally` block.
- **Speaker switches pan, they do not cut.** A new speaker must hold the floor for
  `speaker_min_switch` seconds (hysteresis), and the move to them is bounded by `max_speed`, so it
  reads as a quick camera pan. The speed clamp runs **after** Savitzky-Golay: a polynomial fit rings
  around a step, and clamping last keeps the "no teleport" guarantee (measured: a 49.8 % single-step
  jump becomes 4.5 %).
- **Clip types are one config file.** `tracking/config.py` holds `DEFAULTS` plus a partial override
  per type — `auto, podcast, talking_head, gaming, tutorial, webinar, reaction, event` — each with its
  own smoothing, dead zone, hold frames, speaker-switch delay and layout (`follow`, `split_top_cam`,
  `pip_corner`). `TRACKING_<KEY>` environment variables override any default without touching code.
- **`auto` is a suggestion, not a decision.** `GET /api/editor/clip-types/detect/{project_id}`
  samples the clip, counts concurrent faces and reads the frame aspect, returns
  `{suggested, max_faces, samples, aspect, reason}`; the editor pre-selects it and the user can change
  it. `GET /api/editor/clip-types` returns the catalogue with the resolved parameters, which the
  editor shows as the "clip type" dropdown next to the streamer layout.
- **Smoothness is measured, not claimed.** `metrics` carries `jitter_raw_pct`, `jitter_smoothed_pct`,
  `jitter_cut_pct`, `peak_raw_pct`, `peak_smoothed_pct`, `jumps_*`, `hold_steps` and
  `speaker_switches`; the editor prints `jitter 0.65% → 0.40% (−37%) · 181 path points` under the
  preview. Reproduce the table with
  `backend/.venv/bin/python -m backend.scripts.tracking_bench VIDEO...` and A/B the crop path with
  `backend/.venv/bin/python -m backend.scripts.tracking_path_ab /tmp/out VIDEO CLIP_TYPE`.
- **Cost.** MediaPipe runs on CPU at ~10 Hz on a downscaled 640 px analysis frame: 0.8–3.5 s per
  19 s clip on an M1 (one face, one landmarker pass per detection), cached in `data/faces/`. The
  render itself is still **one FFmpeg pass** — the trajectory is a filter argument, not extra frames
  in OpenCV — so export time is unchanged.

### Shot list, hard cuts & dynamic zoom (Phase 7, stage 1)

- **The shot list is the single source of truth.** `backend/services/shots/` is a package that only
  *reads* the activity timeline that `tracking` already produced — no detection happens in it.
  Detection is expensive and cacheable; framing decisions are cheap and must stay editable.
  `model.Shot` carries `index, start, end, kind, zoom, transition, source, locked` with `start`/`end`
  in **absolute source seconds** (never frame numbers), so trimming a clip does not shift its shots.
  `plan.py` converts to clip-relative time at render time.
- **Kinds** are `follow_speaker`, `follow_person` (`person` label) and `whole_frame`. `split_screen`
  is deliberately absent — it was flagged `[Perlu Konfirmasi]` in the brief and is not implemented
  until asked for.
- **Cuts are placed on voice onset, not on the tracker's switch.** The tracker waits
  `speaker_min_switch` (1.5 s) before it believes a change; a cut needs the new face on screen on the
  first word, so `draft.build_draft` looks at the `talk` mask and cuts `cut_lookahead` (0.15 s)
  *before* the new speaker starts. A cut also needs `cut_min_talk` (0.8 s) of sustained speech, and
  the result is rate-limited: shots shorter than `shot_min_seconds` (2 s) are merged and the draft
  reports `cuts_per_minute`, which must stay under `cut_max_per_minute` (6).
- **Smoothing is per shot, in its own array.** Each shot is smoothed independently, so the One-Euro /
  dead-zone / SavGol / speed-clamp state is fresh at the first frame of a shot. That is what makes the
  cut snap instead of sliding from the old face to the new one — and the interior of a shot keeps
  exactly the smoothing Phase 6 had.
- **Two `sendcmd` commands on the same timestamp** mark a cut: the old value one millisecond before
  the boundary, the new value on the boundary. `sendcmd` holds a value until the next command, so
  without the pair there is one frame rendered somewhere between the two crops.
- **Zoom is applied backwards** — a static `crop`, then `scale=w=iw*z:h=ih*z:eval=frame`, then a fixed
  `crop` back to the output size. FFmpeg cannot change a `crop` size per frame, so enlarging and
  cropping back is the only form that survives in one pass. `zoompan` + `sendcmd` was tested and
  rejected (it re-evaluates the input and drops audio sync under `-ss`).
- **The zoom curve is one target per shot**, not a per-frame tracking value: the median face height of
  that shot sets the target, then a state machine holds → eases in → holds → eases out. A lens that
  follows the face every frame "breathes" on every syllable, which is exactly the "zoom goyang" the
  brief forbids. Measured on a real clip: `zoom 1.00-1.18`, max step 0.025/frame (0.25x/s), **1**
  direction change over 18 s.
- **The mouth gate is `jawOpen`, smoothed, with two thresholds** (`zoom_enter` 0.40 / `zoom_exit`
  0.18 on a 1 s moving average, relative to the p90 of that face). The `talk` mask is deliberately
  sparse — it exists to spot speaker changes — so it cannot drive a lens. `jawOpen` is not comparable
  between people or camera distances, hence the relative normalisation.
- **Zoom is capped by source height** (1080p → 1.4x, 720p → 1.25x, 480p → 1.15x, below → 1.05x) and
  the plan reports a warning when the cap bites, because zoom here is an upscale on top of the
  upscale the vertical crop already costs. The rate limit is re-applied *after* SavGol (a polynomial
  overshoots a ramp), so `zoom_max_rate` holds exactly.
- **Measured cost: +0.9 %.** 18 s of 1080p, `libx264`: 3.97 s flat vs 4.00 s with the zoom curve
  (the earlier +18 % figure came from a 300-term expression; `_rdp` compresses the curve to a handful
  of ramps). Still one FFmpeg pass.
- **Verified numerically, not by eye** — `backend/.venv/bin/python -m backend.scripts.shots_test`:
  (A) a red/blue split at t=2 s with a path jump: all 120 frames decode as pure red or pure blue,
  **0 mixed frames**, transition exactly at frame 60; (B) a 20 px white stripe on black measured
  through the real filter chain: 35/42/50 px for 1.0/1.2/1.4x (expected 35.5/42.6/49.7, ≤1.4 % error);
  (C) a 20 s scripted dialogue: cuts at 3.05 s and 7.05 s, the 0.3 s interjection suppressed, 6 cuts/min.
  On the real clip the rendered face is 1.181x larger at `z=1.175` (target 1.175, +0.5 %).
  `tracking_debug.py` draws the shot plan (white marker = cut) so the timeline can be compared to the
  source video.

### VAD, overlap → whole-frame draft & detection timeline (Phase 7, stage 2)

- **The VAD is a protocol, not a function.** `tracking/vad.py` defines `Vad.run(audio, params) ->
  VadResult` and `get_vad(params)` picks the backend from `vad_backend`. Two backends ship:
  `EnergyPitchVad` (default — RMS dB + autocorrelation pitch, `numpy`+`scipy`, **no new dependency**)
  and `SileroVad` (optional — ONNX through the `onnxruntime` that `faster-whisper` already installs).
  The pipeline only ever sees `VadResult.speech`. Every backend is handed **16 kHz mono float32**,
  because that is what webrtcvad and Silero both expect. `torch` stays banned.
- **The state machine is asymmetric on purpose.** Entering speech needs a *strict* test (a pitched
  frame, or energy `vad_margin_db` 6 dB above the noise floor); staying in speech only needs the
  *loose* one (`vad_release_db` 4 dB). With the strict test on both edges the mask broke on every
  consonant and recall fell to ~0.6; splitting them took recall to 0.89–0.94 with the same margin.
- **The noise floor is a percentile, not a constant.** `vad_floor_percentile` (10) of the RMS
  distribution per window — a podcast recorded 12 dB hotter than another still works. `vad_sweep.py`
  chose the defaults (margin 6.0 / release 4.0 / floor p10) against Whisper word spans.
- **Accuracy on real footage** (`backend/scripts/vad_eval.py`, three 40 s windows of a two-host
  podcast, reference = Whisper word spans ±0.25 s):

  | window (s) | precision | recall | FP | FN | word centre hit |
  | ---------- | --------- | ------ | -- | -- | --------------- |
  | 50–90      | 0.799     | 0.915  | 9.4/min | 3.5/min | 0.952 |
  | 90–130     | 0.897     | 0.886  | 4.7/min | 5.3/min | 0.949 |
  | 600–640    | 0.960     | 0.941  | 1.9/min | 2.8/min | 0.978 |

  Recall is the number that matters for cuts (a missed word can lose a cut); it stays above 0.88. The
  false positives are concentrated where the reference is weakest — the 3.65 s run at 1.06–4.71 s is
  intro music, and Whisper itself only marks 68–80 % of the window as speech.
- **Optional Silero backend** — `vad_backend = "silero"`, off by default. It runs
  `silero_vad_v6.onnx` through the `onnxruntime` that `faster-whisper` already pulls in, so it adds
  **no dependency and downloads nothing at runtime**; the model is looked up in `silero_model` /
  `SILERO_VAD_MODEL`, then `backend/models/`, then `faster_whisper/assets/`. If the package or the file
  is missing, `get_vad()` logs a warning and falls back to energy — a missing model must never fail a
  render. It reads 512-sample windows (32 ms) with 64 samples of context and uses hysteresis
  (`silero_threshold` 0.5 to open, `silero_release` 0.35 to stay open), so its mask is 32 ms granular
  against energy's 10 ms. `backend/scripts/vad_compare.py` scores both against the same Whisper words:

  | clip | window (s) | backend | precision | recall | F1 | FP/min | FN/min | speed |
  | ---- | ---------- | ------- | --------- | ------ | -- | ------ | ------ | ----- |
  | two-host podcast | 50–90 | energy | 0.799 | 0.915 | 0.853 | 9.4 | 3.5 | 293× |
  | two-host podcast | 50–90 | silero | 0.837 | 0.875 | 0.855 | 6.9 | 5.1 | 116× |
  | two-host podcast | 90–130 | energy | 0.897 | 0.886 | 0.891 | 4.7 | 5.3 | 274× |
  | two-host podcast | 90–130 | silero | 0.916 | 0.890 | 0.903 | 3.7 | 5.1 | 116× |
  | two-host podcast | 600–640 | energy | 0.960 | 0.941 | 0.951 | 1.9 | 2.8 | 298× |
  | two-host podcast | 600–640 | silero | 0.961 | 0.949 | 0.955 | 1.8 | 2.4 | 102× |
  | rendered clip | 1637–1702 | energy | 0.979 | 0.913 | 0.945 | 1.1 | 4.8 | 279× |
  | rendered clip | 1637–1702 | silero | 0.978 | 0.916 | 0.946 | 1.2 | 4.7 | 108× |
  | **mean** | | **energy** | **0.909** | **0.914** | **0.910** | **4.3** | **4.1** | **286×** |
  | **mean** | | **silero** | **0.923** | **0.907** | **0.915** | **3.4** | **4.3** | **110×** |

  Silero buys ~1.4 points of precision and a fifth of the false-positive seconds, and gives back ~0.7
  points of recall; on the rendered clip the two are within 0.001 F1. End to end the **shot draft does
  not change**: 4 shots / 3 cuts on the rendered clip, 2 shots / 1 cut on the 40 s window, the same
  boundaries to the centisecond with either backend (`face_service.detect(..., use_cache=False)`) —
  a 32 ms mask rarely moves a boundary past the 2 s minimum shot and the 0.15 s look-ahead.
  **The default stays energy**: 2.6× faster, three times the time resolution, no model file to ship,
  and a wash on accuracy. Reach for Silero on noisy material (music beds, laughter, a room with a fan),
  where the precision gain is the point.
- **Two people talking at once becomes one `whole_frame` shot.** `build_draft` bridges overlap gaps
  shorter than `overlap_max_gap`, drops runs shorter than `overlap_min_seconds` (1.2 s), and splits a
  run when the number of simultaneous talkers changes. A cut *inside* an overlap span is dropped, not
  merged, so the shot does not blink: 0.5 s of crosstalk inside a shot is ignored, 3.5 s becomes
  exactly one `whole_frame` shot with cuts on both edges (`shots_test.case_whole_frame`).
- **`whole_frame` is composited, not cropped.** `video_engine._whole_frame_chain` letterboxes the
  *untouched* landscape frame over a blurred, dimmed copy of itself and the engine overlays it on the
  follow-cam path with `overlay=...:enable='between(t,a,b)+...'`. One `split` of the source, still one
  FFmpeg pass, and the crop keeps following underneath so leaving the span needs no state. Verified on
  real 1080p: a 2–4 s span changes the frame by 30.4 mean abs, the frames outside it are **byte
  identical** (diff 0.0). Streamer `layout` presets ignore whole-frame spans (the layout owns the
  frame there); the render then drops the spans and adds a warning to the plan instead of
  silently compositing over the cam.
- **`tracking_debug.py` now shows the decision, not just the box**: face boxes labelled
  `A TALK jaw 0.42`, a HUD line per frame, the smoothed *and* raw crop window, an orange band over
  `whole_frame` spans, and a bottom timeline strip (shot blocks blue/orange, white cut ticks, yellow
  VAD band, one mouth lane per person, a "now" playhead). A one-frame cut is invisible at 10 fps, so
  the `CUT -> follow_speaker A` banner stays up for 0.5 s and counts down to the next cut.
- **`backend/scripts/timeline.py` is the static version of that strip** — one PNG per window with the
  same signals on one axis, plus `--contact` for a sheet of the frames 0.4 s either side of every cut,
  so a cut can be checked against the real video instead of trusted. It reads the **cached production
  payload** (`face_service.detect`), not a re-run, so what it shows is what a render would do.
  On a 120 s window of the podcast it reported 4 shots / 3 cuts (1.5 cuts/min); the contact sheet
  showed the source itself cutting camera at exactly those three points, i.e. the draft's cut landed
  on the source's own cut rather than inventing one.
- **Cost:** VAD adds ~0.15–0.3 s per 40 s window on CPU, once per cache fill — the shot draft is
  arithmetic on top of an already-cached timeline. Render time is unchanged (whole-frame spans only
  add one `split` + `overlay`).

### Manual shot editor (Phase 7, stage 3)

The draft is a starting point, not a verdict. Stage 3 puts the shot list in the editor and makes every
edit reach the export.

- **Stored shots live in their own sidecar**, `data/shots/<project>__<clip>.json` — not in the project
  JSON. Dragging a boundary therefore never rewrites the project, clearing the face cache never
  destroys an edit, and the endpoint can hand back *both* the stored list and a fresh draft so they
  can be compared. Writes go through a temp file + `os.replace` under a process lock, so a crash
  mid-save cannot leave a half-written list behind.
- **Edits are pure functions.** `backend/services/shot_edit.py` implements `split`, `merge`,
  `move_boundary`, `update` and `redetect` over a list of shots, with no I/O, no faces and no FFmpeg.
  Everything is snapped to `0.01 s` (finer than a 60 fps frame, coarse enough to keep the JSON clean)
  and normalised back to a gap-free list covering the clip window before it is stored or rendered, so
  the renderer can never receive an inconsistent list. Degenerate requests are refused rather than
  guessed at: a split closer than the minimum shot length, a merge with no neighbour, a boundary that
  would collapse a shot to nothing.
- **A manual decision survives re-detection.** Any shot with `source: "manual"` or `locked: true` is
  protected; Re-detect only refills the gaps around it. That is the difference between "run the
  detector again" and "throw my edit away", and it is what makes re-detecting safe after a trim change.
- **The export follows the list, not the draft.** `render_service` and `preview_service` both call
  `edit_service.stored_shots(...)` and pass it to `face_plan(..., shots=...)`, and the preview cache
  key folds in `shots_rev` (a hash of the stored list) so a preview rendered before an edit is never
  served after it. Verified end to end: with shot 2 switched to `whole_frame`, the export at t=40 s
  shows the untouched wide frame over the blurred background while t=10 s and t=62 s stay full-bleed
  crops with karaoke burned in — one 65 s clip, one FFmpeg pass, three different framings.
- **Endpoints** (all under `/api/editor/shots/{project_id}?clip_index=`):

  | Method | Path suffix | Purpose |
  | ------ | ----------- | ------- |
  | `GET` | — | stored list (`origin: stored`) or the draft (`origin: draft`) + metrics |
  | `PUT` | — | **deprecated** — replace the whole list; kept for compatibility, prefer the per-edit calls |
  | `POST` | `/split` `/merge` `/move` `/patch` `/redetect` | one edit per call |
  | `POST` | `/undo` `/redo` | walk the history stored in the sidecar (`409` when empty) |
  | `DELETE` | — | drop the stored list and fall back to the draft |

  `PUT` still works and still writes a sidecar, but it replaces the list wholesale without recording
  anything a UI could explain: no validation of the boundaries you sent, and the previous list lands in
  `past` as one opaque step. The per-edit endpoints (`/patch`, `/split`, `/merge`, `/move`, `/redetect`)
  describe the intent, so undo reads as "undo the split" and the response can carry the metrics of the
  list you just produced. New clients should not use `PUT`; it is documented here only so an existing
  integration keeps working.

  Every mutation is persisted before the response is returned — the browser never holds the only copy.
- **Undo/redo is server-side.** The history lives *in the same sidecar* as the list — `past` is a stack
  of previous shot lists, `future` the branch undo walked back over, both capped at 50 entries. A
  reload, a second tab, or a browser crash cannot lose it, and the buttons can never disagree with what
  will actually render. The first edit of a *draft* pushes the draft itself onto `past`, so the very
  first change is undoable too; any new edit after an undo drops the `future` branch, as expected.
  `Reset to auto` deletes the sidecar and the history with it — there is nothing left to walk back
  through.
- **In the UI** the shot strip is a row of blocks over the clip window: drag a boundary to move a cut,
  click a block to edit it, split at the playhead, merge with the next shot, lock a shot so Re-detect
  leaves it alone, undo/redo, and a badge that reads *Draft* or *Edited <time>*. Framing, person, zoom
  and zoom-entry are per shot; the strip shows `cuts/min` and the shot count live.
- **Framing panel.** The crop-centre slider writes `frameCx` (0..1), debounced 300 ms, and the shot is
  marked manual + locked so Re-detect leaves it alone. Arrow keys step 0.01, Shift+Arrow 0.05; the
  slider is bounded to the `[cxMin, cxMax]` window the backend computes from the crop width and zoom,
  and the value reported back is the *clamped* one the renderer actually uses. *Restore auto* clears
  `frameCx`, drops `source` back to `auto` and releases the slider's lock, so the shot follows the
  tracker again. The panel only appears while face tracking is on — without a tracked path there is no
  crop window to move, and the render is letterboxed.
- **Time base trap.** `shot_plan.build` emits crop points in clip-relative seconds; the segment
  renderer works in absolute source seconds. Slicing the plan with absolute bounds silently matched
  nothing and every segment fell back to the letterbox blur, which made the whole shot list look
  ignored. `preview_cache` now subtracts `start` before slicing — `preview_timing_test` locks it down.
- **Cost:** detection is cached on disk, but re-deriving the draft still costs ~8 s per call, which the
  editor would pay on every drag. `_draft_shots` keeps a small in-process memo (120 s TTL, 8 windows)
  so only the first open pays it: 10.2 s → 0.04 s. Re-detect passes `fresh=True` and always bypasses it.

## Roadmap

| Phase | Scope                                                                     |
| ----- | ------------------------------------------------------------------------- |
| 0     | Runnable scaffold, routes, API contract, i18n, Docker — **done**          |
| 1     | yt-dlp download, faster-whisper transcription, highlight scoring via your model — **done** |
| 2     | Project history in `data/projects`, reopen without re-transcribing — **done** |
| 3     | Clip Studio renderer: crop/letterbox, `.ass` karaoke subtitles, VideoToolbox → `libx264`, per-clip + ZIP export — **done** |
| 4     | Watermark burn-in, BGM mixing, merged compilation export — **done**        |
| 5     | Face tracking (YuNet/Haar), streamer presets, multi-segment hooks, editor with live preview — **done** |
| 6     | MediaPipe tracking pipeline (One-Euro + Savitzky-Golay + dense `sendcmd` path), clip-type presets with auto-detect — **done** |
| 7.1   | Shot list data model, hard cuts with look-ahead, per-shot smoothing, dynamic zoom — **done** |
| 7.2   | VAD, overlap → whole-frame draft, whole-frame compositing, detection timeline — **done** |
| 7.3   | Manual shot editor (timeline blocks, split/merge/lock, undo/redo) — **done** |
| 8.1   | Framing panel: crop-centre slider, clamp reporting, restore-auto, preview crop-point fix — **done** |

## License

MIT © 2026 ClipForge

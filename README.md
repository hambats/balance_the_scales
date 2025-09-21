# BearWatch

BearWatch is a lightweight RTSP monitoring stack that performs real-time YOLO detection, highlights a chosen class, and publishes alerts with annotated snapshots. The service ships with a Flask API, MJPEG stream, Discord webhook integration, and a responsive control dashboard.

## Features

- **Continuous Detection** – Ultralytics YOLO runs inside a background detector thread, smoothing FPS and enforcing cooldowns between alerts.
- **Target Monitoring** – Change the monitored class at runtime; alerts fire when a detection persists for a configurable number of frames.
- **MJPEG Streaming** – `/video` streams annotated frames to the dashboard and any MJPEG-compatible clients.
- **Alerting** – Captures are saved to disk, listed via `/alerts`, and optionally pushed to Discord webhooks.
- **Runtime Status** – `/status` exposes FPS, cooldown state, debug telemetry, and recent detections for observability.
- **Responsive Dashboard** – `index.html` provides controls for class selection, manual snapshots, live status, and alert history.

## Quick Start

1. Install dependencies (Python 3.11 recommended):

   ```bash
   python -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt
   ```

2. Run the service (either set `RTSP` in the environment or provide `--rtsp`):

   ```bash
   export RTSP="rtsp://camera/stream"
   python -m bearwatch --model yolov8n.pt
   ```

3. Open the dashboard at [http://localhost:8080](http://localhost:8080).

## Configuration

Every CLI flag has an environment variable equivalent. Common options:

| Flag | Env | Default | Description |
| --- | --- | --- | --- |
| `--model` | `YOLO_MODEL` | `yolov8n.pt` | YOLO weights to load |
| `--conf` | `CONF_THRES` | `0.5` | Detection confidence threshold |
| `--iou` | `IOU_THRES` | `0.5` | IOU threshold for NMS |
| `--req_frames` | `REQ_FRAMES` | `5` | Consecutive frames required before alerting |
| `--min_h_frac` | `MIN_H_FRAC` | `0.25` | Minimum bounding-box height fraction |
| `--cooldown` | `COOLDOWN_SEC` | `30` | Cooldown window (seconds) |
| `--imgsz` | `IMG_SIZE` | `640` | Inference input size |
| `--save_dir` | `SAVE_DIR` | `./data` | Base directory for saved captures |
| `--webhook` | `DISCORD_WEBHOOK` | `None` | Discord webhook for notifications |
| `--jpeg_quality` | `JPEG_QUALITY` | `80` | JPEG encoding quality |
| `--ff_retry` | `FF_RETRY_MS` | `2000` | Delay between RTSP reconnect attempts (ms) |
| `--debug` | `DEBUG` | `False` | Enable verbose logging and debug overlays |

## API Overview

| Method | Endpoint | Description |
| --- | --- | --- |
| `GET` | `/` | Dashboard HTML |
| `GET` | `/video` | Multipart MJPEG stream |
| `GET` | `/status` | System snapshot including FPS, cooldown, debug data |
| `GET` | `/alerts` | Recent alerts (optional `limit` query) |
| `POST` | `/monitor` | Update monitored class `{ "class": "bear" }` |
| `POST` | `/trigger` | Force-save current annotated frame |
| `GET` | `/capture/<filename>` | Serve saved JPEG |
| `GET` | `/config` | Current configuration sans secrets |

## Docker

A production image is provided via the included `Dockerfile`:

```bash
docker build -t bearwatch .
docker run --rm \
  -e RTSP="rtsp://camera/stream" \
  -p 8080:8080 \
  -v $(pwd)/data:/data \
  bearwatch
```

The container installs FFmpeg and OpenCV runtime dependencies, stores captures at `/data/captures`, and starts the application with `python -m bearwatch`.

## Development Notes

- The detector thread is a daemon that respects a shutdown event triggered at exit.
- Captures are stored under `SAVE_DIR/captures`; ensure persistent storage in production.
- Discord webhook failures are logged but never raise exceptions.
- The dashboard polls `/status` and `/alerts` with exponential backoff on errors.

## License

This project inherits the GPL-3.0 license from the original repository. See `LICENSE` for full details.

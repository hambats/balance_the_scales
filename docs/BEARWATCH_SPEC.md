# BearWatch Application Specification

This document captures the structure and behaviour of the **BearWatch** RTSP monitoring stack.  It is organised so a code-generation system (such as Codex) can recreate the application with minimal ambiguity.  The scope covers the Python back-end, HTTP API, front-end behaviour, runtime state, threading, configuration, and containerisation.

## 1. System Overview

BearWatch ingests an RTSP camera feed, runs YOLO object detection, and raises alerts when a configured target class (default: `bear`) persists across a configurable number of frames.  The service offers:

- Continuous inference using Ultralytics YOLO on incoming frames.
- A multipart MJPEG stream of annotated frames for browser consumption.
- REST endpoints for status, configuration, and alert history.
- Optional Discord webhook notifications with captured images.
- Persistent storage of alert snapshots.

```
Process Flow:
RTSP → OpenCV capture → detector loop → YOLO inference → annotation & alerting →
  ├─ MJPEG generator → Flask /video stream
  └─ Alert saver → Disk + in-memory log + Discord webhook
```

## 2. Python Back-End Architecture

### 2.1 Core Modules and Globals

Create a single `bearwatch.py` module that exposes the runtime entrypoint.  Important module-level objects:

| Name | Type | Purpose |
| --- | --- | --- |
| `ARGS` | `argparse.Namespace` | Parsed CLI/environment configuration (see §6). |
| `model` | `ultralytics.YOLO` | Loaded YOLO model specified by `ARGS.model`. |
| `monitored_class` | `str` | Currently watched class label; default from `ARGS.target_class`. |
| `available_classes` | `list[str]` | Derived from `model.names`. |
| `alert_log` | `collections.deque` | Recent alerts (max length 50) with dict entries `{ts, conf, path}`. |
| `alert_log_lock` | `threading.Lock` | Protects `alert_log`. |
| `current_frame` | `np.ndarray | None` | Latest annotated frame from detector loop. |
| `frame_lock` | `threading.Lock` | Protects `current_frame`. |
| `last_detection_ts` | `float | None` | `time.monotonic()` of last qualifying detection. |
| `last_alert_ts` | `float | None` | `time.monotonic()` of last dispatched alert (cooldown enforcement). |
| `last_debug` | `dict | None` | Snapshot containing FPS and detection metadata for `/status`. |
| `fps_smooth` | `float` | Exponential moving average of instantaneous FPS. |

Initialise locks and default state in module scope before starting threads.

### 2.2 Functions

#### `get_args() -> argparse.Namespace`
- Parse CLI and environment variables using `argparse`.
- CLI options mirror environment variables; CLI takes precedence.
- Enforce types:
  - `--conf`, `--iou`, `--min_h_frac` → `float`
  - `--req_frames`, `--cooldown`, `--imgsz`, `--jpeg_quality`, `--ff_retry`, `--port` → `int`
  - `--debug` → `store_true`
- Required argument: `--rtsp` unless `RTSP` env is set.
- Ensure `ARGS.save_dir` exists (`pathlib.Path.mkdir(parents=True, exist_ok=True)`).
- Normalize `target_class` to lowercase.

#### `open_capture(rtsp: str) -> cv2.VideoCapture`
- Use `cv2.VideoCapture` with backend `cv2.CAP_FFMPEG`.
- Respect `OPENCV_FFMPEG_CAPTURE_OPTIONS` env value if set (no transformation required).
- Return the capture handle; caller checks `isOpened()` and closes if needed.

#### `get_monitored() -> str`
- Thread-safe getter for `monitored_class` using a `threading.Lock` around reads/writes (reuse `alert_log_lock` or a dedicated `monitor_lock`).

#### `set_monitored(new_cls: str) -> bool`
- Validate `new_cls` against `available_classes` (case-insensitive match to canonical label).
- Update `monitored_class` on success and return `True`; return `False` otherwise.

#### `push_discord(jpg_path: Optional[str], conf: float) -> None`
- Skip immediately if `ARGS.webhook` is not supplied.
- POST multipart/form-data to Discord webhook with:
  - `content`: formatted message `BearWatch alert – {monitored_class} @ {conf:.2f}`
  - Optional file payload `files[0]` containing the JPEG.
- Use `requests.post` with timeout ≤ 5 seconds.
- Wrap entire body in `try/except` and swallow all exceptions; log warnings when failures occur.

#### `save_alert(frame: np.ndarray, conf: float) -> None`
- Compose filename: `det_%Y-%m-%dT%H-%M-%S.jpg` using local time.
- Save JPEG to `ARGS.save_dir / "captures"` (ensure subdirectory exists).
- Append log entry `{"ts": iso8601, "conf": conf, "path": f"/capture/{filename}"}` to `alert_log` under lock.
- Invoke `push_discord(saved_path, conf)`.

#### `detector_loop() -> None`
Daemon thread executing continuous detection:
1. Attempt to open the RTSP stream via `open_capture`.  On failure, sleep `ARGS.ff_retry / 1000` seconds and retry.
2. When capture opens, print log `Opened RTSP stream`.
3. Read frames in a loop:
   - If `.read()` fails, log `RTSP read failed`, release capture, sleep retry interval, and restart from step 1.
   - Measure timing segments (`pre`, `infer`, `post`) for debug output.
4. Run inference with `model.predict(frame, conf=ARGS.conf, iou=ARGS.iou, imgsz=ARGS.imgsz, verbose=False)`.
5. Build detection list `[ {"label": name, "conf": float, "box": [x1, y1, x2, y2]} ]` for each returned box.
6. Decide which boxes to draw:
   - When `ARGS.debug` is truthy, draw **all** detections.
   - Otherwise only draw boxes matching `monitored_class` whose height occupies at least `ARGS.min_h_frac` of frame height.
7. Overlay detection boxes and labels on the frame using OpenCV drawing functions.
8. Update FPS estimate with exponential smoothing (e.g., `fps_smooth = fps_smooth * 0.9 + instantaneous_fps * 0.1`).
9. Annotate frame text overlay: `f"FPS: {fps_smooth:.1f}"` and `f"Watching: {monitored_class}"`.
10. Maintain detection streak counter; when the monitored class is present for `ARGS.req_frames` consecutive frames **and** `cooldown` seconds have elapsed since `last_alert_ts`, call `save_alert` and update `last_alert_ts`.
11. Update `last_detection_ts` whenever the monitored class is seen (even if cooldown blocks alert).
12. Store annotated frame in `current_frame` under `frame_lock`.
13. Update `last_debug` with `{ "fps": fps_smooth, "speed": {"pre": float, "infer": float, "post": float}, "boxes": detection_list }`.
14. Loop until process exit; thread should respect a `threading.Event` (`shutdown_event`) if the main thread sets it.

#### `mjpeg_generator() -> Iterator[bytes]`
- Infinite generator that waits on new frames (poll under `frame_lock`).
- Encode `current_frame` to JPEG using `cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, ARGS.jpeg_quality])`.
- Yield multipart chunks in the format:
  ```
  --frame\r\nContent-Type: image/jpeg\r\n\r\n<bytes>\r\n
  ```
- Sleep briefly (e.g., 0.05s) if `current_frame` is `None`.

### 2.3 Flask Application

Construct a Flask app inside `bearwatch.py` (or `app.py`).  Register routes detailed in §3. Start detector thread before `app.run()`.

### 2.4 Logging

- Use Python `logging` configured at INFO level by default, DEBUG when `ARGS.debug`.
- Emit structured messages for RTSP reconnects, alert saves, and webhook failures.

## 3. HTTP API Specification

All API endpoints reside under the root Flask app.

### 3.1 Common Conventions
- Responses are JSON unless otherwise noted.
- Errors use JSON body `{"error": "message"}` with appropriate HTTP status (400 for bad input, 500 for internal errors).
- Timestamps are ISO 8601 with timezone `Z` (UTC).

### 3.2 Endpoints

#### `GET /`
Serve `index.html` located beside the Flask app file. If the file does not exist, return a minimal inline HTML page that includes the MJPEG stream and control UI skeleton.

#### `GET /video`
- Content-Type: `multipart/x-mixed-replace; boundary=frame`
- Body: yielded from `mjpeg_generator()`.

#### `GET /favicon.ico`, `/apple-touch-icon.png`, `/apple-touch-icon-precomposed.png`
Return HTTP 204 with empty body to silence browser icon requests.

#### `GET /status`
Return system snapshot.

Response schema:
```json
{
  "watching": "bear",
  "classes": ["bear", "person", "car"],
  "req_frames": 5,
  "min_h_frac": 0.3,
  "cooldown": {
    "seconds": 30,
    "remaining": 12.4
  },
  "last_detection": {
    "ts": "2024-05-12T07:15:24Z",
    "conf": 0.86
  },
  "last_alert": {
    "ts": "2024-05-12T07:15:00Z",
    "conf": 0.91,
    "path": "/capture/det_2024-05-12T07-15-00.jpg"
  },
  "alerts": [
    {"ts": "2024-05-12T07:15:00Z", "conf": 0.91, "path": "/capture/det_2024-05-12T07-15-00.jpg"}
  ],
  "fps": {
    "smoothed": 28.4,
    "raw": 30.1
  },
  "debug": {
    "speed": {"pre": 2.1, "infer": 48.7, "post": 3.5},
    "boxes": [
      {"label": "bear", "conf": 0.86, "box": [233, 120, 412, 468] }
    ]
  }
}
```
- `classes` enumerates available YOLO labels.
- `cooldown.remaining` is zero when alerts are allowed immediately.
- `debug` is `null` when no detections have run yet or when `ARGS.debug` is false.

#### `GET /alerts`
- Optional query param `limit` (int, default 20).
- Response: `{ "alerts": [ ... latest first ... ] }` using same entries as `/status.alerts`.

#### `POST /monitor`
- Request body: `{ "class": "bear" }`
- Behaviour:
  - Invoke `set_monitored(payload["class"])`.
  - On success: 200 with `{ "watching": "bear" }`.
  - On failure (unknown class): 400 with `{ "error": "unknown_class", "classes": [...] }`.

#### `POST /trigger`
- Forces writing the current annotated frame as an alert (ignoring streak/cooldown) for manual captures.
- Body optional; respond 202 with alert entry or 409 if `current_frame` is `None`.

#### `GET /capture/<path:filename>`
- Serve saved JPEGs from `ARGS.save_dir / "captures"` using `send_from_directory`.
- Apply `Cache-Control: no-cache` to ensure fresh viewing.

#### `GET /config`
- Response: direct reflection of `ARGS` namespace (convert to JSON-serializable dictionary) excluding secrets (e.g., webhook URL).

## 4. Front-End Specification

### 4.1 Layout
- Static `index.html` served by Flask.
- Page sections:
  1. **Header** with application title and status indicator (`<span id="watching">`).
  2. **Video Panel**: `<img id="video-stream" src="/video" alt="Live stream">`.
  3. **Controls**: select drop-down for target class, numeric displays for thresholds, button to trigger manual capture.
  4. **Alerts List**: scrollable list showing recent alerts with thumbnail and timestamp.
  5. **Debug Panel** (toggle) showing raw JSON from `status.debug` when `ARGS.debug`.

### 4.2 JavaScript Modules
Include a small bundled script (vanilla JS) with the following functions:

| Function | Responsibility |
| --- | --- |
| `fetchStatus()` | `fetch('/status')` and return parsed JSON. |
| `fetchAlerts(limit = 20)` | `fetch(`/alerts?limit=${limit}`)` returning JSON. |
| `renderStatus(status)` | Update DOM text for watching class, fps, cooldown, thresholds. |
| `renderAlerts(alerts)` | Populate alerts list with `<li>` entries linking to `/capture/...`. |
| `populateClassOptions(classes)` | Fill `<select id="class-select">` and mark current value. |
| `handleClassChange(event)` | POST `/monitor` with selected class and handle errors (toast message). |
| `handleTriggerClick()` | POST `/trigger` and prepend returned alert to list. |
| `startPolling()` | Kick off `setInterval` every 2 seconds to refresh status/alerts. |
| `stopPolling()` | Clear interval (used on visibility change). |
| `init()` | Bind listeners, populate initial data, start polling on DOMContentLoaded. |
| `formatTimestamp(ts)` | Convert ISO timestamp to local human-readable string. |

Use a simple global `state` object:
```js
const state = {
  pollingTimer: null,
  lastAlerts: [],
  classes: [],
};
```
When `/status` returns new classes, update `state.classes` and repopulate options.

### 4.3 Styling Expectations
- Minimal CSS (Tailwind or custom) is acceptable; ensure responsive layout that stacks vertically on narrow screens.
- Use `.alert-entry` cards with thumbnail `<img>` referencing `/capture/...` and metadata.

### 4.4 Error Handling
- Display toast/banner when API errors occur.
- Reconnect logic: if `/status` fetch fails, exponentially backoff between attempts (start at 2s up to 30s) and show offline indicator.

## 5. Runtime State & Threading Model

- **Detector Thread**: background daemon performing capture, inference, and writing to shared state.
- **Main Thread**: runs Flask development server (use `threaded=True` in production or rely on `waitress` / `gunicorn`).
- Locks ensure safe concurrent access to `current_frame`, `alert_log`, and `monitored_class`.
- Use `threading.Event` named `shutdown_event` to signal loop termination on process exit (register via `atexit` to set event and join thread with timeout).

## 6. Configuration (CLI & Environment)

`get_args()` must support both CLI flags and environment variables (ENV names in parentheses):

| Flag | Env | Type | Default | Description |
| --- | --- | --- | --- | --- |
| `--rtsp` | `RTSP` | `str` | **required** | RTSP stream URL. |
| `--model` | `YOLO_MODEL` | `str` | `"yolov8n.pt"` | Path/name for YOLO weights. |
| `--conf` | `CONF_THRES` | `float` | `0.5` | Detection confidence threshold. |
| `--iou` | `IOU_THRES` | `float` | `0.5` | IOU threshold for NMS. |
| `--req_frames` | `REQ_FRAMES` | `int` | `5` | Required consecutive frames before alert. |
| `--min_h_frac` | `MIN_H_FRAC` | `float` | `0.25` | Minimum bounding box height fraction. |
| `--cooldown` | `COOLDOWN_SEC` | `int` | `30` | Cooldown between alerts (seconds). |
| `--imgsz` | `IMG_SIZE` | `int` | `640` | Inference image size. |
| `--save_dir` | `SAVE_DIR` | `Path` | `./data` | Root directory for persisted files. |
| `--host` | `HOST` | `str` | `0.0.0.0` | Flask bind address. |
| `--port` | `PORT` | `int` | `8080` | Flask port. |
| `--webhook` | `DISCORD_WEBHOOK` | `str | None` | `None` | Discord webhook URL for alerts. |
| `--target_class` | `TARGET_CLASS` | `str` | `"bear"` | Default class to monitor. |
| `--jpeg_quality` | `JPEG_QUALITY` | `int` | `80` | JPEG encoder quality (0–100). |
| `--ff_retry` | `FF_RETRY_MS` | `int` | `2000` | Delay between RTSP reconnection attempts (ms). |
| `--debug` | `DEBUG` | `bool` | `False` | Enable verbose logs and draw all detections. |

## 7. Persistence & Filesystem Layout

- `ARGS.save_dir`
  - `/captures` : JPEG snapshots created by `save_alert`.
  - `/logs` (optional): store rotating log file if desired.
- Ensure directories are created on startup.
- Maintain `alert_log` deque in memory only; persisted alerts exist as image files.

## 8. Containerisation

Provide a Dockerfile with the following characteristics:

1. Base image `python:3.11-slim`.
2. Install system dependencies: `ffmpeg`, `libgl1`, `libglib2.0-0` (required by OpenCV/YOLO).
3. Copy application source and `requirements.txt` (`flask`, `ultralytics`, `opencv-python-headless`, `requests`, `python-dotenv`).
4. Run `pip install --no-cache-dir -r requirements.txt`.
5. Create non-root user `appuser` (optional best practice).
6. Expose `${PORT}` (default 8080).
7. Set environment defaults for `HOST=0.0.0.0` and `SAVE_DIR=/data`.
8. Declare volume `/data` for persistent captures.
9. Entrypoint: `python -m bearwatch --rtsp ${RTSP}` with ability to pass additional flags via `CMD`.

### 8.1 Docker Compose (Optional)

Document sample `docker-compose.yml` snippet:
```yaml
services:
  bearwatch:
    build: .
    environment:
      RTSP: rtsp://camera/stream
      YOLO_MODEL: yolov8n.pt
      DISCORD_WEBHOOK: ${DISCORD_WEBHOOK:-}
    volumes:
      - ./data:/data
    ports:
      - "8080:8080"
```

## 9. Application Boot Sequence

1. Parse configuration via `get_args()`.
2. Configure logging.
3. Load YOLO model into GPU if available (`model.to("cuda")` when `torch.cuda.is_available()`).
4. Populate `available_classes` from model metadata.
5. Start detector thread (`threading.Thread(target=detector_loop, daemon=True)`).
6. Launch Flask app bound to `ARGS.host:ARGS.port`.

## 10. Testing & Diagnostics

- Provide script `scripts/test_camera.py` (optional) to validate RTSP connectivity by grabbing a single frame.
- Unit tests may mock YOLO model to test `detector_loop` logic in isolation.
- Use `pytest` fixtures to confirm API endpoints return expected JSON when state is stubbed.

## 11. Future Enhancements (Non-functional Requirements)

- Add authentication for API endpoints when exposing publicly.
- Expand notification channels (SMS, email).
- Provide web UI toggles to adjust thresholds without restarts.
- Implement retention policy for captures (auto-delete after N days).

---

This specification should enable deterministic regeneration of the BearWatch system across back-end, front-end, and deployment layers.

"""BearWatch RTSP monitoring service."""
from __future__ import annotations

import argparse
import atexit
import logging
import os
import threading
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, List, Optional

import cv2
import numpy as np
import requests
from flask import (Flask, Response, jsonify, make_response, request,
                   send_from_directory)
from ultralytics import YOLO


ARGS: Optional[argparse.Namespace] = None
model: Optional[YOLO] = None
available_classes: List[str] = []
monitored_class: str = ""

alert_log: deque = deque(maxlen=50)
alert_log_lock = threading.Lock()
monitor_lock = threading.Lock()
frame_lock = threading.Lock()
debug_lock = threading.Lock()

current_frame: Optional[np.ndarray] = None
last_detection_ts: Optional[float] = None
last_detection_info: Optional[dict] = None
last_alert_ts: Optional[float] = None
last_alert_entry: Optional[dict] = None
last_debug: Optional[dict] = None
fps_smooth: float = 0.0
last_fps_raw: float = 0.0
shutdown_event = threading.Event()
detector_thread: Optional[threading.Thread] = None

app = Flask(__name__)


# ---------------------------------------------------------------------------
# Configuration & initialisation
# ---------------------------------------------------------------------------

def _env_default(name: str, cast, default):
    raw = os.getenv(name)
    if raw is None:
        return default
    if cast is bool:
        return raw.lower() in {"1", "true", "yes", "on"}
    return cast(raw)


def get_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    """Parse CLI arguments and environment variables."""
    parser = argparse.ArgumentParser(description="BearWatch RTSP monitoring service")
    env = os.getenv

    parser.add_argument("--rtsp", default=env("RTSP"), help="RTSP stream URL", required=env("RTSP") is None)
    parser.add_argument("--model", default=env("YOLO_MODEL", "yolov8n.pt"))
    parser.add_argument("--conf", type=float, default=_env_default("CONF_THRES", float, 0.5))
    parser.add_argument("--iou", type=float, default=_env_default("IOU_THRES", float, 0.5))
    parser.add_argument("--req_frames", type=int, default=_env_default("REQ_FRAMES", int, 5))
    parser.add_argument("--min_h_frac", type=float, default=_env_default("MIN_H_FRAC", float, 0.25))
    parser.add_argument("--cooldown", type=int, default=_env_default("COOLDOWN_SEC", int, 30))
    parser.add_argument("--imgsz", type=int, default=_env_default("IMG_SIZE", int, 640))
    parser.add_argument("--save_dir", default=env("SAVE_DIR", "./data"))
    parser.add_argument("--host", default=env("HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=_env_default("PORT", int, 8080))
    parser.add_argument("--webhook", default=env("DISCORD_WEBHOOK"))
    parser.add_argument("--target_class", default=env("TARGET_CLASS", "bear"))
    parser.add_argument("--jpeg_quality", type=int, default=_env_default("JPEG_QUALITY", int, 80))
    parser.add_argument("--ff_retry", type=int, default=_env_default("FF_RETRY_MS", int, 2000))
    parser.add_argument("--debug", action="store_true", default=_env_default("DEBUG", bool, False))

    args = parser.parse_args(argv)

    save_dir = Path(args.save_dir).expanduser().resolve()
    save_dir.mkdir(parents=True, exist_ok=True)
    captures_dir = save_dir / "captures"
    captures_dir.mkdir(parents=True, exist_ok=True)

    args.save_dir = save_dir
    args.target_class = str(args.target_class).lower()
    args.jpeg_quality = int(np.clip(args.jpeg_quality, 0, 100))

    return args


def _configure_logging(debug: bool) -> None:
    level = logging.DEBUG if debug else logging.INFO
    logging.basicConfig(level=level, format="%(asctime)s [%(levelname)s] %(message)s")


def _normalise_class(name: str) -> Optional[str]:
    lower = name.lower()
    for label in available_classes:
        if label.lower() == lower:
            return label
    return None


# ---------------------------------------------------------------------------
# Shared state helpers
# ---------------------------------------------------------------------------

def get_monitored() -> str:
    with monitor_lock:
        return monitored_class


def set_monitored(new_cls: str) -> bool:
    global monitored_class
    canonical = _normalise_class(new_cls)
    if canonical is None:
        return False
    with monitor_lock:
        monitored_class = canonical
    logging.info("Monitoring class set to %s", canonical)
    return True


def open_capture(rtsp: str) -> cv2.VideoCapture:
    return cv2.VideoCapture(rtsp, cv2.CAP_FFMPEG)


def _iso_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def push_discord(jpg_path: Optional[Path], conf: float) -> None:
    if not ARGS or not ARGS.webhook:
        return
    data = {"content": f"BearWatch alert – {get_monitored()} @ {conf:.2f}"}
    files = None
    try:
        if jpg_path:
            files = {"files[0]": (jpg_path.name, jpg_path.read_bytes(), "image/jpeg")}
        response = requests.post(ARGS.webhook, data=data, files=files, timeout=5)
        if response.status_code >= 400:
            logging.warning("Discord webhook returned %s: %s", response.status_code, response.text)
    except Exception as exc:  # noqa: BLE001 - swallow all per spec
        logging.warning("Failed to push Discord notification: %s", exc)


def save_alert(frame: np.ndarray, conf: float) -> dict:
    global last_alert_entry, last_alert_ts
    timestamp = _iso_now()
    filename = f"det_{timestamp.replace(':', '-')}" + ".jpg"
    captures_dir = ARGS.save_dir / "captures"
    captures_dir.mkdir(parents=True, exist_ok=True)
    path = captures_dir / filename
    cv2.imwrite(str(path), frame)

    entry = {"ts": timestamp, "conf": float(conf), "path": f"/capture/{filename}"}
    with alert_log_lock:
        alert_log.appendleft(entry)
    last_alert_entry = entry
    last_alert_ts = time.monotonic()
    push_discord(path, conf)
    logging.info("Saved alert to %s (conf=%.2f)", path, conf)
    return entry


# ---------------------------------------------------------------------------
# Detector loop
# ---------------------------------------------------------------------------

def detector_loop() -> None:
    global current_frame, fps_smooth, last_debug, last_detection_ts, last_detection_info, last_fps_raw
    retry_seconds = max(ARGS.ff_retry / 1000.0, 0.1)
    streak = 0
    best_conf = 0.0
    while not shutdown_event.is_set():
        cap = open_capture(ARGS.rtsp)
        if not cap.isOpened():
            logging.warning("Failed to open RTSP stream; retrying in %.2fs", retry_seconds)
            shutdown_event.wait(retry_seconds)
            continue
        logging.info("Opened RTSP stream")
        while not shutdown_event.is_set():
            loop_start = time.perf_counter()
            ok, frame = cap.read()
            pre_end = time.perf_counter()
            if not ok or frame is None:
                logging.warning("RTSP read failed; reconnecting")
                cap.release()
                shutdown_event.wait(retry_seconds)
                break

            try:
                results = model.predict(frame, conf=ARGS.conf, iou=ARGS.iou, imgsz=ARGS.imgsz, verbose=False)
            except Exception as exc:  # noqa: BLE001
                logging.exception("Model prediction failed: %s", exc)
                shutdown_event.wait(retry_seconds)
                break
            infer_end = time.perf_counter()

            detection_list = []
            draw_list = []
            names = getattr(results[0], "names", None) if results else None
            if not names:
                names = {i: name for i, name in enumerate(available_classes)}
            frame_h = frame.shape[0]
            saw_target = False
            best_conf = 0.0

            for result in results:
                boxes = getattr(result, "boxes", None)
                if boxes is None:
                    continue
                xyxy = boxes.xyxy.cpu().numpy() if hasattr(boxes.xyxy, "cpu") else boxes.xyxy
                confs = boxes.conf.cpu().numpy() if hasattr(boxes.conf, "cpu") else boxes.conf
                classes = boxes.cls.cpu().numpy() if hasattr(boxes.cls, "cpu") else boxes.cls
                for idx, coords in enumerate(xyxy):
                    label_idx = int(classes[idx])
                    if isinstance(names, dict):
                        label = names.get(label_idx, str(label_idx))
                    else:
                        label = names[label_idx] if 0 <= label_idx < len(names) else str(label_idx)
                    conf = float(confs[idx])
                    x1, y1, x2, y2 = map(int, coords.tolist()) if hasattr(coords, "tolist") else map(int, coords)
                    detection = {"label": label, "conf": conf, "box": [int(x1), int(y1), int(x2), int(y2)]}
                    detection_list.append(detection)

                    height_frac = (y2 - y1) / float(frame_h)
                    if ARGS.debug or label.lower() == get_monitored().lower():
                        draw_entry = (label, conf, x1, y1, x2, y2, height_frac)
                        draw_list.append(draw_entry)
                    if label.lower() == get_monitored().lower() and height_frac >= ARGS.min_h_frac:
                        saw_target = True
                        best_conf = max(best_conf, conf)
            post_start = time.perf_counter()

            annotated = frame.copy()
            for label, conf, x1, y1, x2, y2, height_frac in draw_list:
                if not ARGS.debug and label.lower() != get_monitored().lower():
                    continue
                if not ARGS.debug and height_frac < ARGS.min_h_frac:
                    continue
                cv2.rectangle(annotated, (x1, y1), (x2, y2), (0, 255, 0), 2)
                text = f"{label} {conf:.2f}"
                cv2.putText(annotated, text, (x1, max(y1 - 10, 0)), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 0), 1)

            now_monotonic = time.monotonic()
            if saw_target:
                last_detection_ts = now_monotonic
                last_detection_info = {"ts": _iso_now(), "conf": float(best_conf)}
                streak += 1
            else:
                streak = 0

            cooldown_elapsed = (last_alert_ts is None) or (now_monotonic - last_alert_ts >= ARGS.cooldown)
            if saw_target and streak >= ARGS.req_frames and cooldown_elapsed:
                save_alert(annotated, best_conf)

            cv2.putText(annotated, f"FPS: {fps_smooth:.1f}", (10, 20), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 2)
            cv2.putText(annotated, f"Watching: {get_monitored()}", (10, 45), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 2)

            loop_end = time.perf_counter()
            raw_fps = 1.0 / max(loop_end - loop_start, 1e-6)
            last_fps_raw = raw_fps
            if fps_smooth:
                fps_smooth = fps_smooth * 0.9 + raw_fps * 0.1
            else:
                fps_smooth = raw_fps

            with frame_lock:
                current_frame = annotated

            debug_payload = {
                "fps": fps_smooth,
                "speed": {
                    "pre": (pre_end - loop_start) * 1000.0,
                    "infer": (infer_end - pre_end) * 1000.0,
                    "post": (loop_end - post_start) * 1000.0,
                },
                "boxes": detection_list,
            }
            with debug_lock:
                last_debug = debug_payload if ARGS.debug else None

            if shutdown_event.wait(0):
                break
        cap.release()
    logging.info("Detector loop exiting")


# ---------------------------------------------------------------------------
# MJPEG generator
# ---------------------------------------------------------------------------

def mjpeg_generator() -> Iterator[bytes]:
    while not shutdown_event.is_set():
        with frame_lock:
            frame = None if current_frame is None else current_frame.copy()
        if frame is None:
            time.sleep(0.05)
            continue
        success, encoded = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), ARGS.jpeg_quality])
        if not success:
            time.sleep(0.05)
            continue
        chunk = b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + bytearray(encoded) + b"\r\n"
        yield chunk


# ---------------------------------------------------------------------------
# Flask routes
# ---------------------------------------------------------------------------


@app.get("/")
def index() -> Response:
    index_path = Path(__file__).with_name("index.html")
    if index_path.exists():
        return make_response(index_path.read_text(encoding="utf-8"), 200, {"Content-Type": "text/html; charset=utf-8"})
    fallback = """<!doctype html><html><body><h1>BearWatch</h1><img src='/video' alt='video'></body></html>"""
    return make_response(fallback, 200, {"Content-Type": "text/html; charset=utf-8"})


@app.get("/favicon.ico")
@app.get("/apple-touch-icon.png")
@app.get("/apple-touch-icon-precomposed.png")
def icons() -> Response:
    return Response(status=204)


@app.get("/video")
def video() -> Response:
    return Response(mjpeg_generator(), mimetype="multipart/x-mixed-replace; boundary=frame")


@app.get("/status")
def status() -> Response:
    now = time.monotonic()
    remaining = 0.0
    if last_alert_ts is not None:
        remaining = max(0.0, ARGS.cooldown - (now - last_alert_ts))
    with alert_log_lock:
        alerts_snapshot = list(alert_log)
    payload = {
        "watching": get_monitored(),
        "classes": available_classes,
        "req_frames": ARGS.req_frames,
        "min_h_frac": ARGS.min_h_frac,
        "cooldown": {"seconds": ARGS.cooldown, "remaining": remaining},
        "last_detection": last_detection_info,
        "last_alert": last_alert_entry,
        "alerts": alerts_snapshot,
        "fps": {"smoothed": fps_smooth, "raw": last_fps_raw},
        "debug": None,
    }
    with debug_lock:
        if ARGS.debug:
            payload["debug"] = last_debug
    return jsonify(payload)


@app.get("/alerts")
def alerts() -> Response:
    try:
        limit = int(request.args.get("limit", 20))
    except ValueError:
        return jsonify({"error": "invalid_limit"}), 400
    with alert_log_lock:
        items = list(alert_log)[:limit]
    return jsonify({"alerts": items})


@app.post("/monitor")
def monitor() -> Response:
    data = request.get_json(silent=True) or {}
    cls = data.get("class")
    if not cls:
        return jsonify({"error": "missing_class"}), 400
    if not set_monitored(cls):
        return jsonify({"error": "unknown_class", "classes": available_classes}), 400
    return jsonify({"watching": get_monitored()})


@app.post("/trigger")
def trigger() -> Response:
    with frame_lock:
        if current_frame is None:
            return jsonify({"error": "no_frame"}), 409
        frame = current_frame.copy()
    entry = save_alert(frame, conf=1.0)
    return jsonify(entry), 202


@app.get("/capture/<path:filename>")
def capture(filename: str) -> Response:
    captures_dir = ARGS.save_dir / "captures"
    response = send_from_directory(captures_dir, filename)
    response.headers["Cache-Control"] = "no-cache"
    return response


@app.get("/config")
def config() -> Response:
    data = {}
    for key, value in vars(ARGS).items():
        if key == "webhook":
            continue
        if isinstance(value, Path):
            data[key] = str(value)
        else:
            data[key] = value
    return jsonify(data)


# ---------------------------------------------------------------------------
# Bootstrap
# ---------------------------------------------------------------------------

def _start_detector() -> None:
    global detector_thread
    if detector_thread and detector_thread.is_alive():
        return
    detector_thread = threading.Thread(target=detector_loop, name="detector", daemon=True)
    detector_thread.start()


def _shutdown() -> None:
    shutdown_event.set()
    if detector_thread:
        detector_thread.join(timeout=5)


def create_app(parsed_args: Optional[argparse.Namespace] = None) -> Flask:
    global ARGS, model, available_classes, monitored_class
    if ARGS is None:
        args = parsed_args or get_args()
        _configure_logging(args.debug)
        logging.info("Loading model %s", args.model)
        loaded_model = YOLO(args.model)
        names = loaded_model.names
        if isinstance(names, dict):
            classes = [names[i] for i in sorted(names.keys())]
        else:
            classes = list(names)
        if not classes:
            raise RuntimeError("Model provided no class names")

        requested_class = args.target_class.lower()
        canonical = None
        for label in classes:
            if label.lower() == requested_class:
                canonical = label
                break
        if canonical is None:
            canonical = classes[0]
            logging.warning("Target class %s not found; defaulting to %s", requested_class, canonical)
        args.target_class = canonical

        ARGS = args
        model = loaded_model
        available_classes = list(classes)
        with monitor_lock:
            monitored_class = canonical

        atexit.register(_shutdown)
        _start_detector()
    return app


def main(argv: Optional[List[str]] = None) -> None:
    create_app(get_args(argv))
    app.run(host=ARGS.host, port=ARGS.port, threaded=True)


if __name__ == "__main__":
    main()

# Nature Watch

Backyard wildlife monitor that detects motion via webcam and uploads 20-second video clips to AWS S3.

Runs on a Raspberry Pi 4 with a USB webcam, or on any laptop using the integrated camera.

## How it works

1. Reads frames continuously from the configured camera
2. Detects motion using OpenCV frame differencing
3. Records a 20-second MP4 clip when motion is detected
4. Uploads the clip to S3 under a `YYYY-MM-DD/HH/` partition
5. Enforces a hard cap of 30 clips/hour to prevent S3 flooding
6. Buffers clips to disk when offline and retries automatically on reconnect

## Requirements

- Python 3.9+
- A camera (USB webcam or integrated laptop camera)
- AWS credentials with `s3:PutObject` on your bucket

## Installation

```bash
git clone <repo-url> nature-watch
cd nature-watch
pip install -r requirements.txt
cp .env.example .env
```

Edit `.env` and set at minimum:

```
NW_S3_BUCKET=your-bucket-name
AWS_ACCESS_KEY_ID=...
AWS_SECRET_ACCESS_KEY=...
```

## Configuration

All settings live in `config.yaml`. Every value can be overridden with an `NW_` environment variable (e.g. `NW_CAMERA_INDEX=1`).

| Setting | Default | Description |
| --- | --- | --- |
| `camera_index` | `0` | cv2 device index (`0` = first camera) |
| `motion_threshold` | `500` | Minimum contour area (px²) to count as motion |
| `motion_blur_ksize` | `21` | Gaussian blur kernel size (must be odd) |
| `clip_duration_s` | `20` | Length of each recorded clip in seconds |
| `clip_cooldown_s` | `10` | Seconds to wait after a clip ends before re-triggering |
| `clips_per_hour_cap` | `30` | Hard rate limit per hour |
| `local_buffer_dir` | `./buffer` | Local spool directory for offline buffering |
| `s3_bucket` | — | **Required.** S3 bucket name |
| `s3_prefix` | `wildlife` | Top-level prefix inside the bucket |
| `aws_region` | `us-east-1` | AWS region |
| `health_port` | `8080` | Port for the health check HTTP server |

## Running

```bash
python -m src.main
```

The app logs to stdout. Stop with `Ctrl-C` or `SIGTERM`.

**Laptop test** — no changes needed; the integrated camera is typically index `0`.

**USB webcam on Pi** — also typically index `0`. If you have a second device attached, set `camera_index: 1` in `config.yaml`.

## Snapshot viewer

A tkinter GUI for browsing snapshots and motion clips stored in S3. Media is organised by the same partition structure used in S3 — you click into a date, then an hour, then see thumbnails for that window. Favorites and albums are also supported.

### Prerequisites (first time only)

```bash
sudo apt-get install python3.13-tk   # or python3-tk if using system Python
pip install Pillow
```

### Launch

```bash
python -m src.viewer
```

### Navigation

The viewer mirrors the S3 folder hierarchy. No media is downloaded until you reach a leaf partition.

```
Home
├── Snapshots
│   ├── 2026-04-24          ← Central Time date, grouped client-side
│   └── 2026-04-23
└── Wildlife Clips
    ├── 2026-04-24 (UTC)    ← S3 partition date (UTC)
    │   ├── 09:00 CDT       ← hour converted to Central Time
    │   └── 14:00 CDT
    └── 2026-04-23 (UTC)
```

A breadcrumb bar at the top shows your current path and lets you jump back to any level.

### Actions

| Action | How |
| --- | --- |
| Browse by date | Click **Snapshots** or **Wildlife Clips**, then click a date card |
| Browse by hour (clips) | Click a date card, then click an hour card |
| Open a photo full-size | Click any thumbnail |
| Navigate between photos | Use **← Prev** / **Next →** in the detail window |
| Play a video clip | Click an MP4 thumbnail — downloads and opens in the system video player |
| Favourite a photo | Click **☆ Favorite** in the detail window |
| Filter to favourites | Click **★ Favorites** in the toolbar |
| Add to album | Click **+ Add to Album** in the detail window and type a name |
| Browse an album | Click **Albums ▾** in the toolbar and select from the menu |
| Return home | Click **⌂ Home** in the toolbar |

Favorites and albums are stored locally in `viewer_meta.json` (next to `config.yaml`) and persist between sessions. All times are displayed in **Central Time**.

### S3 cost controls

The viewer is designed to minimise S3 data transfer:

- **Partition navigation** makes only lightweight LIST calls — no media is downloaded until you reach a thumbnail grid.
- **Viewport-aware loading** downloads thumbnails only for cells visible in the current scroll window (~10 at a time). Scrolling loads the next batch; scrolling back is free (cached in memory).
- **Session download counter** in the toolbar shows `↓ X.X MB` in real time.
- **50 MB warning** — a one-time dialog appears if a session exceeds 50 MB, noting the S3 free tier and per-GB cost.

See [`docs/cost-analysis.md`](docs/cost-analysis.md) for a full cost breakdown.

---

## S3 folder structure

```
s3://your-bucket/
├── snapshots/20260423_143201_a1b2c3d4.jpg      ← on-demand snapshots (UTC timestamp)
└── wildlife/2026-04-23/14/                      ← motion clips partitioned by UTC date/hour
        20260423_143201_a1b2c3d4.mp4
```

## Health check

A lightweight HTTP server runs on port 8080 (configurable).

```bash
# Is the process alive?
curl localhost:8080/health

# Full status
curl localhost:8080/status
```

`/status` returns:

```json
{
  "uptime_s": 3600,
  "camera_ok": true,
  "clips_this_hour": 12,
  "clips_per_hour_cap": 30,
  "last_clip_utc": "2026-04-23T14:32:01Z",
  "upload_queue_depth": 0,
  "last_upload_utc": "2026-04-23T14:32:05Z"
}
```

## Raspberry Pi — running as a service

Copy the systemd unit, then enable it:

```bash
# Update WorkingDirectory and EnvironmentFile paths in the unit file first
sudo cp nature-watch.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now nature-watch

# View logs
journalctl -u nature-watch -f
```

The service starts on boot and restarts automatically on failure.

## Offline behaviour

When the Pi loses internet connectivity, clips are saved to `buffer/` and their paths are written to `buffer/queue.json`. A background thread retries the upload every 30 seconds. On reconnect, pending clips are uploaded in order and removed from disk.

## Rate limiting

When the 30 clips/hour cap is reached, further motion events are dropped and a warning is logged. The cap resets automatically at the start of each new hour.

With `clip_duration_s: 20` and `clip_cooldown_s: 10`, each clip cycle takes 30 seconds, giving a hardware maximum of 120 clips/hour. The cap of 30 means sustained motion activity fills the quota in 15 minutes.

> **TODO:** Wire up an AWS SNS alert when the cap is hit. The stub is in `src/rate_limiter.py`; set `sns_topic_arn` in `config.yaml` once you have a topic ARN.

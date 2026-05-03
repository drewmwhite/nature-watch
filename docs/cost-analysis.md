# S3 Cost Analysis — Nature Watch

## Overview

Nature Watch stores motion-triggered video clips and manual snapshots in AWS S3.
This document breaks down every cost component: what drives it, how to calculate it,
and what the viewer tool adds on top.

All prices are **us-east-1** as of 2026. Check the
[S3 pricing page](https://aws.amazon.com/s3/pricing/) for current rates.

---

## AWS S3 Pricing Reference (us-east-1)

| Component | Rate |
| --- | --- |
| Storage | $0.023 per GB-month (first 50 TB) |
| PUT / COPY / POST requests | $0.005 per 1,000 requests |
| GET / SELECT requests | $0.0004 per 1,000 requests |
| LIST requests | $0.005 per 1,000 requests |
| Data transfer **in** to S3 | Free |
| Data transfer **out** to internet | First 100 GB/month free, then $0.09/GB |

---

## What the Recorder Uploads

Every time motion is detected and the cooldown has expired, the recorder writes one
20-second MP4 clip to the local buffer and queues it for upload to S3.

### Key config values (config.yaml)

| Setting | Value | Effect |
| --- | --- | --- |
| `clip_duration_s` | 20 | Each clip is 20 seconds long |
| `clip_cooldown_s` | 10 | Minimum 10 seconds between clips ending and next trigger |
| `clips_per_hour_cap` | 30 | Hard ceiling: at most 30 clips uploaded per hour |

### Theoretical maximum clip rate

```
clip cycle = clip_duration_s + clip_cooldown_s = 20 + 10 = 30 seconds
max clips/hour = 3600 / 30 = 120 clips/hour (hardware limit)
enforced cap   = 30 clips/hour
```

The cap of 30 means motion can trigger continuously for 15 minutes before the
hour limit is hit. After that, clips are dropped until the next hour.

### Clip file size

Based on observed files in `buffer/`:

| File | Size |
| --- | --- |
| `20260424_023023_a0e24b4a.mp4` | 683 KB |
| `20260501_033411_8c3e1b4b.mp4` | 344 KB |

Conservative average: **~500 KB per clip** (varies with scene complexity and lighting).

### Snapshot file size

Snapshots are taken on-demand via `nature-watch-snap`. They are JPEG at quality 90
from the configured webcam. At typical webcam resolution (640×480): **~150 KB each**.

---

## S3 API Call Breakdown

### Uploads (recorder + snap)

Each clip or snapshot upload is **one PUT request**.

| Scenario | Clips/hour | Hours/day | Clips/month | PUT requests/month | PUT cost/month |
| --- | --- | --- | --- | --- | --- |
| Light | 5 | 8 | 1,200 | ~1,200 | $0.006 |
| Moderate | 15 | 24 | 10,800 | ~10,800 | $0.054 |
| Maxed out | 30 | 24 | 21,600 | ~21,600 | $0.108 |

Snapshots add a negligible number of PUTs (assume ~10–30/day = 300–900/month = < $0.005).

**PUT requests are not a meaningful cost driver at this scale.**

### Viewer navigation (LIST calls)

The viewer uses `list_objects_v2` with a `Delimiter` to navigate the S3 partition
tree lazily. Results are cached in memory — navigating back to a previously visited
level costs nothing.

| Action | S3 calls made | Subsequent visits |
| --- | --- | --- |
| Open "Wildlife Clips" | 1 LIST (gets date folders) | 0 — cached |
| Click into a date | 1 LIST (gets hour folders) | 0 — cached |
| Click into an hour | 1 LIST (gets clip keys) | 0 — cached |
| Open "Snapshots" | 1 LIST (gets all snapshot keys) | 0 — cached |
| Click into a snapshot date | 0 (grouped client-side) | 0 — cached |

A typical browsing session navigating through several dates and hours makes
**fewer than 10 LIST calls total**. At $0.005 per 1,000, this is effectively $0.00.

---

## Storage Cost

Storage is billed per GB-month — the average number of GB stored across the month.

### Clips

```
storage_per_month_GB = clips_per_hour × avg_clip_size_KB × 24 × 30 / (1024 × 1024)
```

| Scenario | Clips/hour | GB stored/month | Storage cost/month |
| --- | --- | --- | --- |
| Light (5/hr, 8 hrs/day) | 5 avg × 8h × 30d | ~6 GB | $0.14 |
| Moderate (15/hr, 24/7) | 15 avg × 24h × 30d | ~16 GB | $0.37 |
| Maxed out (30/hr, 24/7) | 30 × 24h × 30d | ~32 GB | $0.74 |

Storage accumulates permanently unless you set up an S3 Lifecycle rule to expire
old clips. Without a lifecycle policy, every month adds to the previous total.

**Example: after 6 months at moderate usage = ~96 GB stored = $2.21/month.**

### Snapshots

At 20 snapshots/day × 150 KB × 30 days = ~90 MB/month. Negligible.

---

## Data Transfer Cost (the one that can surprise you)

Data transfer **in** to S3 is always free. Data transfer **out** (downloading from S3
to a computer outside AWS) is free for the first 100 GB/month, then $0.09/GB.

### What the viewer downloads

The viewer downloads actual file bytes for two purposes:

**1. Thumbnails**
When you enter a leaf partition (e.g., an hour of clips), the viewer downloads
the full file for each thumbnail it renders:

- JPEG snapshot thumbnail: downloads full JPEG (~150 KB)
- MP4 clip thumbnail: downloads full MP4 (~500 KB) to extract the first frame

Only cells **visible in the current viewport** are downloaded (typically 1–2 rows,
~5–10 items). Scrolling loads the next row. Files already loaded are cached in memory
for the session — scrolling back is free.

**2. Full-size view and video playback**
- Opening a full-size photo: 1 × ~150 KB
- Playing a video clip: 1 × ~500 KB (downloads to temp file, opens in system player)

### Transfer cost per action

| Action | Data transferred |
| --- | --- |
| Navigate to a partition (date/hour) | 0 bytes |
| Enter an hour grid (first viewport) | ~5–10 × 500 KB = 2.5–5 MB |
| Scroll one row down | ~5 × 500 KB = 2.5 MB |
| Open a full-size photo | ~150 KB |
| Play a video clip | ~500 KB |
| Browse an entire month at cap (21,600 clips) without viewport loading | ~10.8 GB |
| Browse an entire month at cap (21,600 clips) **with** viewport loading | Only what you scroll to |

### Monthly transfer budget

The first 100 GB/month out is free. At ~500 KB per clip thumbnail:

```
clips to exhaust free tier = 100 GB / 0.5 MB = ~200,000 clip thumbnails viewed
```

At moderate usage (10,800 clips/month stored), you would need to scroll through
every clip thumbnail **~18 times in a month** to approach the free tier limit.
Normal usage will never come close.

The in-app download counter (shown in the toolbar as `↓ X.X MB`) tracks all bytes
transferred in the current session. A warning dialog appears at 50 MB.

---

## Total Monthly Cost Scenarios

### Scenario 1 — Light
- Pi active 8 hours/day, averaging 5 clips/hour, ~5 snapshots/day
- Viewer used ~2 sessions/week, browsing ~50 clips per session

| Component | Monthly cost |
| --- | --- |
| Storage (~6 GB) | $0.14 |
| PUT requests (~1,350) | $0.007 |
| GET requests (viewer, ~400 clips × 2 sessions × 4 weeks) | $0.001 |
| Data transfer out (~1.6 GB, well under free tier) | $0.00 |
| **Total** | **~$0.15/month** |

### Scenario 2 — Moderate
- Pi active 24/7, averaging 15 clips/hour, ~20 snapshots/day
- Viewer used ~4 sessions/week, browsing ~100 clips per session

| Component | Monthly cost |
| --- | --- |
| Storage (~16 GB) | $0.37 |
| PUT requests (~11,400) | $0.057 |
| GET requests (~1,600 clips × 4 sessions × 4 weeks) | $0.001 |
| Data transfer out (~12.8 GB, under free tier) | $0.00 |
| **Total** | **~$0.43/month** |

### Scenario 3 — Maxed out
- Pi active 24/7, always hitting 30/hr cap, viewer used heavily

| Component | Monthly cost |
| --- | --- |
| Storage (~32 GB) | $0.74 |
| PUT requests (~21,600) | $0.108 |
| GET requests (~3,000 clips × daily browsing) | $0.001 |
| Data transfer out (~23 GB, under free tier) | $0.00 |
| **Total** | **~$0.85/month** |

### Scenario 4 — Storage after 12 months (moderate, no lifecycle policy)
- Storage grows each month; all other costs stay roughly the same

| Month | Cumulative storage | Storage cost that month |
| --- | --- | --- |
| 1 | 16 GB | $0.37 |
| 3 | 48 GB | $1.10 |
| 6 | 96 GB | $2.21 |
| 12 | 192 GB | $4.42 |

**Recommendation: set an S3 Lifecycle rule to expire clips after 90 days.**
This caps storage at ~3 months of accumulation (~48 GB at moderate = $1.10/month)
and keeps costs flat indefinitely.

---

## Cost Safeguards Built Into the Viewer

| Safeguard | How it works |
| --- | --- |
| Lazy partition navigation | LIST calls only happen when you click into a new level; results are cached so back-navigation is free |
| Viewport-aware thumbnail loading | Only the ~10 thumbnails visible in the current scroll window are downloaded; scrolling loads the next batch |
| In-memory thumbnail cache | A thumbnail downloaded once is never re-downloaded during the same session |
| Session download counter | Toolbar shows `↓ X.X MB` updated in real time after every thumbnail or video download |
| 50 MB session warning | One-time dialog when the session crosses 50 MB, noting the free tier and per-GB cost |
| Bounded connection pool | Max 6 concurrent S3 connections (well under boto3's default pool of 10) to prevent connection pool exhaustion warnings |

---

## Recommendations

1. **Set an S3 Lifecycle expiry rule** — expire clips after 60–90 days to keep storage costs flat. Snapshots can be retained longer since they are tiny.

2. **Monitor the download counter** — if you regularly see sessions pushing 20–30 MB, consider whether you need to scroll through that many clips or if the motion threshold (`motion_threshold` in config.yaml) is too sensitive and generating unnecessary clips.

3. **Tune `clips_per_hour_cap`** — currently set to 30. If your camera sees sustained motion (wind, busy street), lower it further. Each clip saved is ~500 KB of permanent storage.

4. **Tune `motion_threshold`** — increasing this value (default 500 px²) reduces false positives from minor movements like shadows or insects, directly reducing clip count and storage.

# Dog Monitor — Computer Vision Classification System
### Technical Specification

---

## 1. Overview

This document describes the design and implementation plan for adding AI-powered video classification to an existing Tkinter-based dog monitoring application. The system will analyze video clips fetched from S3 and classify dog activities, locations in frame, and behavioral alerts using YOLOv8 pose detection combined with a lightweight custom classifier.

**Scope of this spec:**
- YOLOv8 integration for pose and bounding box extraction
- Three classification heads: Activity, Location, Alert
- Tkinter UI extensions for labeling and inference
- Local model training pipeline
- S3 integration with existing folder structure
- GPU detection and fallback

---

## 2. System Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                      Tkinter Application                    │
│                                                             │
│  ┌──────────────┐   ┌─────────────────┐  ┌──────────────┐  │
│  │  S3 Browser  │   │  Labeling Panel │  │ Results Panel│  │
│  │  (existing)  │──▶│  (new)          │  │  (new)       │  │
│  └──────────────┘   └────────┬────────┘  └──────┬───────┘  │
│                              │                   │          │
└──────────────────────────────┼───────────────────┼──────────┘
                               │                   │
               ┌───────────────▼───────────────────▼───────┐
               │           Inference Engine                  │
               │                                            │
               │  ┌─────────────┐    ┌──────────────────┐  │
               │  │  YOLOv8     │───▶│ Feature Extractor │  │
               │  │  (pose)     │    │ (keypoints + bbox)│  │
               │  └─────────────┘    └────────┬─────────┘  │
               │                              │             │
               │              ┌───────────────▼──────────┐  │
               │              │    Classifier (3 heads)   │  │
               │              │  - Activity               │  │
               │              │  - Location               │  │
               │              │  - Alert                  │  │
               │              └──────────────────────────┘  │
               └────────────────────────────────────────────┘
                               │
               ┌───────────────▼────────────────────────────┐
               │           Data Layer                        │
               │  S3 (date folders) ◀──▶ Local cache/labels │
               └────────────────────────────────────────────┘
```

---

## 3. Project Structure

```
dog_monitor/
├── app.py                     # Existing Tkinter entry point
├── classifier/
│   ├── __init__.py
│   ├── inference.py           # Run YOLO + classifier on a clip
│   ├── feature_extractor.py   # Convert YOLO output → feature vectors
│   ├── model.py               # Classifier model definition (PyTorch)
│   ├── train.py               # Training script
│   └── gpu_utils.py           # Device detection helper
├── data/
│   ├── labels/                # JSON label files per clip
│   │   └── YYYY-MM-DD/
│   │       └── clip_name.json
│   ├── features/              # Cached extracted features (optional)
│   └── models/                # Saved .pt classifier weights
│       ├── activity_classifier.pt
│       ├── location_classifier.pt
│       └── alert_classifier.pt
├── ui/
│   ├── labeling_panel.py      # New: label clips in the UI
│   └── results_panel.py       # New: display inference results
├── s3/
│   └── client.py              # Existing S3 fetch/upload logic
└── requirements.txt
```

---

## 4. Dependencies

```txt
# requirements.txt
ultralytics>=8.2.0       # YOLOv8 — YOLO model + pose
torch>=2.1.0             # PyTorch — classifier training + inference
torchvision>=0.16.0
opencv-python>=4.9.0     # Frame extraction from video clips
boto3>=1.34.0            # S3 (existing)
numpy>=1.26.0
Pillow>=10.0.0
tqdm>=4.66.0             # Training progress bars
```

Install:
```bash
pip install -r requirements.txt
```

> **GPU Note:** PyTorch will automatically use CUDA if an NVIDIA GPU is available. On Apple Silicon (M1/M2/M3), use `torch` with MPS backend. If neither is present, inference runs on CPU — perfectly usable for manual clip-by-clip analysis, just slower. See Section 9 for device detection.

---

## 5. Classification Design

### 5.1 Three Classification Heads

Each head is a separate lightweight classifier trained on features extracted from YOLO output. Keeping them separate lets you retrain one without affecting the others.

| Head | Labels (examples) | Input |
|---|---|---|
| **Activity** | sleeping, eating, playing, sitting, walking | Pose keypoints + motion delta |
| **Location** | on couch, at door, in crate, on floor, out of frame | Bounding box position + size |
| **Alert** | normal, barking, distress, unusual behavior | Pose keypoints + motion magnitude |

### 5.2 Label Schema

Each labeled clip produces a JSON file saved locally under `data/labels/YYYY-MM-DD/`:

```json
{
  "clip_key": "2024-06-01/morning_clip_001.mp4",
  "labeled_at": "2024-06-10T14:23:00",
  "activity": "sleeping",
  "location": "on couch",
  "alert": "normal",
  "notes": ""
}
```

### 5.3 Suggested Starter Label Sets

**Activities:** `sleeping`, `eating`, `playing`, `sitting`, `walking`, `running`, `unknown`

**Locations:** `on_couch`, `at_door`, `in_crate`, `on_floor`, `out_of_frame`, `unknown`

**Alerts:** `normal`, `barking`, `distress`, `unknown`

Start small — 3–4 classes per head — and expand once you have enough data.

---

## 6. YOLOv8 Pose Extraction

### 6.1 Why Pose Over Raw Frames?

Using YOLOv8's pose model gives you 17 keypoints (nose, shoulders, hips, paws, etc.) per frame rather than training on raw pixels. This dramatically reduces the amount of labeled data needed and makes the classifier robust to lighting and camera angle changes.

### 6.2 Feature Extractor

```python
# classifier/feature_extractor.py

import cv2
import numpy as np
from ultralytics import YOLO

YOLO_MODEL = "yolov8n-pose.pt"  # nano = fastest, swap to yolov8m-pose.pt for accuracy

def extract_features_from_clip(video_path: str, sample_every_n_frames: int = 5) -> dict | None:
    """
    Run YOLOv8 pose on a video clip and return aggregated feature vectors.
    Returns None if no dog is detected.
    """
    model = YOLO(YOLO_MODEL)
    cap = cv2.VideoCapture(video_path)

    all_keypoints = []
    all_bboxes = []
    frame_idx = 0

    while cap.isOpened():
        ret, frame = cap.read()
        if not ret:
            break

        if frame_idx % sample_every_n_frames == 0:
            results = model(frame, verbose=False)
            for r in results:
                if r.keypoints is not None and len(r.keypoints.xy) > 0:
                    kp = r.keypoints.xy[0].cpu().numpy()   # (17, 2)
                    all_keypoints.append(kp.flatten())     # → (34,)
                if r.boxes is not None and len(r.boxes.xyxy) > 0:
                    box = r.boxes.xyxy[0].cpu().numpy()    # (x1, y1, x2, y2)
                    all_bboxes.append(box)

        frame_idx += 1

    cap.release()

    if not all_keypoints:
        return None

    kp_array = np.array(all_keypoints)   # (n_frames, 34)
    bb_array = np.array(all_bboxes)      # (n_frames, 4)

    # Aggregate: mean + std across sampled frames gives activity signal
    kp_mean = kp_array.mean(axis=0)      # (34,)
    kp_std  = kp_array.std(axis=0)       # (34,) — high std = movement

    # Normalize bounding box by frame size
    h, w = frame.shape[:2]
    bb_norm = bb_array / np.array([w, h, w, h])
    bb_mean = bb_norm.mean(axis=0)       # (4,) — center + size → location signal

    return {
        "keypoint_mean": kp_mean,    # used by activity + alert classifiers
        "keypoint_std":  kp_std,     # motion signal
        "bbox_mean":     bb_mean,    # used by location classifier
    }


def build_feature_vector(features: dict, head: str) -> np.ndarray:
    """Select and concatenate features relevant to each classification head."""
    if head == "activity":
        return np.concatenate([features["keypoint_mean"], features["keypoint_std"]])  # (68,)
    elif head == "location":
        return features["bbox_mean"]  # (4,)
    elif head == "alert":
        return np.concatenate([features["keypoint_mean"], features["keypoint_std"]])  # (68,)
    else:
        raise ValueError(f"Unknown head: {head}")
```

---

## 7. Classifier Model

A small MLP (multi-layer perceptron) is used for each head. This is intentionally simple — it trains fast on CPU with relatively little data (50–200 labeled clips per class is enough to start).

```python
# classifier/model.py

import torch
import torch.nn as nn

class DogActivityClassifier(nn.Module):
    def __init__(self, input_dim: int, num_classes: int, hidden_dim: int = 128):
        super().__init__()
        self.net = nn.Sequential(
            nn.Linear(input_dim, hidden_dim),
            nn.ReLU(),
            nn.Dropout(0.3),
            nn.Linear(hidden_dim, hidden_dim // 2),
            nn.ReLU(),
            nn.Linear(hidden_dim // 2, num_classes)
        )

    def forward(self, x):
        return self.net(x)


# Input dimensions per head:
# activity: 68  (34 kp_mean + 34 kp_std)
# location:  4  (normalized bbox)
# alert:    68  (same as activity)

HEAD_CONFIGS = {
    "activity": {"input_dim": 68},
    "location": {"input_dim": 4},
    "alert":    {"input_dim": 68},
}
```

---

## 8. Training Pipeline

```python
# classifier/train.py

import json, os
import numpy as np
import torch
import torch.nn as nn
from torch.utils.data import DataLoader, TensorDataset, random_split
from classifier.model import DogActivityClassifier, HEAD_CONFIGS
from classifier.feature_extractor import extract_features_from_clip, build_feature_vector

LABEL_DIR  = "data/labels"
MODEL_DIR  = "data/models"
CLIP_CACHE = {}   # optional: cache extracted features to avoid re-running YOLO


def load_labels(head: str) -> tuple[list, list, list]:
    """Walk label JSONs and return (clip_paths, labels, class_names)."""
    clips, labels, class_set = [], [], set()

    for date_folder in os.listdir(LABEL_DIR):
        folder_path = os.path.join(LABEL_DIR, date_folder)
        for fname in os.listdir(folder_path):
            if not fname.endswith(".json"):
                continue
            with open(os.path.join(folder_path, fname)) as f:
                data = json.load(f)
            label = data.get(head)
            if label and label != "unknown":
                clips.append(data["clip_key"])
                labels.append(label)
                class_set.add(label)

    class_names = sorted(class_set)
    label_indices = [class_names.index(l) for l in labels]
    return clips, label_indices, class_names


def train_head(head: str, local_clip_dir: str, epochs: int = 30):
    """Full training run for one classification head."""
    device = get_device()
    clips, label_indices, class_names = load_labels(head)
    print(f"[{head}] {len(clips)} labeled clips | classes: {class_names}")

    # Extract features
    X, y = [], []
    for clip_key, label_idx in zip(clips, label_indices):
        local_path = os.path.join(local_clip_dir, clip_key)
        if not os.path.exists(local_path):
            print(f"  Skipping missing clip: {clip_key}")
            continue
        features = extract_features_from_clip(local_path)
        if features is None:
            print(f"  No dog detected in: {clip_key}")
            continue
        X.append(build_feature_vector(features, head))
        y.append(label_idx)

    X = torch.tensor(np.array(X), dtype=torch.float32)
    y = torch.tensor(y, dtype=torch.long)

    dataset = TensorDataset(X, y)
    train_size = int(0.8 * len(dataset))
    val_size = len(dataset) - train_size
    train_ds, val_ds = random_split(dataset, [train_size, val_size])

    train_loader = DataLoader(train_ds, batch_size=16, shuffle=True)
    val_loader   = DataLoader(val_ds,   batch_size=16)

    input_dim   = HEAD_CONFIGS[head]["input_dim"]
    num_classes = len(class_names)
    model = DogActivityClassifier(input_dim, num_classes).to(device)
    optimizer = torch.optim.Adam(model.parameters(), lr=1e-3)
    criterion = nn.CrossEntropyLoss()

    for epoch in range(epochs):
        model.train()
        for xb, yb in train_loader:
            xb, yb = xb.to(device), yb.to(device)
            loss = criterion(model(xb), yb)
            optimizer.zero_grad()
            loss.backward()
            optimizer.step()

        # Validation
        model.eval()
        correct = total = 0
        with torch.no_grad():
            for xb, yb in val_loader:
                preds = model(xb.to(device)).argmax(dim=1)
                correct += (preds.cpu() == yb).sum().item()
                total += len(yb)
        print(f"  Epoch {epoch+1:02d}/{epochs} — val acc: {correct/total:.1%}")

    # Save model + class names
    os.makedirs(MODEL_DIR, exist_ok=True)
    save_path = os.path.join(MODEL_DIR, f"{head}_classifier.pt")
    torch.save({"model_state": model.state_dict(),
                "class_names": class_names,
                "input_dim":   input_dim}, save_path)
    print(f"[{head}] Saved to {save_path}")
```

Run training from the terminal:
```bash
python -m classifier.train
```

---

## 9. GPU Detection

```python
# classifier/gpu_utils.py

import torch

def get_device() -> torch.device:
    if torch.cuda.is_available():
        print("Using NVIDIA GPU (CUDA)")
        return torch.device("cuda")
    elif hasattr(torch.backends, "mps") and torch.backends.mps.is_available():
        print("Using Apple Silicon GPU (MPS)")
        return torch.device("mps")
    else:
        print("No GPU detected — running on CPU")
        return torch.device("cpu")
```

---

## 10. Inference

```python
# classifier/inference.py

import torch
import numpy as np
from classifier.model import DogActivityClassifier
from classifier.feature_extractor import extract_features_from_clip, build_feature_vector
from classifier.gpu_utils import get_device

MODEL_DIR = "data/models"
_loaded_models = {}   # cache loaded models in memory

def load_model(head: str):
    if head in _loaded_models:
        return _loaded_models[head]
    path = f"{MODEL_DIR}/{head}_classifier.pt"
    checkpoint = torch.load(path, map_location="cpu")
    model = DogActivityClassifier(checkpoint["input_dim"], len(checkpoint["class_names"]))
    model.load_state_dict(checkpoint["model_state"])
    model.eval()
    _loaded_models[head] = (model, checkpoint["class_names"])
    return model, checkpoint["class_names"]


def run_inference(video_path: str) -> dict:
    """
    Run all three classifiers on a local video clip.
    Returns a dict with predictions and confidence scores.
    """
    features = extract_features_from_clip(video_path)

    if features is None:
        return {"error": "No dog detected in this clip"}

    results = {}
    for head in ["activity", "location", "alert"]:
        try:
            model, class_names = load_model(head)
            fv = build_feature_vector(features, head)
            x  = torch.tensor(fv, dtype=torch.float32).unsqueeze(0)
            with torch.no_grad():
                logits = model(x)
                probs  = torch.softmax(logits, dim=1)[0]
            top_idx   = probs.argmax().item()
            results[head] = {
                "label":      class_names[top_idx],
                "confidence": round(probs[top_idx].item(), 3),
                "all_probs":  {c: round(p.item(), 3)
                               for c, p in zip(class_names, probs)}
            }
        except FileNotFoundError:
            results[head] = {"label": "not trained", "confidence": 0.0}

    return results
```

---

## 11. Tkinter UI Extensions

### 11.1 Labeling Panel

Add a new panel that appears when a clip is selected in the existing S3 browser. It shows the clip (via OpenCV frame preview), dropdowns for each label head, and a save button.

```python
# ui/labeling_panel.py

import tkinter as tk
from tkinter import ttk
import json, os
from datetime import datetime

ACTIVITY_OPTIONS = ["sleeping", "eating", "playing", "sitting", "walking", "running", "unknown"]
LOCATION_OPTIONS = ["on_couch", "at_door", "in_crate", "on_floor", "out_of_frame", "unknown"]
ALERT_OPTIONS    = ["normal", "barking", "distress", "unknown"]

class LabelingPanel(tk.Frame):
    def __init__(self, parent, on_label_saved=None, **kwargs):
        super().__init__(parent, **kwargs)
        self.on_label_saved = on_label_saved
        self.current_clip_key = None
        self._build_ui()

    def _build_ui(self):
        tk.Label(self, text="Label This Clip", font=("Arial", 13, "bold")).pack(pady=(8, 4))

        for label_text, options, attr in [
            ("Activity",  ACTIVITY_OPTIONS, "activity_var"),
            ("Location",  LOCATION_OPTIONS, "location_var"),
            ("Alert",     ALERT_OPTIONS,    "alert_var"),
        ]:
            row = tk.Frame(self)
            row.pack(fill="x", padx=12, pady=3)
            tk.Label(row, text=label_text, width=10, anchor="w").pack(side="left")
            var = tk.StringVar(value=options[-1])  # default to "unknown"
            setattr(self, attr, var)
            ttk.Combobox(row, textvariable=var, values=options,
                         state="readonly", width=20).pack(side="left")

        tk.Label(self, text="Notes (optional):").pack(anchor="w", padx=12)
        self.notes_entry = tk.Entry(self, width=35)
        self.notes_entry.pack(padx=12, pady=(0, 8))

        tk.Button(self, text="Save Label", command=self._save_label,
                  bg="#2d7d46", fg="white", font=("Arial", 11, "bold")).pack(pady=4)

        self.status_label = tk.Label(self, text="", fg="green")
        self.status_label.pack()

    def set_clip(self, clip_key: str):
        """Call this when the user selects a clip in the S3 browser."""
        self.current_clip_key = clip_key
        self.status_label.config(text="")
        self._load_existing_label(clip_key)

    def _load_existing_label(self, clip_key: str):
        label_path = self._label_path(clip_key)
        if os.path.exists(label_path):
            with open(label_path) as f:
                data = json.load(f)
            self.activity_var.set(data.get("activity", "unknown"))
            self.location_var.set(data.get("location", "unknown"))
            self.alert_var.set(data.get("alert", "unknown"))
            self.notes_entry.delete(0, "end")
            self.notes_entry.insert(0, data.get("notes", ""))

    def _save_label(self):
        if not self.current_clip_key:
            return
        label_data = {
            "clip_key":   self.current_clip_key,
            "labeled_at": datetime.now().isoformat(),
            "activity":   self.activity_var.get(),
            "location":   self.location_var.get(),
            "alert":      self.alert_var.get(),
            "notes":      self.notes_entry.get(),
        }
        path = self._label_path(self.current_clip_key)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            json.dump(label_data, f, indent=2)

        self.status_label.config(text="✓ Label saved")
        if self.on_label_saved:
            self.on_label_saved(label_data)

    def _label_path(self, clip_key: str) -> str:
        # clip_key format: "YYYY-MM-DD/clip_name.mp4"
        date_folder = clip_key.split("/")[0]
        clip_name   = os.path.splitext(os.path.basename(clip_key))[0]
        return os.path.join("data", "labels", date_folder, f"{clip_name}.json")
```

### 11.2 Results Panel

Displays inference output after the user clicks "Analyze Clip":

```python
# ui/results_panel.py

import tkinter as tk
from classifier.inference import run_inference
import threading

class ResultsPanel(tk.Frame):
    def __init__(self, parent, **kwargs):
        super().__init__(parent, **kwargs)
        self._build_ui()

    def _build_ui(self):
        tk.Label(self, text="Analysis Results", font=("Arial", 13, "bold")).pack(pady=(8, 4))
        self.result_frame = tk.Frame(self)
        self.result_frame.pack(fill="both", expand=True, padx=12)
        self.status = tk.Label(self, text="Select a clip and click Analyze")
        self.status.pack(pady=6)

        tk.Button(self, text="Analyze Clip", command=self._trigger_analysis,
                  bg="#1a6fa8", fg="white", font=("Arial", 11, "bold")).pack(pady=4)

        self.current_clip_path = None

    def set_clip_path(self, local_path: str):
        self.current_clip_path = local_path

    def _trigger_analysis(self):
        if not self.current_clip_path:
            self.status.config(text="No clip selected")
            return
        self.status.config(text="Analyzing...")
        # Run in background thread so UI doesn't freeze
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        results = run_inference(self.current_clip_path)
        self.after(0, lambda: self._display(results))

    def _display(self, results: dict):
        for widget in self.result_frame.winfo_children():
            widget.destroy()

        if "error" in results:
            tk.Label(self.result_frame, text=results["error"], fg="red").pack()
            self.status.config(text="Done")
            return

        COLORS = {"activity": "#2d5fa8", "location": "#7a3fa8", "alert": "#a83f3f"}

        for head, data in results.items():
            row = tk.Frame(self.result_frame, bd=1, relief="solid", padx=8, pady=6)
            row.pack(fill="x", pady=3)
            color = COLORS.get(head, "black")
            tk.Label(row, text=head.upper(), fg=color,
                     font=("Arial", 10, "bold"), width=10, anchor="w").grid(row=0, column=0)
            tk.Label(row, text=data["label"],
                     font=("Arial", 11), width=16, anchor="w").grid(row=0, column=1)
            tk.Label(row, text=f"{data['confidence']:.0%}",
                     fg="gray").grid(row=0, column=2)

        self.status.config(text="Analysis complete")
```

### 11.3 Wiring Into app.py

In your existing `app.py`, add the new panels to your layout and connect them to clip selection:

```python
# In app.py — additions to existing code

from ui.labeling_panel import LabelingPanel
from ui.results_panel import ResultsPanel

# After your existing S3 browser widget is set up:
labeling_panel = LabelingPanel(root)
labeling_panel.pack(side="right", fill="y", padx=8)

results_panel = ResultsPanel(root)
results_panel.pack(side="right", fill="y", padx=8)

# Hook into your existing clip selection callback.
# Replace `on_clip_selected` with whatever your current callback is named.
def on_clip_selected(clip_key, local_path):
    labeling_panel.set_clip(clip_key)
    results_panel.set_clip_path(local_path)
```

---

## 12. S3 Integration Notes

Your existing S3 client already fetches from `YYYY-MM-DD/` folders. The classifier needs clips downloaded locally to run YOLO on them. The recommended approach:

1. User selects a clip in the existing S3 browser (already implemented)
2. Clip is downloaded to a local temp cache folder (e.g., `data/clip_cache/`)
3. The local path is passed to `ResultsPanel.set_clip_path()`
4. Labels are stored locally in `data/labels/YYYY-MM-DD/` — no S3 write needed unless you choose to sync them later

**Optional:** Add a "Sync Labels to S3" button that uploads the `data/labels/` folder to a `labels/` prefix in your bucket. This keeps labels backed up and makes them accessible if you later want to train on a different machine.

---

## 13. Recommended Build Order

Work through these phases in order. Each phase is independently useful before moving to the next.

| Phase | What You Build | Value Delivered |
|---|---|---|
| **1 — Setup** | Install deps, verify YOLO runs on a sample clip | Confirms environment works |
| **2 — Labeling UI** | Add `LabelingPanel` to existing app | Start collecting training data immediately |
| **3 — Feature Extraction** | Implement `feature_extractor.py`, test on labeled clips | Validate YOLO detects your dog reliably |
| **4 — Train First Head** | Train just the activity classifier | First working model |
| **5 — Results UI** | Add `ResultsPanel`, wire up inference | End-to-end working system |
| **6 — All Three Heads** | Train location + alert classifiers | Full classification suite |
| **7 — Iteration** | Add more labels, retrain, improve accuracy | Ongoing improvement |

---

## 14. Data Requirements (Practical Estimates)

| Head | Minimum clips per class | Recommended |
|---|---|---|
| Activity | 30 | 80–150 |
| Location | 20 | 50–100 |
| Alert | 40 (alerts are rarer) | 100+ |

Start labeling while the dog is monitored day-to-day. You don't need to label everything — even a few weeks of casual labeling during clip review will build a solid training set.

---

## 15. Accuracy Considerations

- **YOLO model size:** `yolov8n-pose.pt` (nano) is fastest. If detection is unreliable, upgrade to `yolov8m-pose.pt` (medium) at the cost of ~3x inference time.
- **Low confidence threshold:** If a prediction has confidence below ~60%, consider surfacing it as "uncertain" in the UI rather than committing to a label.
- **Class imbalance:** Your dog probably sleeps more than it barks. If one class dominates, use `WeightedRandomSampler` in PyTorch to balance training batches.
- **Retraining:** As you add more labels, retrain frequently. The pipeline is fast enough (minutes on CPU for small datasets) to retrain after every 20–30 new labels.
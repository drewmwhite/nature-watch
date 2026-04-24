import os
from pathlib import Path

import yaml


_ENV_PREFIX = "NW_"

_DEFAULTS = {
    "camera_index": 0,
    "motion_threshold": 500,
    "motion_blur_ksize": 21,
    "clip_duration_s": 20,
    "clip_cooldown_s": 10,
    "clips_per_hour_cap": 100,
    "local_buffer_dir": "./buffer",
    "s3_bucket": "",
    "s3_prefix": "wildlife",
    "aws_region": "us-east-1",
    "sns_topic_arn": "",
    "health_port": 8080,
}

_INT_KEYS = {
    "camera_index", "motion_threshold", "motion_blur_ksize",
    "clip_duration_s", "clip_cooldown_s", "clips_per_hour_cap", "health_port",
}


def load(path: str | None = None) -> dict:
    cfg = dict(_DEFAULTS)

    config_path = Path(path) if path else Path(__file__).parent.parent / "config.yaml"
    if config_path.exists():
        with config_path.open() as f:
            file_cfg = yaml.safe_load(f) or {}
        cfg.update({k: v for k, v in file_cfg.items() if k in _DEFAULTS})

    for key in _DEFAULTS:
        env_key = _ENV_PREFIX + key.upper()
        env_val = os.environ.get(env_key)
        if env_val is not None:
            cfg[key] = int(env_val) if key in _INT_KEYS else env_val

    if not cfg["s3_bucket"]:
        raise ValueError(
            "s3_bucket is required. Set it in config.yaml or via NW_S3_BUCKET env var."
        )

    return cfg

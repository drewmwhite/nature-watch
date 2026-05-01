import io
import json
import logging
import re
import subprocess
import tempfile
import threading
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional
import tkinter as tk
from tkinter import ttk, messagebox, simpledialog

import boto3
from botocore.exceptions import BotoCoreError, ClientError
import cv2
from PIL import Image, ImageDraw, ImageTk

from . import config as cfg_module

log = logging.getLogger(__name__)

THUMB_W = 150
THUMB_H = 120
THUMB_PAD = 6
COLS = 5
CARD_COLS = 4
META_FILE = "viewer_meta.json"
_TS_RE = re.compile(r"^(\d{8}_\d{6})")
_THUMB_WORKERS = 6  # keep well under boto3's default pool of 10


def _parse_ts(key: str) -> datetime:
    basename = key.rsplit("/", 1)[-1]
    m = _TS_RE.match(basename)
    if not m:
        return datetime.min
    return datetime.strptime(m.group(1), "%Y%m%d_%H%M%S")


def _grey_placeholder() -> Image.Image:
    img = Image.new("RGB", (THUMB_W, THUMB_H), (70, 70, 70))
    draw = ImageDraw.Draw(img)
    draw.text((THUMB_W // 2 - 4, THUMB_H // 2 - 6), "?", fill=(140, 140, 140))
    return img


class MetaStore:
    def __init__(self, path: Path) -> None:
        self._path = path
        self.favorites: set = set()
        self.albums: dict = {}
        if path.exists():
            self._load()

    def _load(self) -> None:
        try:
            data = json.loads(self._path.read_text())
            self.favorites = set(data.get("favorites", []))
            self.albums = {k: list(v) for k, v in data.get("albums", {}).items()}
        except Exception:
            log.warning("Could not load %s, starting fresh", self._path)

    def save(self) -> None:
        tmp = self._path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps({
            "favorites": sorted(self.favorites),
            "albums": self.albums,
        }, indent=2))
        tmp.rename(self._path)

    def add_favorite(self, key: str) -> None:
        self.favorites.add(key)

    def remove_favorite(self, key: str) -> None:
        self.favorites.discard(key)

    def is_favorite(self, key: str) -> bool:
        return key in self.favorites

    def add_to_album(self, album: str, key: str) -> None:
        lst = self.albums.setdefault(album, [])
        if key not in lst:
            lst.append(key)

    def remove_from_album(self, album: str, key: str) -> None:
        if album in self.albums:
            try:
                self.albums[album].remove(key)
            except ValueError:
                pass

    def list_albums(self) -> list:
        return list(self.albums.keys())

    def keys_in_album(self, album: str) -> list:
        return list(self.albums.get(album, []))


class S3Loader:
    def __init__(self, bucket: str, prefix_video: str, region: str) -> None:
        self._s3 = boto3.client("s3", region_name=region)
        self._bucket = bucket
        self.prefix_video = prefix_video

    def list_prefixes(self, prefix: str) -> list:
        """One (or a few) S3 calls: returns immediate virtual sub-folders under prefix."""
        prefixes = []
        paginator = self._s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self._bucket, Prefix=prefix, Delimiter="/"):
            prefixes.extend(cp["Prefix"] for cp in page.get("CommonPrefixes", []))
        return prefixes

    def list_keys(self, prefix: str) -> list:
        """Paginated S3 calls: returns all object keys directly under prefix."""
        keys = []
        paginator = self._s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self._bucket, Prefix=prefix):
            keys.extend(obj["Key"] for obj in page.get("Contents", []))
        return sorted(keys, key=_parse_ts, reverse=True)

    def download_bytes(self, key: str) -> bytes:
        return self._s3.get_object(Bucket=self._bucket, Key=key)["Body"].read()

    def download_to_tempfile(self, key: str) -> str:
        data = self.download_bytes(key)
        tmp = tempfile.NamedTemporaryFile(suffix=".mp4", delete=False)
        tmp.write(data)
        tmp.close()
        return tmp.name


class ThumbnailCache:
    def __init__(self, loader: S3Loader) -> None:
        self._loader = loader
        self._cache: dict = {}
        self._lock = threading.Lock()
        self._pending: set = set()
        self._pool = ThreadPoolExecutor(max_workers=_THUMB_WORKERS)

    def get(self, key: str) -> Optional[Image.Image]:
        with self._lock:
            return self._cache.get(key)

    def request(self, key: str, callback: Callable) -> None:
        with self._lock:
            if key in self._cache or key in self._pending:
                return
            self._pending.add(key)
        self._pool.submit(self._load, key, callback)

    def _load(self, key: str, callback: Callable) -> None:
        try:
            data = self._loader.download_bytes(key)
            if key.endswith(".mp4"):
                img = self._first_frame(data)
            else:
                img = Image.open(io.BytesIO(data)).convert("RGB")
            img.thumbnail((THUMB_W, THUMB_H), Image.LANCZOS)
        except Exception:
            log.warning("Thumbnail failed for %s", key, exc_info=True)
            img = _grey_placeholder()
        with self._lock:
            self._cache[key] = img
            self._pending.discard(key)
        callback(key, img)

    @staticmethod
    def _first_frame(data: bytes) -> Image.Image:
        with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as tmp:
            tmp.write(data)
            tmp_path = tmp.name
        try:
            cap = cv2.VideoCapture(tmp_path)
            ok, frame = cap.read()
            cap.release()
            if not ok or frame is None:
                return _grey_placeholder()
            return Image.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
        finally:
            Path(tmp_path).unlink(missing_ok=True)


class DetailWindow(tk.Toplevel):
    def __init__(self, parent, key: str, all_keys: list,
                 loader: S3Loader, meta: MetaStore) -> None:
        super().__init__(parent)
        self._key = key
        self._all_keys = all_keys
        self._idx = all_keys.index(key)
        self._loader = loader
        self._meta = meta
        self._photo_ref = None
        self.title(key.rsplit("/", 1)[-1])
        self.resizable(True, True)
        self._build_ui()
        self._load_image()

    def _build_ui(self) -> None:
        self._img_label = tk.Label(self, bg="#111111", cursor="watch")
        self._img_label.pack(fill=tk.BOTH, expand=True)

        bar = ttk.Frame(self, padding=6)
        bar.pack(side=tk.BOTTOM, fill=tk.X)

        ttk.Button(bar, text="← Prev", command=self._go_prev).pack(side=tk.LEFT)
        ttk.Button(bar, text="Next →", command=self._go_next).pack(side=tk.LEFT)

        self._fav_btn = ttk.Button(bar, command=self._toggle_favorite)
        self._fav_btn.pack(side=tk.LEFT, padx=8)
        self._update_fav_btn()

        ttk.Button(bar, text="+ Add to Album", command=self._add_to_album).pack(side=tk.LEFT)
        ttk.Button(bar, text="Close", command=self.destroy).pack(side=tk.RIGHT)

        self._caption_var = tk.StringVar()
        ttk.Label(bar, textvariable=self._caption_var).pack(side=tk.LEFT, padx=12)
        self._caption_var.set(self._key.rsplit("/", 1)[-1])

    def _load_image(self) -> None:
        self._img_label.configure(image="", text="Loading…", fg="#888888",
                                   font=("TkDefaultFont", 14))
        key = self._key

        def worker():
            try:
                data = self._loader.download_bytes(key)
                img = Image.open(io.BytesIO(data)).convert("RGB")
                img.thumbnail((900, 700), Image.LANCZOS)
            except Exception as exc:
                self.after(0, messagebox.showerror, "Load Error", str(exc))
                return
            self.after(0, self._display_image, img)

        threading.Thread(target=worker, daemon=True).start()

    def _display_image(self, img: Image.Image) -> None:
        photo = ImageTk.PhotoImage(img)
        self._photo_ref = photo
        self._img_label.configure(image=photo, text="")
        self.geometry(f"{img.width}x{img.height + 52}")

    def _go_prev(self) -> None:
        if self._idx > 0:
            self._idx -= 1
            self._switch_key(self._all_keys[self._idx])

    def _go_next(self) -> None:
        if self._idx < len(self._all_keys) - 1:
            self._idx += 1
            self._switch_key(self._all_keys[self._idx])

    def _switch_key(self, key: str) -> None:
        self._key = key
        self._caption_var.set(key.rsplit("/", 1)[-1])
        self.title(key.rsplit("/", 1)[-1])
        self._update_fav_btn()
        self._load_image()

    def _toggle_favorite(self) -> None:
        if self._meta.is_favorite(self._key):
            self._meta.remove_favorite(self._key)
        else:
            self._meta.add_favorite(self._key)
        self._meta.save()
        self._update_fav_btn()

    def _update_fav_btn(self) -> None:
        star = "★" if self._meta.is_favorite(self._key) else "☆"
        self._fav_btn.configure(text=f"{star} Favorite")

    def _add_to_album(self) -> None:
        existing = self._meta.list_albums()
        initial = existing[0] if existing else ""
        album = simpledialog.askstring(
            "Add to Album", "Album name:", parent=self, initialvalue=initial
        )
        if album and album.strip():
            self._meta.add_to_album(album.strip(), self._key)
            self._meta.save()


class ViewerApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("Nature Watch Viewer")
        self.geometry("900x650")
        self.configure(bg="#1e1e1e")

        cfg = cfg_module.load()
        config_dir = Path(__file__).parent.parent
        self._meta = MetaStore(config_dir / META_FILE)
        self._loader = S3Loader(
            bucket=cfg["s3_bucket"],
            prefix_video=cfg["s3_prefix"],
            region=cfg["aws_region"],
        )
        self._cache = ThumbnailCache(self._loader)
        # All S3 listing results cached here — re-navigating never hits S3 again
        self._listing_cache: dict = {}
        # Keys in the currently-displayed leaf grid (used by DetailWindow for prev/next)
        self._current_keys: list = []
        self._photo_refs: dict = {}
        # Breadcrumb: list of (label, callable) — callable re-renders that level
        self._breadcrumb: list = []

        self._build_ui()
        self._show_home()

    # ── UI construction ──────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        toolbar = ttk.Frame(self, padding=4)
        toolbar.pack(side=tk.TOP, fill=tk.X)

        ttk.Button(toolbar, text="⌂ Home", command=self._show_home).pack(side=tk.LEFT, padx=2)
        ttk.Button(toolbar, text="★ Favorites", command=self._show_favorites).pack(side=tk.LEFT, padx=2)
        ttk.Button(toolbar, text="Albums ▾", command=self._show_albums_menu).pack(side=tk.LEFT, padx=2)

        self._status_var = tk.StringVar(value="")
        ttk.Label(toolbar, textvariable=self._status_var).pack(side=tk.RIGHT, padx=8)

        self._crumb_frame = tk.Frame(self, bg="#1e1e1e", padx=8, pady=3)
        self._crumb_frame.pack(side=tk.TOP, fill=tk.X)

        outer = ttk.Frame(self)
        outer.pack(fill=tk.BOTH, expand=True)

        self._canvas = tk.Canvas(outer, bg="#1e1e1e", highlightthickness=0)
        vbar = ttk.Scrollbar(outer, orient=tk.VERTICAL, command=self._canvas.yview)
        self._canvas.configure(yscrollcommand=vbar.set)
        vbar.pack(side=tk.RIGHT, fill=tk.Y)
        self._canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        self._grid_frame = ttk.Frame(self._canvas)
        self._canvas_window_id = self._canvas.create_window(
            (0, 0), window=self._grid_frame, anchor="nw"
        )

        self._grid_frame.bind("<Configure>", self._on_grid_configure)
        self._canvas.bind("<Configure>", self._on_canvas_configure)
        self._canvas.bind("<MouseWheel>", self._on_mousewheel)
        self._canvas.bind("<Button-4>", self._on_mousewheel)
        self._canvas.bind("<Button-5>", self._on_mousewheel)

    def _update_breadcrumb(self) -> None:
        for w in self._crumb_frame.winfo_children():
            w.destroy()
        for i, (label, fn) in enumerate(self._breadcrumb):
            if i > 0:
                tk.Label(self._crumb_frame, text=" › ", bg="#1e1e1e",
                         fg="#555555").pack(side=tk.LEFT)
            is_last = i == len(self._breadcrumb) - 1
            lbl = tk.Label(
                self._crumb_frame, text=label, bg="#1e1e1e",
                fg="#ffffff" if is_last else "#4fc3f7",
                font=("TkDefaultFont", 9, "bold" if is_last else "normal"),
                cursor="" if is_last else "hand2",
            )
            lbl.pack(side=tk.LEFT)
            if not is_last:
                lbl.bind("<Button-1>", lambda e, f=fn: f())

    def _clear_content(self) -> None:
        for w in self._grid_frame.winfo_children():
            w.destroy()
        self._photo_refs.clear()
        self._canvas.yview_moveto(0)

    def _on_grid_configure(self, event) -> None:
        self._canvas.configure(scrollregion=self._canvas.bbox("all"))

    def _on_canvas_configure(self, event) -> None:
        self._canvas.itemconfig(self._canvas_window_id, width=event.width)

    def _on_mousewheel(self, event) -> None:
        if event.num == 4:
            self._canvas.yview_scroll(-1, "units")
        elif event.num == 5:
            self._canvas.yview_scroll(1, "units")
        else:
            self._canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

    # ── Navigation ───────────────────────────────────────────────────────────

    def _show_home(self) -> None:
        self._breadcrumb = [("Home", self._show_home)]
        self._update_breadcrumb()
        self._status_var.set("")
        self._render_cards([
            ("Snapshots", "", self._show_snapshot_dates),
            ("Wildlife Clips", "", self._show_clip_dates),
        ])

    def _show_snapshot_dates(self) -> None:
        self._breadcrumb = [
            ("Home", self._show_home),
            ("Snapshots", self._show_snapshot_dates),
        ]
        self._update_breadcrumb()
        # List all snapshot keys once; group by date client-side — no extra calls needed
        self._fetch_async(
            "_snapshots_all",
            lambda: self._loader.list_keys("snapshots/"),
            self._on_snapshot_keys_loaded,
        )

    def _on_snapshot_keys_loaded(self, keys: list) -> None:
        by_date: dict = defaultdict(list)
        for k in keys:
            ts = _parse_ts(k)
            date_str = ts.strftime("%Y-%m-%d") if ts != datetime.min else "unknown"
            by_date[date_str].append(k)
        # Cache each date's key list so clicking into a date is free
        for date, date_keys in by_date.items():
            self._listing_cache[f"_snap_{date}"] = date_keys
        dates = sorted(by_date.keys(), reverse=True)
        cards = [
            (date, f"{len(by_date[date])} photo{'s' if len(by_date[date]) != 1 else ''}",
             lambda d=date: self._show_snapshot_date(d))
            for date in dates
        ]
        self._render_cards(cards)

    def _show_snapshot_date(self, date: str) -> None:
        self._breadcrumb = [
            ("Home", self._show_home),
            ("Snapshots", self._show_snapshot_dates),
            (date, lambda d=date: self._show_snapshot_date(d)),
        ]
        self._update_breadcrumb()
        keys = self._listing_cache.get(f"_snap_{date}", [])
        self._current_keys = keys
        self._status_var.set(f"{len(keys)} photo{'s' if len(keys) != 1 else ''}")
        self._render_grid(keys)

    def _show_clip_dates(self) -> None:
        self._breadcrumb = [
            ("Home", self._show_home),
            ("Wildlife Clips", self._show_clip_dates),
        ]
        self._update_breadcrumb()
        prefix = f"{self._loader.prefix_video}/"
        self._fetch_async(
            f"_prefixes_{prefix}",
            lambda: self._loader.list_prefixes(prefix),
            self._on_clip_dates_loaded,
        )

    def _on_clip_dates_loaded(self, prefixes: list) -> None:
        cards = []
        for p in sorted(prefixes, reverse=True):
            date = p.rstrip("/").rsplit("/", 1)[-1]
            cards.append((date, "", lambda p=p, d=date: self._show_clip_hours(p, d)))
        if not cards:
            self._status_var.set("No clips found")
        self._render_cards(cards)

    def _show_clip_hours(self, date_prefix: str, date_label: str) -> None:
        self._breadcrumb = [
            ("Home", self._show_home),
            ("Wildlife Clips", self._show_clip_dates),
            (date_label, lambda p=date_prefix, d=date_label: self._show_clip_hours(p, d)),
        ]
        self._update_breadcrumb()
        self._fetch_async(
            f"_prefixes_{date_prefix}",
            lambda: self._loader.list_prefixes(date_prefix),
            lambda prefixes: self._on_clip_hours_loaded(prefixes, date_prefix, date_label),
        )

    def _on_clip_hours_loaded(self, prefixes: list, date_prefix: str, date_label: str) -> None:
        cards = []
        for p in sorted(prefixes):
            hour = p.rstrip("/").rsplit("/", 1)[-1]
            cards.append((
                f"{hour}:00", "",
                lambda p=p, dp=date_prefix, dl=date_label, h=hour:
                    self._show_clip_hour(p, dp, dl, h),
            ))
        self._render_cards(cards)

    def _show_clip_hour(self, hour_prefix: str, date_prefix: str,
                        date_label: str, hour: str) -> None:
        self._breadcrumb = [
            ("Home", self._show_home),
            ("Wildlife Clips", self._show_clip_dates),
            (date_label, lambda dp=date_prefix, dl=date_label: self._show_clip_hours(dp, dl)),
            (f"{hour}:00", lambda hp=hour_prefix, dp=date_prefix, dl=date_label, h=hour:
                self._show_clip_hour(hp, dp, dl, h)),
        ]
        self._update_breadcrumb()
        self._fetch_async(
            f"_keys_{hour_prefix}",
            lambda: self._loader.list_keys(hour_prefix),
            self._on_leaf_keys_loaded,
        )

    def _on_leaf_keys_loaded(self, keys: list) -> None:
        self._current_keys = keys
        self._status_var.set(f"{len(keys)} clip{'s' if len(keys) != 1 else ''}")
        self._render_grid(keys)

    def _fetch_async(self, cache_key: str, fetch_fn: Callable, on_done: Callable) -> None:
        """Run fetch_fn in a background thread. Returns cached result immediately if available."""
        if cache_key in self._listing_cache:
            on_done(self._listing_cache[cache_key])
            return
        self._clear_content()
        self._status_var.set("Loading…")

        def worker():
            try:
                result = fetch_fn()
            except (BotoCoreError, ClientError) as exc:
                self.after(0, messagebox.showerror, "S3 Error", str(exc))
                self.after(0, self._status_var.set, "Error")
                return
            self._listing_cache[cache_key] = result
            self.after(0, on_done, result)

        threading.Thread(target=worker, daemon=True).start()

    # ── Card grid (partition browser) ────────────────────────────────────────

    def _render_cards(self, cards: list) -> None:
        """Render a grid of clickable partition cards. cards = [(title, subtitle, on_click)]"""
        self._clear_content()
        CARD_W, CARD_H = 190, 80

        for idx, (title, subtitle, on_click) in enumerate(cards):
            row, col = divmod(idx, CARD_COLS)
            card = tk.Frame(self._grid_frame, bg="#2d3748",
                            width=CARD_W, height=CARD_H, cursor="hand2")
            card.grid(row=row, column=col, padx=8, pady=8)
            card.grid_propagate(False)

            rely_title = 0.38 if subtitle else 0.5
            title_lbl = tk.Label(card, text=title, bg="#2d3748", fg="#e2e8f0",
                                 font=("TkDefaultFont", 11, "bold"),
                                 wraplength=CARD_W - 16)
            title_lbl.place(relx=0.5, rely=rely_title, anchor="center")

            children = [title_lbl]
            if subtitle:
                sub_lbl = tk.Label(card, text=subtitle, bg="#2d3748", fg="#a0aec0",
                                   font=("TkSmallCaptionFont",))
                sub_lbl.place(relx=0.5, rely=0.72, anchor="center")
                children.append(sub_lbl)

            def _on_enter(e, f=card, lbls=children):
                f.configure(bg="#4a5568")
                for lbl in lbls:
                    lbl.configure(bg="#4a5568")

            def _on_leave(e, f=card, lbls=children):
                f.configure(bg="#2d3748")
                for lbl in lbls:
                    lbl.configure(bg="#2d3748")

            for w in [card] + children:
                w.bind("<Button-1>", lambda e, fn=on_click: fn())
                w.bind("<Enter>", _on_enter)
                w.bind("<Leave>", _on_leave)

    # ── Thumbnail grid (leaf view) ────────────────────────────────────────────

    def _render_grid(self, keys: list) -> None:
        self._clear_content()
        cell_w = THUMB_W + THUMB_PAD * 2
        cell_h = THUMB_H + THUMB_PAD * 2 + 20

        for idx, key in enumerate(keys):
            row, col = divmod(idx, COLS)
            cell = tk.Frame(self._grid_frame, bg="#2a2a2a",
                            width=cell_w, height=cell_h, relief=tk.FLAT, bd=1)
            cell.grid(row=row, column=col,
                      padx=THUMB_PAD // 2, pady=THUMB_PAD // 2)
            cell.grid_propagate(False)

            thumb_label = tk.Label(cell, bg="#333333", width=THUMB_W, height=THUMB_H)
            thumb_label.place(x=THUMB_PAD, y=THUMB_PAD, width=THUMB_W, height=THUMB_H)

            cap_label = tk.Label(cell, text=key.rsplit("/", 1)[-1][:22],
                                 bg="#2a2a2a", fg="#cccccc",
                                 font=("TkSmallCaptionFont",))
            cap_label.place(x=2, y=THUMB_H + THUMB_PAD + 2)

            for widget in (cell, thumb_label, cap_label):
                widget.bind("<Button-1>", lambda e, k=key: self._open_detail(k))
                widget.bind("<Enter>", lambda e, f=cell: f.configure(bg="#3a3a3a"))
                widget.bind("<Leave>", lambda e, f=cell: f.configure(bg="#2a2a2a"))

            cached = self._cache.get(key)
            if cached:
                self._apply_thumb(thumb_label, key, cached)
            else:
                self._cache.request(
                    key,
                    lambda k, img, lbl=thumb_label:
                        self.after(0, self._apply_thumb, lbl, k, img)
                )

    def _apply_thumb(self, label: tk.Label, key: str, img: Image.Image) -> None:
        photo = ImageTk.PhotoImage(img)
        self._photo_refs[key] = photo
        label.configure(image=photo, width=THUMB_W, height=THUMB_H)

    # ── Detail / playback ────────────────────────────────────────────────────

    def _open_detail(self, key: str) -> None:
        if key.endswith(".mp4"):
            self._play_video(key)
        else:
            DetailWindow(self, key, self._current_keys, self._loader, self._meta)

    def _play_video(self, key: str) -> None:
        self._status_var.set("Downloading video…")

        def worker():
            try:
                tmp_path = self._loader.download_to_tempfile(key)
                subprocess.Popen(["xdg-open", tmp_path])
                self.after(0, self._status_var.set, "")
            except Exception as exc:
                self.after(0, messagebox.showerror, "Video Error", str(exc))

        threading.Thread(target=worker, daemon=True).start()

    # ── Favorites / Albums ───────────────────────────────────────────────────

    def _show_favorites(self) -> None:
        self._breadcrumb = [("Home", self._show_home), ("Favorites", self._show_favorites)]
        self._update_breadcrumb()
        keys = sorted(self._meta.favorites, key=_parse_ts, reverse=True)
        self._current_keys = keys
        self._status_var.set(f"Favorites — {len(keys)} item{'s' if len(keys) != 1 else ''}")
        self._render_grid(keys)

    def _show_albums_menu(self) -> None:
        menu = tk.Menu(self, tearoff=0)
        albums = self._meta.list_albums()
        if albums:
            for album in albums:
                menu.add_command(label=album, command=lambda a=album: self._show_album(a))
        else:
            menu.add_command(label="(no albums yet)", state=tk.DISABLED)
        menu.tk_popup(self.winfo_pointerx(), self.winfo_pointery())

    def _show_album(self, album: str) -> None:
        self._breadcrumb = [
            ("Home", self._show_home),
            (f"Album: {album}", lambda a=album: self._show_album(a)),
        ]
        self._update_breadcrumb()
        keys = self._meta.keys_in_album(album)
        self._current_keys = keys
        self._status_var.set(f"Album: {album} — {len(keys)} item{'s' if len(keys) != 1 else ''}")
        self._render_grid(keys)


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    )
    app = ViewerApp()
    app.mainloop()


if __name__ == "__main__":
    main()

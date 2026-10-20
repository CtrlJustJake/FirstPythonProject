"""Card collection viewer that can be frozen into a Windows executable.

Three jobs live in this file:

1. Paths. A frozen .exe unpacks into a temporary folder that disappears.
   Read bundled files from that folder. Write memory (settings, cache, images)
   next to the .exe so the next launch still has them.
2. Memory. RAM is wiped when the process exits. A small JSON file is how the
   program remembers the last card and your settings.
3. The window. CardApp owns the widgets, the current card, and the saved state.
   Add a feature by adding a widget in _build_ui and a variable in DEFAULT_STATE.
"""

import json
import logging
import queue
import sys
import threading
import time
import tkinter as tk
from datetime import datetime, timezone
from pathlib import Path
from tkinter import messagebox, ttk

import pandas as pd
import requests
from PIL import Image, ImageTk

APP_NAME = "CardShow"
USER_AGENT = "CardShow/1.0 (personal Magic collection viewer)"
SCRYFALL_COLLECTION = "https://api.scryfall.com/cards/collection"
BATCH_SIZE = 75
REQUEST_GAP_SECONDS = 0.12

# Colors are collected here so a new theme is a second dictionary, not a hunt
# through the widget code.
THEMES = {
    "dark": {
        "bg": "#12141a",
        "panel": "#1c2030",
        "text": "#f3efe4",
        "muted": "#a39e93",
        "gold": "#d4af37",
        "up": "#3dd68c",
        "down": "#f07178",
        "field": "#0e1118",
    },
    "light": {
        "bg": "#f6f3ec",
        "panel": "#fffdf8",
        "text": "#1c1a16",
        "muted": "#6d675d",
        "gold": "#8a6a12",
        "up": "#0d7a45",
        "down": "#a32030",
        "field": "#ffffff",
    },
}

DEFAULT_STATE = {
    "last_id": None,
    "autoplay": True,
    "resume": True,
    "interval_seconds": 4,
    "image_width": 320,
    "sort": "Name",
    "price_filter": "All",
    "theme": "dark",
    "geometry": "440x860",
}


def is_frozen():
    """True when PyInstaller is running this file as an .exe."""
    return getattr(sys, "frozen", False)


def bundle_dir():
    """Folder of files packed into the executable. Read-only after a build."""
    if is_frozen():
        return Path(sys._MEIPASS)
    return Path(__file__).resolve().parent


def data_dir():
    """Folder we are allowed to write. This is the program's long-term memory."""
    if is_frozen():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def collection_csv():
    """Prefer a CSV sitting beside the .exe, so you can swap collections without rebuilding."""
    beside_program = data_dir() / "ManaBox_Collection.csv"
    if beside_program.exists():
        return beside_program
    return bundle_dir() / "ManaBox_Collection.csv"


def state_path():
    return data_dir() / "cardshow_state.json"


def cache_path():
    return data_dir() / "price_cache.json"


def image_dir():
    folder = data_dir() / "downloaded_images"
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def image_file(card_id):
    return image_dir() / f"{card_id}.png"


def setup_logging():
    logging.basicConfig(
        filename=data_dir() / "cardshow.log",
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )


def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return default
    if isinstance(default, dict) and isinstance(data, dict):
        merged = dict(default)
        merged.update(data)
        return merged
    return data


def save_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2)
    temporary.replace(path)


def load_collection(csv_path):
    """One row per Scryfall printing, with quantity and a weighted purchase price."""
    frame = pd.read_csv(csv_path)
    frame = frame.rename(columns={"Scryfall ID": "ScryfallID", "Purchase price": "PurchasePrice"})
    frame = frame.dropna(subset=["ScryfallID"])
    frame["Quantity"] = pd.to_numeric(frame["Quantity"], errors="coerce").fillna(1)
    frame["PurchasePrice"] = pd.to_numeric(frame["PurchasePrice"], errors="coerce")
    frame["Spend"] = frame["PurchasePrice"] * frame["Quantity"]

    grouped = frame.groupby("ScryfallID", as_index=False).agg(
        name=("Name", "first"),
        set_name=("Set name", "first"),
        rarity=("Rarity", "first"),
        foil=("Foil", "first"),
        quantity=("Quantity", "sum"),
        spend=("Spend", "sum"),
    )
    grouped["purchase"] = grouped["spend"] / grouped["quantity"]

    cards = []
    for row in grouped.itertuples(index=False):
        cards.append(
            {
                "id": str(row.ScryfallID),
                "name": str(row.name),
                "set_name": "" if pd.isna(row.set_name) else str(row.set_name),
                "rarity": "" if pd.isna(row.rarity) else str(row.rarity),
                "foil": "" if pd.isna(row.foil) else str(row.foil),
                "quantity": int(row.quantity),
                "purchase": None if pd.isna(row.purchase) else float(row.purchase),
                "usd": None,
                "usd_foil": None,
                "usd_etched": None,
                "png": None,
            }
        )
    return cards


def apply_cache(cards, cache):
    stored = cache.get("cards", {})
    for card in cards:
        found = stored.get(card["id"])
        if not found:
            continue
        card["png"] = found.get("png")
        for key in ("usd", "usd_foil", "usd_etched"):
            card[key] = _as_float(found.get(key))


def cache_from_cards(cards):
    stored = {}
    for card in cards:
        stored[card["id"]] = {
            "png": card.get("png"),
            "usd": card.get("usd"),
            "usd_foil": card.get("usd_foil"),
            "usd_etched": card.get("usd_etched"),
        }
    return {
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "cards": stored,
    }


def _as_float(value):
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def market_price(card):
    """Use the finish you actually own. Fall back if Scryfall has no price for it."""
    finish = (card.get("foil") or "").lower()
    if finish == "foil":
        order = ("usd_foil", "usd", "usd_etched")
    elif finish == "etched":
        order = ("usd_etched", "usd", "usd_foil")
    else:
        order = ("usd", "usd_foil", "usd_etched")
    for key in order:
        price = card.get(key)
        if price is not None:
            return price
    return None


def png_url_from_card(payload):
    uris = payload.get("image_uris") or {}
    if uris.get("png"):
        return uris["png"]
    faces = payload.get("card_faces") or []
    if faces:
        return (faces[0].get("image_uris") or {}).get("png")
    return None


def fetch_scryfall(card_ids, status_callback):
    """Ask Scryfall for prices in batches. Returns a dict keyed by card id."""
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    found = {}
    ids = [card_id for card_id in card_ids if card_id]
    batches = [ids[start : start + BATCH_SIZE] for start in range(0, len(ids), BATCH_SIZE)]

    for number, batch in enumerate(batches, start=1):
        status_callback(f"Fetching prices {number} / {len(batches)}")
        payload = {"identifiers": [{"id": card_id} for card_id in batch]}
        response = requests.post(SCRYFALL_COLLECTION, json=payload, headers=headers, timeout=30)
        response.raise_for_status()
        for card in response.json().get("data", []):
            prices = card.get("prices") or {}
            found[card.get("id")] = {
                "png": png_url_from_card(card),
                "usd": _as_float(prices.get("usd")),
                "usd_foil": _as_float(prices.get("usd_foil")),
                "usd_etched": _as_float(prices.get("usd_etched")),
            }
        time.sleep(REQUEST_GAP_SECONDS)
    return found


def download_png(url, destination):
    headers = {"User-Agent": USER_AGENT, "Accept": "image/png"}
    response = requests.get(url, headers=headers, timeout=30)
    response.raise_for_status()
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(".part")
    temporary.write_bytes(response.content)
    temporary.replace(destination)


def money(value):
    if value is None:
        return "—"
    return f"${value:,.2f}"


def gain_text(current, purchase):
    if current is None or purchase in (None, 0):
        return "—", None
    delta = current - purchase
    percent = (current / purchase - 1) * 100
    sign = "+" if delta >= 0 else ""
    return f"{sign}{money(delta)}  ({sign}{percent:.0f}%)", delta >= 0


class CardApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.state = load_json(state_path(), DEFAULT_STATE)
        self.theme_name = self.state.get("theme", "dark")
        if self.theme_name not in THEMES:
            self.theme_name = "dark"
        self.colors = THEMES[self.theme_name]

        self.cards = load_collection(collection_csv())
        self.cache = load_json(cache_path(), {"fetched_at": None, "cards": {}})
        apply_cache(self.cards, self.cache)
        self.by_id = {card["id"]: card for card in self.cards}

        self.events = queue.Queue()
        self.image_jobs = queue.Queue()
        self.photo = None
        self.current_id = None
        self.playing = bool(self.state.get("autoplay", True))
        self._after_id = None
        self._refreshing = False
        self._image_inflight = set()
        threading.Thread(target=self._image_loop, daemon=True).start()

        self.title(APP_NAME)
        self.geometry(self.state.get("geometry") or DEFAULT_STATE["geometry"])
        self.minsize(380, 700)
        self._build_ui()
        self._apply_theme()
        self._restore_position()
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.after(100, self._poll_events)

        if not self.cache.get("cards"):
            self.refresh_prices()
        else:
            self._set_status(self._cache_status())

    def _build_ui(self):
        self.option_add("*Font", ("Segoe UI", 10))
        self.search_var = tk.StringVar()
        self.sort_var = tk.StringVar(value=self.state.get("sort", "Name"))
        self.filter_var = tk.StringVar(value=self.state.get("price_filter", "All"))
        self.search_var.trace_add("write", lambda *_: self._on_view_changed())

        self.menu = tk.Menu(self)
        self.config(menu=self.menu)
        file_menu = tk.Menu(self.menu, tearoff=0)
        view_menu = tk.Menu(self.menu, tearoff=0)
        help_menu = tk.Menu(self.menu, tearoff=0)
        self.menu.add_cascade(label="File", menu=file_menu)
        self.menu.add_cascade(label="View", menu=view_menu)
        self.menu.add_cascade(label="Help", menu=help_menu)
        file_menu.add_command(label="Refresh prices", command=self.refresh_prices)
        file_menu.add_command(label="Save memory now", command=self.save_memory)
        file_menu.add_separator()
        file_menu.add_command(label="Exit", command=self.on_close)
        view_menu.add_command(label="Settings", command=self.open_settings)
        help_menu.add_command(label="Where memory is stored", command=self.show_memory_help)

        top = tk.Frame(self, padx=12, pady=8)
        top.pack(fill="x")
        self.top = top

        self.find_label = tk.Label(top, text="Find")
        self.find_label.pack(side="left", padx=(0, 6))
        self.search_entry = tk.Entry(top, textvariable=self.search_var)
        self.search_entry.pack(side="left", fill="x", expand=True, ipady=4)

        self.sort_box = ttk.Combobox(
            top,
            textvariable=self.sort_var,
            values=("Name", "Price high", "Price low", "Gain high", "Gain low"),
            width=12,
            state="readonly",
        )
        self.sort_box.pack(side="left", padx=(8, 0))
        self.sort_box.bind("<<ComboboxSelected>>", lambda _event: self._on_view_changed())

        self.filter_box = ttk.Combobox(
            top,
            textvariable=self.filter_var,
            values=("All", "Gainers", "Losers", "Unpriced"),
            width=10,
            state="readonly",
        )
        self.filter_box.pack(side="left", padx=(8, 0))
        self.filter_box.bind("<<ComboboxSelected>>", lambda _event: self._on_view_changed())

        self.name_label = tk.Label(self, text="", font=("Segoe UI", 14, "bold"), wraplength=400)
        self.name_label.pack(padx=12, pady=(4, 0))
        self.meta_label = tk.Label(self, text="", font=("Segoe UI", 9))
        self.meta_label.pack(padx=12)

        self.image_label = tk.Label(self, text="No card yet")
        self.image_label.pack(padx=12, pady=8)

        prices = tk.Frame(self)
        prices.pack(fill="x", padx=16)
        self.prices = prices
        self.now_label = tk.Label(prices, text="", font=("Segoe UI", 12, "bold"))
        self.paid_label = tk.Label(prices, text="")
        self.gain_label = tk.Label(prices, text="", font=("Segoe UI", 11, "bold"))
        self.now_label.pack(side="left")
        self.paid_label.pack(side="left", padx=12)
        self.gain_label.pack(side="right")

        controls = tk.Frame(self, pady=10)
        controls.pack()
        self.controls = controls
        self.prev_button = tk.Button(controls, text="Prev", width=10, command=self.show_previous)
        self.play_button = tk.Button(controls, text="Pause", width=10, command=self.toggle_play)
        self.next_button = tk.Button(controls, text="Next", width=10, command=self.show_next)
        self.prev_button.pack(side="left", padx=4)
        self.play_button.pack(side="left", padx=4)
        self.next_button.pack(side="left", padx=4)

        self.count_label = tk.Label(self, text="")
        self.count_label.pack()
        self.status_label = tk.Label(self, text="", anchor="w")
        self.status_label.pack(fill="x", padx=12, pady=(4, 10))

        self.bind("<Left>", lambda _event: self.show_previous())
        self.bind("<Right>", lambda _event: self.show_next())
        self.bind("<space>", self._on_space)

    def _apply_theme(self):
        colors = self.colors
        self.configure(bg=colors["bg"])
        for widget in (
            self.top,
            self.find_label,
            self.name_label,
            self.meta_label,
            self.image_label,
            self.prices,
            self.now_label,
            self.paid_label,
            self.gain_label,
            self.controls,
            self.count_label,
            self.status_label,
        ):
            widget.configure(bg=colors["bg"])
        for label in (self.name_label, self.now_label, self.count_label):
            label.configure(fg=colors["text"])
        for label in (self.find_label, self.meta_label, self.paid_label, self.status_label):
            label.configure(fg=colors["muted"])
        self.image_label.configure(fg=colors["muted"], bg=colors["panel"])
        self.search_entry.configure(
            bg=colors["field"],
            fg=colors["text"],
            insertbackground=colors["text"],
            relief="flat",
        )
        for button in (self.prev_button, self.play_button, self.next_button):
            button.configure(
                bg=colors["panel"],
                fg=colors["text"],
                activebackground=colors["gold"],
                activeforeground=colors["bg"],
                relief="flat",
                padx=6,
                pady=4,
            )

    def visible_cards(self):
        query = self.search_var.get().strip().lower()
        choice = self.filter_var.get()
        rows = []
        for card in self.cards:
            if query and query not in card["name"].lower():
                continue
            current = market_price(card)
            purchase = card.get("purchase")
            if choice == "Gainers" and (current is None or purchase is None or current < purchase):
                continue
            if choice == "Losers" and (current is None or purchase is None or current >= purchase):
                continue
            if choice == "Unpriced" and current is not None:
                continue
            rows.append(card)

        def sort_key(card):
            current = market_price(card)
            purchase = card.get("purchase")
            gain = None if current is None or purchase is None else current - purchase
            name = card["name"].lower()
            mode = self.sort_var.get()
            if mode == "Price high":
                return (current is None, -(current or 0), name)
            if mode == "Price low":
                return (current is None, current if current is not None else 0, name)
            if mode == "Gain high":
                return (gain is None, -(gain or 0), name)
            if mode == "Gain low":
                return (gain is None, gain if gain is not None else 0, name)
            return (name,)

        rows.sort(key=sort_key)
        return rows

    def _restore_position(self):
        cards = self.visible_cards()
        if not cards:
            self._show_empty()
            return
        visible_ids = {card["id"] for card in cards}
        target = self.state.get("last_id") if self.state.get("resume", True) else None
        self.show_card(target if target in visible_ids else cards[0]["id"])

    def _on_view_changed(self):
        cards = self.visible_cards()
        if not cards:
            self._show_empty()
            return
        if self.current_id not in {card["id"] for card in cards}:
            self.show_card(cards[0]["id"])
        else:
            self._update_count()

    def _show_empty(self):
        self.current_id = None
        self._cancel_advance()
        self.name_label.config(text="No cards match")
        self.meta_label.config(text="Clear the search or choose All")
        self.image_label.config(image="", text="Nothing to show")
        self.now_label.config(text="")
        self.paid_label.config(text="")
        self.gain_label.config(text="")
        self.count_label.config(text="0 cards")

    def show_card(self, card_id):
        card = self.by_id.get(card_id)
        if card is None:
            return
        self.current_id = card_id
        self.name_label.config(text=card["name"])
        finish = card["foil"] if card["foil"] and card["foil"] != "normal" else "nonfoil"
        self.meta_label.config(
            text=f"{card['set_name']}  ·  {card['rarity']}  ·  {finish}  ·  ×{card['quantity']}"
        )

        current = market_price(card)
        text, positive = gain_text(current, card.get("purchase"))
        self.now_label.config(text=f"Now {money(current)}")
        self.paid_label.config(text=f"Paid {money(card.get('purchase'))}")
        gain_color = self.colors["muted"]
        if positive is True:
            gain_color = self.colors["up"]
        elif positive is False:
            gain_color = self.colors["down"]
        self.gain_label.config(text=text, fg=gain_color)

        self._draw_image(card)
        self._ensure_image(card)
        upcoming = self._neighbor(1)
        if upcoming:
            self._ensure_image(upcoming)
        self._update_count()
        self._schedule_advance()
        self.state["last_id"] = card_id

    def _draw_image(self, card):
        path = image_file(card["id"])
        if not path.exists():
            self.image_label.config(image="", text="Fetching image…")
            self.photo = None
            return
        width = int(self.state.get("image_width") or 320)
        with Image.open(path) as raw:
            image = raw.convert("RGBA")
            image.thumbnail((width, int(width * 1.4)), Image.Resampling.LANCZOS)
            self.photo = ImageTk.PhotoImage(image.copy())
        self.image_label.config(image=self.photo, text="")

    def _update_count(self):
        cards = self.visible_cards()
        ids = [card["id"] for card in cards]
        if self.current_id in ids:
            place = ids.index(self.current_id) + 1
        else:
            place = 0
        self.count_label.config(text=f"{place} of {len(cards)}")
        self.play_button.config(text="Pause" if self.playing else "Play")

    def _neighbor(self, step):
        cards = self.visible_cards()
        if not cards or self.current_id is None:
            return None
        ids = [card["id"] for card in cards]
        if self.current_id not in ids:
            return cards[0]
        index = (ids.index(self.current_id) + step) % len(ids)
        return cards[index]

    def show_next(self):
        card = self._neighbor(1)
        if card:
            self.show_card(card["id"])

    def show_previous(self):
        card = self._neighbor(-1)
        if card:
            self.show_card(card["id"])

    def _on_space(self, _event):
        self.toggle_play()
        return "break"

    def toggle_play(self):
        self.playing = not self.playing
        self.state["autoplay"] = self.playing
        self._update_count()
        if self.playing:
            self._schedule_advance()
        else:
            self._cancel_advance()

    def _cancel_advance(self):
        if self._after_id is not None:
            self.after_cancel(self._after_id)
            self._after_id = None

    def _schedule_advance(self):
        self._cancel_advance()
        if not self.playing:
            return
        seconds = float(self.state.get("interval_seconds") or 4)
        self._after_id = self.after(int(seconds * 1000), self.show_next)

    def _ensure_image(self, card):
        """Queue one download. A single worker keeps Scryfall requests spaced out."""
        if image_file(card["id"]).exists() or card["id"] in self._image_inflight:
            return
        if not card.get("png"):
            return
        if self.image_jobs.qsize() >= 4:
            return
        self._image_inflight.add(card["id"])
        self.image_jobs.put((card["id"], card["png"]))

    def _image_loop(self):
        while True:
            card_id, url = self.image_jobs.get()
            try:
                download_png(url, image_file(card_id))
                self.events.put(("image", card_id))
            except Exception:
                logging.exception("Image download failed for %s", card_id)
                self.events.put(("status", "Could not download the current image"))
            finally:
                self._image_inflight.discard(card_id)
                time.sleep(REQUEST_GAP_SECONDS)

    def refresh_prices(self):
        if self._refreshing:
            return
        self._refreshing = True
        self._set_status("Refreshing prices…")
        threading.Thread(target=self._refresh_worker, daemon=True).start()

    def _refresh_worker(self):
        try:
            found = fetch_scryfall([card["id"] for card in self.cards], lambda text: self.events.put(("status", text)))
            self.events.put(("prices", found))
        except Exception as exc:
            logging.exception("Price refresh failed")
            self.events.put(("status", f"Price refresh failed: {exc}"))
            self.events.put(("refresh_done", None))

    def _poll_events(self):
        while True:
            try:
                kind, payload = self.events.get_nowait()
            except queue.Empty:
                break
            if kind == "status":
                self._set_status(payload)
            elif kind == "image" and payload == self.current_id:
                card = self.by_id.get(payload)
                if card:
                    self._draw_image(card)
            elif kind == "prices":
                for card_id, prices in payload.items():
                    card = self.by_id.get(card_id)
                    if not card:
                        continue
                    card.update(prices)
                self.cache = cache_from_cards(self.cards)
                save_json(cache_path(), self.cache)
                self._refreshing = False
                self._set_status(self._cache_status())
                if self.current_id:
                    self.show_card(self.current_id)
            elif kind == "refresh_done":
                self._refreshing = False
        self.after(100, self._poll_events)

    def _cache_status(self):
        when = self.cache.get("fetched_at")
        if not when:
            return "No saved prices yet"
        return f"Prices remembered from {when}"

    def _set_status(self, text):
        self.status_label.config(text=text)

    def open_settings(self):
        window = tk.Toplevel(self)
        window.title("Settings")
        window.configure(bg=self.colors["bg"])
        window.transient(self)
        window.resizable(False, False)

        interval = tk.IntVar(value=int(self.state.get("interval_seconds") or 4))
        autoplay = tk.BooleanVar(value=bool(self.state.get("autoplay")))
        resume = tk.BooleanVar(value=bool(self.state.get("resume", True)))
        width = tk.IntVar(value=int(self.state.get("image_width") or 320))
        theme = tk.StringVar(value=self.theme_name)

        def add_row(row, label, widget):
            tk.Label(window, text=label, bg=self.colors["bg"], fg=self.colors["text"]).grid(
                row=row, column=0, sticky="w", padx=12, pady=8
            )
            widget.grid(row=row, column=1, sticky="ew", padx=12, pady=8)

        add_row(0, "Seconds per card", tk.Scale(
            window, from_=1, to=30, orient="horizontal", variable=interval,
            bg=self.colors["bg"], fg=self.colors["text"], highlightthickness=0, length=180,
        ))
        add_row(1, "Image width", tk.Scale(
            window, from_=220, to=480, orient="horizontal", variable=width,
            bg=self.colors["bg"], fg=self.colors["text"], highlightthickness=0, length=180,
        ))
        add_row(2, "Theme", ttk.Combobox(
            window, textvariable=theme, values=tuple(THEMES), state="readonly", width=12,
        ))
        tk.Checkbutton(
            window, text="Start slideshow playing", variable=autoplay,
            bg=self.colors["bg"], fg=self.colors["text"], selectcolor=self.colors["panel"],
            activebackground=self.colors["bg"], activeforeground=self.colors["text"],
        ).grid(row=3, column=0, columnspan=2, sticky="w", padx=12)
        tk.Checkbutton(
            window, text="Resume the last card next time", variable=resume,
            bg=self.colors["bg"], fg=self.colors["text"], selectcolor=self.colors["panel"],
            activebackground=self.colors["bg"], activeforeground=self.colors["text"],
        ).grid(row=4, column=0, columnspan=2, sticky="w", padx=12, pady=(0, 8))

        def apply_settings():
            self.state["interval_seconds"] = int(interval.get())
            self.state["autoplay"] = bool(autoplay.get())
            self.state["resume"] = bool(resume.get())
            self.state["image_width"] = int(width.get())
            self.state["theme"] = theme.get()
            self.playing = self.state["autoplay"]
            self.theme_name = theme.get() if theme.get() in THEMES else "dark"
            self.colors = THEMES[self.theme_name]
            self._apply_theme()
            if self.current_id:
                self.show_card(self.current_id)
            else:
                self._update_count()
            self.save_memory()
            window.destroy()

        tk.Button(
            window, text="Save settings", command=apply_settings,
            bg=self.colors["gold"], fg=self.colors["bg"], relief="flat", padx=8, pady=4,
        ).grid(row=5, column=0, columnspan=2, pady=12)

    def save_memory(self):
        self.state["sort"] = self.sort_var.get()
        self.state["price_filter"] = self.filter_var.get()
        self.state["geometry"] = self.geometry()
        self.state["theme"] = self.theme_name
        self.state["last_id"] = self.current_id
        save_json(state_path(), self.state)
        self._set_status(f"Memory saved to {state_path().name}")

    def show_memory_help(self):
        messagebox.showinfo(
            "Memory",
            "Closing the window saves cardshow_state.json beside the program.\n"
            "That file is the last card, sort, filter, theme, and slideshow settings.\n\n"
            "price_cache.json remembers Scryfall prices so the next launch does not start from zero.\n"
            "downloaded_images keeps pictures you have already fetched.\n\n"
            f"Those files live in:\n{data_dir()}",
        )

    def on_close(self):
        try:
            self.save_memory()
        finally:
            self.destroy()


def main():
    setup_logging()
    csv_path = collection_csv()
    if not csv_path.exists():
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror(
            APP_NAME,
            f"Could not find ManaBox_Collection.csv.\nLooked in:\n{csv_path}",
        )
        return
    app = CardApp()
    app.mainloop()


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""

solitare

made by @literal-gargoyle

Features
- draw 1 / draw 3
- Colors with suit-aware rendering (♥ ♦ red, ♠ ♣ neutral)
- Settings menu saved to disk (draw mode, theme, hints, ASCII fallback, animations)
- Local leaderboards (best times, fewest moves) stored in JSON
- Undo, hints, auto-move to foundations, restart
- Automatic dependency setup on Windows (installs `windows-curses` if needed)

Controls (in game)
  Arrow keys .. Move cursor
  Enter/Space . Select / place
  D .......... Draw from stock
  U .......... Undo
  H .......... Hint (toggle highlight)
  A .......... Auto-move any safe cards to foundations
  S .......... Settings
  L .......... Leaderboards
  N .......... New game
  Q .......... Quit

"""
from __future__ import annotations
import os, sys, json, random, time, platform, shutil, subprocess, locale
from dataclasses import dataclass, field
from typing import List, Tuple, Optional, Dict, Any

# ------------------------------------------------------------
# Dependency bootstrap (for Windows): install windows-curses
# ------------------------------------------------------------

def _ensure_curses():
    try:
        import curses  # noqa: F401
        return
    except Exception as e:
        if os.name == 'nt':
            # Try to install windows-curses automatically
            try:
                print("Installing windows-curses ...")
                subprocess.check_call([sys.executable, "-m", "pip", "install", "--upgrade", "pip"])  # ensure pip
            except Exception:
                pass
            try:
                subprocess.check_call([sys.executable, "-m", "pip", "install", "windows-curses"]) 
            except Exception as ie:
                print("Couldn't install windows-curses automatically. Please run: pip install windows-curses")
                print("Error:", ie)
                raise
        else:
            # Non-Windows error — re-raise original
            raise e

_ensure_curses()
import curses

# ------------------------------------------------------------
# App paths & persistence
# ------------------------------------------------------------
APP_DIR = os.path.join(os.path.expanduser("~"), ".solitaire_cmd")
SETTINGS_PATH = os.path.join(APP_DIR, "settings.json")
LEADERBOARD_PATH = os.path.join(APP_DIR, "leaderboard.json")

DEFAULT_SETTINGS = {
    "draw_count": 3,           # 1 or 3
    "auto_move": True,
    "show_hints": True,
    "theme": "classic",       # classic | high_contrast | green | blue
    "ascii_only": False,       # force ASCII fallback for suits
    "animations": False        # simple drop/flip animations (subtle)
}

if not os.path.isdir(APP_DIR):
    try:
        os.makedirs(APP_DIR, exist_ok=True)
    except Exception:
        pass


def load_settings() -> Dict[str, Any]:
    try:
        with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        # merge with defaults
        for k, v in DEFAULT_SETTINGS.items():
            data.setdefault(k, v)
        return data
    except Exception:
        return DEFAULT_SETTINGS.copy()


def save_settings(settings: Dict[str, Any]) -> None:
    try:
        with open(SETTINGS_PATH, "w", encoding="utf-8") as f:
            json.dump(settings, f, indent=2)
    except Exception:
        pass


def load_leaderboard() -> Dict[str, List[Dict[str, Any]]]:
    # { "best_times": [...], "fewest_moves": [...] }
    try:
        with open(LEADERBOARD_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        data.setdefault("best_times", [])
        data.setdefault("fewest_moves", [])
        return data
    except Exception:
        return {"best_times": [], "fewest_moves": []}


def save_leaderboard(board: Dict[str, List[Dict[str, Any]]]) -> None:
    try:
        with open(LEADERBOARD_PATH, "w", encoding="utf-8") as f:
            json.dump(board, f, indent=2)
    except Exception:
        pass

# ------------------------------------------------------------
# Cards & game model
# ------------------------------------------------------------
SUITS_UNICODE = ['♠', '♥', '♦', '♣']
SUITS_ASCII   = ['S', 'H', 'D', 'C']
VALUES = [None, 'A', '2', '3', '4', '5', '6', '7', '8', '9', '10', 'J', 'Q', 'K']

@dataclass
class Card:
    value: int     # 1..13
    suit: int      # 0..3 index into SUITS
    face_up: bool = False

    def color_red(self) -> bool:
        return self.suit in (1, 2)  # hearts or diamonds

    def suit_char(self, ascii_only: bool) -> str:
        return (SUITS_ASCII if ascii_only else SUITS_UNICODE)[self.suit]

    def label(self, ascii_only: bool) -> str:
        return f"{VALUES[self.value]}{self.suit_char(ascii_only)}"


def new_deck() -> List[Card]:
    deck = [Card(v, s, False) for s in range(4) for v in range(1, 14)]
    random.shuffle(deck)
    return deck

@dataclass
class GameState:
    stock: List[Card] = field(default_factory=list)
    waste: List[Card] = field(default_factory=list)
    foundations: List[List[Card]] = field(default_factory=lambda: [[], [], [], []])  # per suit
    tableaus: List[List[Card]] = field(default_factory=lambda: [[] for _ in range(7)])
    moves: int = 0
    start_time: float = 0.0
    won: bool = False

    def clone(self) -> 'GameState':
        # manual deep copy for performance clarity
        gs = GameState()
        gs.stock = [Card(c.value, c.suit, c.face_up) for c in self.stock]
        gs.waste = [Card(c.value, c.suit, c.face_up) for c in self.waste]
        gs.foundations = [[Card(c.value, c.suit, c.face_up) for c in pile] for pile in self.foundations]
        gs.tableaus = [[Card(c.value, c.suit, c.face_up) for c in pile] for pile in self.tableaus]
        gs.moves = self.moves
        gs.start_time = self.start_time
        gs.won = self.won
        return gs

# ------------------------------------------------------------
# Game logic
# ------------------------------------------------------------

def deal_new_game(settings: Dict[str, Any]) -> GameState:
    deck = new_deck()
    gs = GameState()
    # tableau: 1..7 with last face up
    for col in range(7):
        for i in range(col + 1):
            card = deck.pop()
            if i == col:
                card.face_up = True
            gs.tableaus[col].append(card)
    gs.stock = deck  # rest face down
    gs.moves = 0
    gs.start_time = time.monotonic()
    gs.won = False
    return gs


def can_place_on_tableau(moving: Card, target: Optional[Card]) -> bool:
    if target is None:
        return moving.value == 13  # only king on empty column
    # alternating colors, descending
    if moving.color_red() == target.color_red():
        return False
    return moving.value == target.value - 1


def can_place_on_foundation(moving: Card, foundation_top: Optional[Card]) -> bool:
    if foundation_top is None:
        return moving.value == 1  # Ace
    # same suit, ascending
    return moving.suit == foundation_top.suit and moving.value == foundation_top.value + 1


def flip_if_needed(pile: List[Card]) -> None:
    if pile and not pile[-1].face_up:
        pile[-1].face_up = True


# Move helpers return True if a move happened

def move_waste_to_foundation(gs: GameState) -> bool:
    if not gs.waste:
        return False
    card = gs.waste[-1]
    for s in range(4):
        top = gs.foundations[s][-1] if gs.foundations[s] else None
        if can_place_on_foundation(card, top) and (top is None or top.suit == card.suit):
            gs.foundations[s].append(gs.waste.pop())
            gs.moves += 1
            return True
    return False


def move_waste_to_tableau(gs: GameState, col: int) -> bool:
    if not gs.waste:
        return False
    card = gs.waste[-1]
    target = gs.tableaus[col][-1] if gs.tableaus[col] and gs.tableaus[col][-1].face_up else None
    if can_place_on_tableau(card, target):
        gs.tableaus[col].append(gs.waste.pop())
        gs.moves += 1
        return True
    return False


def move_tableau_to_foundation(gs: GameState, col: int) -> bool:
    if not gs.tableaus[col]:
        return False
    card = gs.tableaus[col][-1]
    if not card.face_up:
        return False
    s = card.suit
    top = gs.foundations[s][-1] if gs.foundations[s] else None
    if can_place_on_foundation(card, top):
        gs.foundations[s].append(gs.tableaus[col].pop())
        flip_if_needed(gs.tableaus[col])
        gs.moves += 1
        return True
    return False


def move_tableau_to_tableau(gs: GameState, src: int, dst: int, depth: int) -> bool:
    # depth counts from top of src face-up run
    if src == dst:
        return False
    if not gs.tableaus[src]:
        return False
    # find run of face-up cards
    pile = gs.tableaus[src]
    # get starting index of face-up run
    idx = len(pile) - 1
    while idx >= 0 and pile[idx].face_up:
        idx -= 1
    first_faceup = idx + 1
    if first_faceup >= len(pile):
        return False
    run = pile[first_faceup:]
    if depth < 1 or depth > len(run):
        return False
    moving_stack = run[:depth]
    target = gs.tableaus[dst][-1] if (gs.tableaus[dst] and gs.tableaus[dst][-1].face_up) else None
    if not can_place_on_tableau(moving_stack[0], target):
        return False
    # perform move
    gs.tableaus[dst].extend(moving_stack)
    del pile[first_faceup:first_faceup + depth]
    flip_if_needed(gs.tableaus[src])
    gs.moves += 1
    return True


def draw_from_stock(gs: GameState, draw_count: int) -> bool:
    if gs.stock:
        k = min(draw_count, len(gs.stock))
        for _ in range(k):
            c = gs.stock.pop()
            c.face_up = True
            gs.waste.append(c)
        gs.moves += 1
        return True
    else:
        if gs.waste:
            # recycle waste back to stock (face down)
            while gs.waste:
                c = gs.waste.pop()
                c.face_up = False
                gs.stock.append(c)
            gs.moves += 1
            return True
    return False


def check_win(gs: GameState) -> bool:
    total = sum(len(p) for p in gs.foundations)
    if total == 52:
        gs.won = True
        return True
    return False


def auto_move_safe(gs: GameState) -> int:
    """Move any clearly safe moves to foundations.
    Returns number of moves performed.
    """
    count = 0
    moved = True
    while moved:
        moved = False
        # Waste to foundation
        if move_waste_to_foundation(gs):
            moved = True; count += 1
        # Tableau to foundation
        for col in range(7):
            if move_tableau_to_foundation(gs, col):
                moved = True; count += 1
    return count

# ------------------------------------------------------------
# UI (curses)
# ------------------------------------------------------------

CARD_W = 4   # like [A♥]
CARD_H = 1
TAB_VSTEP = 1

class UI:
    def __init__(self, stdscr, settings: Dict[str, Any]):
        self.stdscr = stdscr
        self.settings = settings
        self.ascii_only = settings.get("ascii_only", False) or not self._supports_unicode()
        self.theme = settings.get("theme", "classic")
        self.status_msg = ""
        self.hint_targets: List[Tuple[str, int]] = []  # e.g., ("tableau", 3)
        self.cursor = ("stock", 0)  # (zone, index)

    def _supports_unicode(self) -> bool:
        try:
            enc = locale.getpreferredencoding(False) or "utf-8"
            "♠".encode(enc, errors="strict")
            return True
        except Exception:
            return False

    def init_colors(self):
        curses.start_color()
        curses.use_default_colors()
        # color pairs: 1 default, 2 red, 3 highlight, 4 dim, 5 accent
        base_fg = self._theme_color()
        curses.init_pair(1, base_fg, -1)
        curses.init_pair(2, curses.COLOR_RED, -1)
        curses.init_pair(3, curses.COLOR_BLACK, curses.COLOR_YELLOW)
        curses.init_pair(4, curses.COLOR_CYAN, -1)
        curses.init_pair(5, curses.COLOR_GREEN, -1)

    def _theme_color(self) -> int:
        if self.theme == "green":
            return curses.COLOR_GREEN
        if self.theme == "blue":
            return curses.COLOR_BLUE
        if self.theme == "high_contrast":
            return curses.COLOR_WHITE
        return curses.COLOR_WHITE

    # ------------------------- drawing -------------------------
    def draw(self, gs: GameState):
        self.stdscr.erase()
        maxy, maxx = self.stdscr.getmaxyx()

        # title and stats
        title = "Solitaire"
        self.stdscr.addstr(0, 2, title, curses.color_pair(4) | curses.A_BOLD)
        elapsed = int(time.monotonic() - gs.start_time) if not gs.won else int(self._win_time)
        mins, secs = divmod(max(elapsed, 0), 60)
        stats = f"Time {mins:02d}:{secs:02d}  Moves {gs.moves}  Draw{self.settings['draw_count']}  H for hints  S settings  L leaderboards"
        self.stdscr.addstr(1, 2, stats, curses.color_pair(1))
        if self.status_msg:
            self.stdscr.addstr(2, 2, self.status_msg[:max(0, maxx-4)], curses.color_pair(5))

        # zones layout (top row)
        y0 = 4
        x0 = 2
        pad = 2
        # Stock, Waste, Foundations(4)
        # Stock box
        self._draw_pile_box(y0, x0, label="STK", selected=self.cursor==("stock",0), count=len(gs.stock), face_up=False)
        # Waste
        self._draw_card(y0, x0 + (CARD_W+pad)*1, gs.waste[-1] if gs.waste else None, label="WST", selected=self.cursor==("waste",0))
        # Foundations
        for s in range(4):
            card = gs.foundations[s][-1] if gs.foundations[s] else None
            sel = self.cursor == ("foundation", s)
            self._draw_card(y0, x0 + (CARD_W+pad)*(3+s), card, label=f"F{s+1}", selected=sel)

        # Tableaus (7 cols)
        y_tab = y0 + 3
        for col in range(7):
            x = x0 + (CARD_W+pad)*col
            self._draw_column(gs.tableaus[col], y_tab, x, selected=(self.cursor==("tableau", col)))

        if gs.won:
            msg = "YOU WON! Press N for new game or L for leaderboards."
            self._center_banner(msg, color=5)

        self.stdscr.refresh()

    def _center_banner(self, msg: str, color=4):
        maxy, maxx = self.stdscr.getmaxyx()
        y = maxy//2
        x = max(0, (maxx - len(msg))//2)
        try:
            self.stdscr.addstr(y, x, msg, curses.color_pair(color) | curses.A_BOLD)
        except curses.error:
            pass

    def _draw_pile_box(self, y: int, x: int, label: str = "", selected: bool=False, count: int=0, face_up: bool=False):
        txt = "[##]" if count>0 else "[  ]"
        attr = curses.color_pair(1) | (curses.A_REVERSE if selected else 0)
        try:
            self.stdscr.addstr(y, x, txt, attr)
            if label:
                self.stdscr.addstr(y+1, x, f" {label} ", curses.color_pair(4))
        except curses.error:
            pass

    def _draw_card(self, y: int, x: int, card: Optional[Card], label: str = "", selected: bool=False):
        if card is None:
            txt = "[  ]"
            attr = curses.color_pair(1)
        else:
            s = card.label(self.ascii_only)
            s = s.rjust(2)[:2] if len(s)<=2 else s
            txt = f"[{s}]"
            attr = curses.color_pair(2) if card.color_red() else curses.color_pair(1)
        if selected:
            attr |= curses.A_REVERSE
        try:
            self.stdscr.addstr(y, x, txt, attr)
            if label:
                self.stdscr.addstr(y+1, x, f" {label} ", curses.color_pair(4))
        except curses.error:
            pass

    def _draw_column(self, pile: List[Card], y: int, x: int, selected: bool=False):
        # draw vertical pile
        for i, c in enumerate(pile):
            yy = y + i * TAB_VSTEP
            if c.face_up:
                self._draw_card(yy, x, c)
            else:
                try:
                    self.stdscr.addstr(yy, x, "[##]", curses.color_pair(4))
                except curses.error:
                    pass
        if selected:
            try:
                self.stdscr.addstr(y-1, x, "▼", curses.color_pair(5))
            except curses.error:
                pass

# ------------------------------------------------------------
# Menus
# ------------------------------------------------------------

def show_settings(stdscr, settings: Dict[str, Any]) -> bool:
    """Returns True if settings changed."""
    opts = [
        ("Draw count", [1,3]),
        ("Auto-move", ["Off", "On"]),
        ("Show hints", ["Off", "On"]),
        ("Theme", ["classic", "high_contrast", "green", "blue"]),
        ("ASCII suits", ["Off", "On"]),
        ("Animations", ["Off", "On"]),
    ]
    idxs = {
        0: (0, 1 if settings['draw_count']==3 else 0),
        1: (1, 1 if settings['auto_move'] else 0),
        2: (2, 1 if settings['show_hints'] else 0),
        3: (3, ["classic","high_contrast","green","blue"].index(settings['theme'])),
        4: (4, 1 if settings['ascii_only'] else 0),
        5: (5, 1 if settings['animations'] else 0),
    }
    sel = 0
    changed = False

    while True:
        stdscr.erase()
        maxy, maxx = stdscr.getmaxyx()
        title = "Settings — arrows to change, Enter to toggle, Q to exit"
        try:
            stdscr.addstr(1, max(0,(maxx-len(title))//2), title, curses.color_pair(4) | curses.A_BOLD)
        except curses.error:
            pass
        y = 4
        for i, (name, choices) in enumerate(opts):
            current = None
            if i == 0:
                current = settings['draw_count']
            elif i == 1:
                current = "On" if settings['auto_move'] else "Off"
            elif i == 2:
                current = "On" if settings['show_hints'] else "Off"
            elif i == 3:
                current = settings['theme']
            elif i == 4:
                current = "On" if settings['ascii_only'] else "Off"
            elif i == 5:
                current = "On" if settings['animations'] else "Off"

            row = f"{name:<14}: {current}"
            attr = curses.A_REVERSE if i==sel else 0
            try:
                stdscr.addstr(y+i, 4, row, curses.color_pair(1) | attr)
            except curses.error:
                pass

        ch = stdscr.getch()
        if ch in (ord('q'), ord('Q')):
            break
        elif ch in (curses.KEY_UP, ord('k')):
            sel = (sel - 1) % len(opts)
        elif ch in (curses.KEY_DOWN, ord('j')):
            sel = (sel + 1) % len(opts)
        elif ch in (curses.KEY_LEFT, ord('h')):
            # flip setting left
            changed = True
            if sel == 0:
                settings['draw_count'] = 1 if settings['draw_count']==3 else 3
            elif sel == 1:
                settings['auto_move'] = not settings['auto_move']
            elif sel == 2:
                settings['show_hints'] = not settings['show_hints']
            elif sel == 3:
                themes = ["classic","high_contrast","green","blue"]
                settings['theme'] = themes[(themes.index(settings['theme']) - 1) % len(themes)]
            elif sel == 4:
                settings['ascii_only'] = not settings['ascii_only']
            elif sel == 5:
                settings['animations'] = not settings['animations']
        elif ch in (curses.KEY_RIGHT, ord('l'), 10, 13):
            # flip right or enter
            changed = True
            if sel == 0:
                settings['draw_count'] = 3 if settings['draw_count']==1 else 1
            elif sel == 1:
                settings['auto_move'] = not settings['auto_move']
            elif sel == 2:
                settings['show_hints'] = not settings['show_hints']
            elif sel == 3:
                themes = ["classic","high_contrast","green","blue"]
                settings['theme'] = themes[(themes.index(settings['theme']) + 1) % len(themes)]
            elif sel == 4:
                settings['ascii_only'] = not settings['ascii_only']
            elif sel == 5:
                settings['animations'] = not settings['animations']

    return changed


def show_leaderboards(stdscr) -> None:
    board = load_leaderboard()
    entries_t = board.get("best_times", [])[:10]
    entries_m = board.get("fewest_moves", [])[:10]
    stdscr.erase()
    maxy, maxx = stdscr.getmaxyx()
    title = "Leaderboards — Q to exit"
    try:
        stdscr.addstr(1, max(0,(maxx-len(title))//2), title, curses.color_pair(4) | curses.A_BOLD)
    except curses.error:
        pass

    stdscr.addstr(4, 4, "Best Times:", curses.color_pair(1) | curses.A_BOLD)
    for i, e in enumerate(entries_t, start=1):
        mins, secs = divmod(int(e['time_s']), 60)
        line = f"{i:>2}. {e['name']:<14} {mins:02d}:{secs:02d}  moves:{e['moves']:<4} draw:{e['draw']}  {e['date']}"
        try:
            stdscr.addstr(4+i, 4, line, curses.color_pair(1))
        except curses.error:
            pass

    y0 = 6 + len(entries_t)
    stdscr.addstr(y0, 4, "Fewest Moves:", curses.color_pair(1) | curses.A_BOLD)
    for i, e in enumerate(entries_m, start=1):
        mins, secs = divmod(int(e['time_s']), 60)
        line = f"{i:>2}. {e['name']:<14} {e['moves']:<5}  time:{mins:02d}:{secs:02d} draw:{e['draw']}  {e['date']}"
        try:
            stdscr.addstr(y0+i, 4, line, curses.color_pair(1))
        except curses.error:
            pass

    while True:
        ch = stdscr.getch()
        if ch in (ord('q'), ord('Q'), 27):
            break

# ------------------------------------------------------------
# Main game loop
# ------------------------------------------------------------

class Game:
    def __init__(self, stdscr, settings: Dict[str, Any]):
        self.stdscr = stdscr
        self.settings = settings
        self.ui = UI(stdscr, settings)
        self.ui.init_colors()
        self.state = deal_new_game(settings)
        self.undo_stack: List[GameState] = []
        self._win_time = 0

    def push_undo(self):
        self.undo_stack.append(self.state.clone())
        if len(self.undo_stack) > 200:
            self.undo_stack.pop(0)

    def undo(self):
        if self.undo_stack:
            self.state = self.undo_stack.pop()

    def save_score_if_win(self):
        if self.state.won:
            end = time.monotonic()
            self._win_time = int(end - self.state.start_time)
            board = load_leaderboard()
            name = os.getenv("USERNAME") or os.getenv("USER") or "Player"
            entry = {
                "name": name,
                "time_s": self._win_time,
                "moves": self.state.moves,
                "draw": self.settings['draw_count'],
                "date": time.strftime("%Y-%m-%d")
            }
            # Best times sorted ascending time
            bt = board.setdefault("best_times", [])
            bt.append(entry)
            bt.sort(key=lambda e: e['time_s'])
            bt[:] = bt[:20]
            # Fewest moves sorted asc
            fm = board.setdefault("fewest_moves", [])
            fm.append(entry)
            fm.sort(key=lambda e: e['moves'])
            fm[:] = fm[:20]
            save_leaderboard(board)

    def cycle_cursor(self, dy: int, dx: int):
        zone, idx = self.ui.cursor
        order = [
            ("stock",0), ("waste",0), ("foundation",0), ("foundation",1), ("foundation",2), ("foundation",3),
            ("tableau",0), ("tableau",1), ("tableau",2), ("tableau",3), ("tableau",4), ("tableau",5), ("tableau",6)
        ]
        i = order.index((zone, idx))
        i = (i + dx + dy*0) % len(order)  # horizontal rotation only
        self.ui.cursor = order[i]

    def select_or_move(self):
        # basic selection: if cursor on a source with movable, remember it; next enter places to dest
        if not hasattr(self, "_sel"):
            self._sel = None
        zone, idx = self.ui.cursor
        if self._sel is None:
            # select origin
            if zone == "stock":
                # draw
                self.push_undo()
                draw_from_stock(self.state, self.settings['draw_count'])
            elif zone == "waste":
                if self.state.waste:
                    self._sel = ("waste", 0)
            elif zone == "foundation":
                # usually destination only; allow pick top card back to tableau
                if self.state.foundations[idx]:
                    self._sel = ("foundation", idx)
            elif zone == "tableau":
                # select top face-up run; default depth 1, allow ↑/↓ later to adjust depth
                if any(c.face_up for c in self.state.tableaus[idx][-len(self.state.tableaus[idx]):]):
                    self._sel = ("tableau", idx, 1)  # (src, col, depth)
        else:
            # place to destination
            src = self._sel
            self.push_undo()
            moved = False
            if src[0] == "waste":
                if zone == "foundation":
                    moved = move_waste_to_foundation(self.state)
                elif zone == "tableau":
                    moved = move_waste_to_tableau(self.state, idx)
            elif src[0] == "foundation":
                # move back to tableau if allowed
                if zone == "tableau":
                    # take top card of that foundation
                    card = self.state.foundations[src[1]][-1]
                    target = self.state.tableaus[idx][-1] if (self.state.tableaus[idx] and self.state.tableaus[idx][-1].face_up) else None
                    if can_place_on_tableau(card, target):
                        self.state.tableaus[idx].append(self.state.foundations[src[1]].pop())
                        self.state.moves += 1
                        moved = True
            elif src[0] == "tableau":
                s_col, depth = src[1], src[2]
                if zone == "foundation":
                    moved = move_tableau_to_foundation(self.state, s_col)
                elif zone == "tableau":
                    moved = move_tableau_to_tableau(self.state, s_col, idx, depth)
            if not moved:
                # rollback undo push if nothing happened
                if self.undo_stack:
                    self.state = self.undo_stack.pop()
            self._sel = None

    def adjust_depth(self, delta: int):
        if getattr(self, "_sel", None) and self._sel[0] == "tableau":
            zone, col, depth = self._sel
            # compute max depth available
            pile = self.state.tableaus[col]
            # length of face-up run
            i = len(pile)-1
            while i>=0 and pile[i].face_up: i -= 1
            maxd = len(pile) - (i+1)
            depth = max(1, min(maxd, depth + delta))
            self._sel = (zone, col, depth)

    def hint(self):
        if not self.settings.get('show_hints', True):
            self.ui.status_msg = "Hints are disabled in Settings."
            return
        tips = []
        # waste -> foundation/tableau
        if self.state.waste:
            w = self.state.waste[-1]
            # foundation
            top = self.state.foundations[w.suit][-1] if self.state.foundations[w.suit] else None
            if can_place_on_foundation(w, top):
                tips.append(("foundation", w.suit))
            # tableau
            for col in range(7):
                tgt = self.state.tableaus[col][-1] if (self.state.tableaus[col] and self.state.tableaus[col][-1].face_up) else None
                if can_place_on_tableau(w, tgt):
                    tips.append(("tableau", col))
        # tableau -> foundation
        for col in range(7):
            ttop = self.state.tableaus[col][-1] if self.state.tableaus[col] else None
            if ttop and ttop.face_up:
                top = self.state.foundations[ttop.suit][-1] if self.state.foundations[ttop.suit] else None
                if can_place_on_foundation(ttop, top):
                    tips.append(("foundation", ttop.suit))
        self.ui.hint_targets = tips
        if not tips:
            self.ui.status_msg = "No obvious moves. Try drawing (D)."
        else:
            self.ui.status_msg = f"Hints: {len(tips)} potential destinations highlighted (not shown in minimal UI)."

    def game_loop(self):
        curses.curs_set(0)
        self.stdscr.nodelay(False)
        while True:
            self.ui.draw(self.state)
            if check_win(self.state):
                self.save_score_if_win()
            ch = self.stdscr.getch()
            if ch in (ord('q'), ord('Q')):
                break
            elif ch in (curses.KEY_LEFT,):
                self.cycle_cursor(0, -1)
            elif ch in (curses.KEY_RIGHT,):
                self.cycle_cursor(0, +1)
            elif ch in (curses.KEY_UP,):
                self.adjust_depth(+1)
            elif ch in (curses.KEY_DOWN,):
                self.adjust_depth(-1)
            elif ch in (10, 13, ord(' ')):
                self.select_or_move()
                if self.settings.get('auto_move', True):
                    auto_move_safe(self.state)
            elif ch in (ord('d'), ord('D')):
                self.push_undo(); draw_from_stock(self.state, self.settings['draw_count'])
                if self.settings.get('auto_move', True):
                    auto_move_safe(self.state)
            elif ch in (ord('u'), ord('U')):
                self.undo()
            elif ch in (ord('h'), ord('H')):
                self.hint()
            elif ch in (ord('a'), ord('A')):
                self.push_undo(); auto_move_safe(self.state)
            elif ch in (ord('s'), ord('S')):
                if show_settings(self.stdscr, self.settings):
                    save_settings(self.settings)
                    # refresh UI settings
                    self.ui = UI(self.stdscr, self.settings)
                    self.ui.init_colors()
            elif ch in (ord('n'), ord('N')):
                self.state = deal_new_game(self.settings)
                self.undo_stack.clear()
                setattr(self, "_sel", None)
            elif ch in (ord('l'), ord('L')):
                show_leaderboards(self.stdscr)
            # ignore others


# ------------------------------------------------------------
# Entry point
# ------------------------------------------------------------

def main(stdscr):
    settings = load_settings()
    game = Game(stdscr, settings)
    game.game_loop()

if __name__ == "__main__":
    try:
        curses.wrapper(main)
    except KeyboardInterrupt:
        pass

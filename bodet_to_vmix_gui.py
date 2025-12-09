#!/usr/bin/env python3
import socket
import threading
import time
from dataclasses import dataclass, field
from typing import Dict, Any
import requests
import tkinter as tk
from tkinter import ttk
import os

# ==========================================================
#  KONFIGURASJON
# ==========================================================

CONFIG = {
    # Lytter på alle interfaces, port 4001, for Bodet "TV Protocol"
    "listen_host": "0.0.0.0",
    "listen_port": 4001,

    # vMix-tilkobling
    "vmix_host": "192.168.100.75",
    "vmix_port": 8088,
    "vmix_input": "17",  # ENBL_SCORE_BUG.gtzip

    # Mapping fra "logiske" felter -> GT SelectedName fra XML
    "fields": {
        # Lagnavn
        "home_name":        "A_TEAM_NAME.Text",
        "away_name":        "B_TEAM_NAME.Text",

        # Score
        "home_score":       "A_SCORE.Text",
        "away_score":       "B_SCORE.Text",

        # Periode / quarter-tekst (f.eks. "1st", "2nd", "OT")
        "period":           "QUARTER1.Text",

        # Tidsfelter
        "game_clock":       "TIME.Text",
        "shot_clock":       "SHOTCLOCK.Text",
    },

    # Fouls-bilder
    # 0 fouls → 0.png for begge lag
    # A: a1.png .. a5.png
    # B: b1.png .. b5.png
    "fouls_base_path": r"D:\enbl-grafikk\ENBL PROJECT\SCOREBUG\FOULS\Fouls",
    "fouls_files": {
        "A": {
            0: "0.png",
            1: "a1.png",
            2: "a2.png",
            3: "a3.png",
            4: "a4.png",
            5: "a5.png",
        },
        "B": {
            0: "0.png",
            1: "b1.png",
            2: "b2.png",
            3: "b3.png",
            4: "b4.png",
            5: "b5.png",
        },
    },
}

# ==========================================================
#  STATE / OVERRIDES
# ==========================================================

def dig(b: int) -> int:
    return b - 48 if 48 <= b <= 57 else 0


@dataclass
class TeamState:
    name: str = ""
    score: int = 0
    fouls: int = 0
    period_fouls: int = 0
    timeouts: int = 0


@dataclass
class ScoreState:
    # Tekst for vMix
    clock: str = "10:00"
    period: int = 1
    shot_clock: int = 24

    # Numerisk tid i sekunder (for lokal nedtelling)
    clock_seconds: float = 600.0    # 10:00
    shot_seconds: float = 24.0

    # RUN/flagg (fra Bodet-status)
    clock_running: bool = False
    shot_running: bool = False

    home: TeamState = field(default_factory=TeamState)
    away: TeamState = field(default_factory=TeamState)

    to_home: int = 0
    to_away: int = 0


@dataclass
class Overrides:
    # Hvis disse ikke er tomme brukes de i stedet for Bodet-navn
    home_name: str = ""
    away_name: str = ""

    # Player overrides per lag: draktnummer -> navn (for fremtidige overlays)
    players_home: Dict[int, str] = field(default_factory=dict)
    players_away: Dict[int, str] = field(default_factory=dict)

    # Flagg: bruk override-navn når de faktisk er satt
    force_team_names: bool = True
    force_player_names: bool = True


STATE = ScoreState()
OVERRIDES = Overrides()

_last_sent_score = {"home": 0, "away": 0}
state_lock = threading.Lock()

# ==========================================================
#  VMIX-KLIENT
# ==========================================================

class VmixClient:
    def __init__(self, host: str, port: int, input_name: str, fields: Dict[str, str]):
        self.host = host
        self.port = port
        self.input = input_name
        self.fields = fields
        self._last_values: Dict[str, Any] = {}

    def _set_text(self, selected_name: str, value: str):
        try:
            url = f"http://{self.host}:{self.port}/api/"
            params = {
                "Function": "SetText",
                "Input": self.input,
                "SelectedName": selected_name,
                "Value": value
            }
            requests.get(url, params=params, timeout=0.3)
        except Exception as e:
            print(f"[vMix] ERROR {selected_name}: {e}")

    def _set_image(self, selected_name: str, file_path: str):
        try:
            url = f"http://{self.host}:{self.port}/api/"
            params = {
                "Function": "SetImage",
                "Input": self.input,
                "SelectedName": selected_name,
                "Value": file_path
            }
            requests.get(url, params=params, timeout=0.3)
        except Exception as e:
            print(f"[vMix] ERROR SetImage {selected_name}: {e}")

    def update_from_state(self, state: ScoreState):
        """
        Viser Bodet-lagnavn som default.
        Bruker override-navn KUN hvis:
          - force_team_names = True, OG
          - minst ett av override-feltene faktisk har tekst.
        """
        use_overrides = (
            OVERRIDES.force_team_names and
            (OVERRIDES.home_name.strip() or OVERRIDES.away_name.strip())
        )

        if use_overrides:
            home_name = OVERRIDES.home_name.strip() or state.home.name
            away_name = OVERRIDES.away_name.strip() or state.away.name
        else:
            home_name = state.home.name
            away_name = state.away.name

        def format_period(p: int) -> str:
            mapping = {
                1: "1st",
                2: "2nd",
                3: "3rd",
                4: "4th",
            }
            return mapping.get(p, f"P{p}")

        snapshot = {
            "home_name":  home_name,
            "away_name":  away_name,
            "home_score": str(state.home.score),
            "away_score": str(state.away.score),
            "period":     format_period(state.period),
            "game_clock": state.clock,
            "shot_clock": str(state.shot_clock),
        }

        for key, value in snapshot.items():
            sel = self.fields.get(key)
            if not sel:
                continue
            last = self._last_values.get(key)
            if last != value:
                print(f"[vMix] {key} -> {sel} = {value}")
                self._set_text(sel, value)
                self._last_values[key] = value


VMIX = VmixClient(
    CONFIG["vmix_host"],
    CONFIG["vmix_port"],
    CONFIG["vmix_input"],
    CONFIG["fields"],
)

# ==========================================================
#  LAGFEIL-VISUAL (A_FAULS / B_FAULS med bilder)
# ==========================================================

def update_team_fouls_visual(team: str, fouls: int):
    team_key = team.upper()
    fouls = max(0, min(5, fouls))

    base_path = CONFIG["fouls_base_path"]
    fouls_map_all = CONFIG["fouls_files"]
    team_map = fouls_map_all.get(team_key)
    if not team_map:
        print(f"[FOULS] Ingen fouls-mapping for team '{team_key}'")
        return

    file_name = team_map.get(fouls)
    if not file_name:
        print(f"[FOULS] Ingen fil definert for {team_key} med {fouls} fouls")
        return

    file_path = os.path.join(base_path, file_name)
    selected_name = "A_FAULS.Source" if team_key == "A" else "B_FAULS.Source"

    print(f"[FOULS] {team_key} fouls={fouls} -> {selected_name} = {file_path}")
    VMIX._set_image(selected_name, file_path)

# ==========================================================
#  SCOREDEKODER
# ==========================================================

def decode_score(d1: int, d2: int, d3: int, prev: int) -> int:
    """
    Håndterer Bodet sine varianter:

      - 0 4 0  -> 4   (før 10 poeng)
      - 0 8 0  -> 8
      - 0 1 7  -> 17
      - 0 6 5  -> 65
      - 0 7 0  -> 70 (når prev >= 10)
      - 1 0 3  -> 103
    """

    # Alt null
    if d1 == 0 and d2 == 0 and d3 == 0:
        return 0

    # 0 x 0: før 10 poeng enkeltsiffer, etter 10: tiere
    if d1 == 0 and d3 == 0 and d2 > 0:
        if prev is not None and prev >= 10:
            return d2 * 10
        else:
            return d2

    # 0 0 x
    if d1 == 0 and d2 == 0 and d3 > 0:
        return d3

    # 0 x y
    if d1 == 0:
        return d2 * 10 + d3

    candidate2 = d1 * 10 + d2
    candidate3 = d1 * 100 + d2 * 10 + d3

    if prev is None:
        return candidate3 if candidate3 >= 100 else candidate2

    def is_small_step(new: int, old: int) -> bool:
        diff = new - old
        return 0 <= diff <= 3

    if prev >= 100:
        if is_small_step(candidate3, prev):
            return candidate3
        if is_small_step(candidate2, prev):
            return candidate2
        return candidate3 if abs(candidate3 - prev) < abs(candidate2 - prev) else candidate2
    else:
        if is_small_step(candidate2, prev):
            return candidate2
        if is_small_step(candidate3, prev):
            return candidate3
        return candidate2 if abs(candidate2 - prev) < abs(candidate3 - prev) else candidate3

# ==========================================================
#  BODET-PARSER – HOVEDLOGIKK
# ==========================================================

def apply_bodet_message(_msg_id: int, msg: bytes):
    global STATE, _last_sent_score

    if len(msg) < 2:
        return

    try:
        nid = (msg[0] - 48) * 10 + (msg[1] - 48)
    except Exception:
        return

    with state_lock:
        # 18 – hovedklokke / timeouts / periode
        if nid == 18:
            print("[BODET 18] len=", len(msg),
                  "data=", " ".join(f"{b:02X}" for b in msg))

            if len(msg) < 8:
                return

            status = msg[2]
            running = bool(status & 0x02)  # bit 1 = RUN

            # mm:ss
            minutes = dig(msg[4]) * 10 + dig(msg[5])
            seconds = dig(msg[6]) * 10 + dig(msg[7])
            STATE.clock_seconds = float(minutes * 60 + seconds)
            STATE.clock_running = running
            STATE.clock = f"{minutes:02d}:{seconds:02d}"

            # timeouts (hvis tilstede)
            if len(msg) >= 10:
                STATE.to_home = dig(msg[8])
                STATE.to_away = dig(msg[9])

            # periode (typisk index 12, fallback 13)
            period = 0
            if len(msg) > 12:
                period = dig(msg[12])
            if period == 0 and len(msg) > 13:
                period = dig(msg[13])

            if period > 0:
                if period != STATE.period:
                    STATE.home.period_fouls = 0
                    STATE.away.period_fouls = 0
                STATE.period = period

            VMIX.update_from_state(STATE)

        # 30 – lag-score
        elif nid == 30:
            if len(msg) < 9:
                return

            # Hjemmelag
            h1 = dig(msg[3])
            h2 = dig(msg[4])
            h3 = dig(msg[5])

            # Bortelag
            a1 = dig(msg[6])
            a2 = dig(msg[7])
            a3 = dig(msg[8])

            nh = decode_score(h1, h2, h3, _last_sent_score["home"])
            na = decode_score(a1, a2, a3, _last_sent_score["away"])

            STATE.home.score = nh
            STATE.away.score = na

            if nh != _last_sent_score["home"]:
                diff = nh - _last_sent_score["home"]
                _last_sent_score["home"] = nh
                print(f"[EVENT] HOME SCORE +{diff} -> {nh}")

            if na != _last_sent_score["away"]:
                diff = na - _last_sent_score["away"]
                _last_sent_score["away"] = na
                print(f"[EVENT] AWAY SCORE +{diff} -> {na}")

            VMIX.update_from_state(STATE)

        # 31 – lagfeil
        elif nid == 31:
            if len(msg) < 7:
                return

            STATE.home.fouls = dig(msg[4])
            STATE.away.fouls = dig(msg[6])

            STATE.home.period_fouls = max(STATE.home.period_fouls, STATE.home.fouls)
            STATE.away.period_fouls = max(STATE.away.period_fouls, STATE.away.fouls)

            print(f"[EVENT] TEAM FOULS: H={STATE.home.fouls} A={STATE.away.fouls}")

            update_team_fouls_visual("A", STATE.home.fouls)
            update_team_fouls_visual("B", STATE.away.fouls)

            VMIX.update_from_state(STATE)

        # 36 – siste minutt, tideler (0:ss.t)
        elif nid == 36:
            if len(msg) < 5:
                return
            status = msg[2]
            running = bool(status & 0x02)

            seconds = dig(msg[2]) * 10 + dig(msg[3])
            tenths = dig(msg[4])
            STATE.clock_seconds = float(seconds) + tenths / 10.0
            STATE.clock_running = running
            STATE.clock = f"0:{seconds:02d}.{tenths}"
            VMIX.update_from_state(STATE)

        # 50 – shot clock
        elif nid == 50:
            if len(msg) < 5:
                return
            status = msg[2]
            running = bool(status & 0x02)

            shot = dig(msg[3]) * 10 + dig(msg[4])
            STATE.shot_seconds = float(shot)
            STATE.shot_running = running
            STATE.shot_clock = shot
            VMIX.update_from_state(STATE)

        # 98/99 – lagnavn
        elif nid == 98:
            name_bytes = msg[2:20]
            try:
                STATE.home.name = name_bytes.decode(errors="ignore").strip()
            except Exception:
                pass
            VMIX.update_from_state(STATE)

        elif nid == 99:
            name_bytes = msg[2:20]
            try:
                STATE.away.name = name_bytes.decode(errors="ignore").strip()
            except Exception:
                pass
            VMIX.update_from_state(STATE)

# ==========================================================
#  LOKAL NEDTELLING FOR KLOKKE / SHOTCLOCK
# ==========================================================

def clock_ticker():
    last = time.monotonic()
    while True:
        time.sleep(0.1)
        now = time.monotonic()
        dt = now - last
        last = now

        with state_lock:
            updated = False

            # Kampklokke
            if STATE.clock_running and STATE.clock_seconds > 0:
                STATE.clock_seconds = max(0.0, STATE.clock_seconds - dt)

                if STATE.clock_seconds >= 60.0:
                    m = int(STATE.clock_seconds // 60)
                    s = int(STATE.clock_seconds % 60)
                    new_clock_str = f"{m:02d}:{s:02d}"
                else:
                    total_tenths = int(round(STATE.clock_seconds * 10))
                    if total_tenths < 0:
                        total_tenths = 0
                    sec = total_tenths // 10
                    tenth = total_tenths % 10
                    if sec > 59:
                        sec = 59
                    new_clock_str = f"0:{sec:02d}.{tenth}"

                if new_clock_str != STATE.clock:
                    STATE.clock = new_clock_str
                    updated = True

            # Skuddklokke
            if STATE.shot_running and STATE.shot_seconds > 0:
                STATE.shot_seconds = max(0.0, STATE.shot_seconds - dt)
                new_shot = max(0, int(round(STATE.shot_seconds)))
                if new_shot != STATE.shot_clock:
                    STATE.shot_clock = new_shot
                    updated = True

            if updated:
                VMIX.update_from_state(STATE)

# ==========================================================
#  TCP-PARSING
# ==========================================================

def parse_stream_and_apply(conn: socket.socket):
    buffer = b""
    SOH, STX, ETX = 0x01, 0x02, 0x03

    print("[TCP] Klar til å motta data fra Scorepad ...")

    while True:
        data = conn.recv(1024)
        if not data:
            print("[TCP] Scorepad koblet fra (recv=0 bytes)")
            break

        print(f"[TCP] Mottok {len(data)} bytes: {data!r}")
        buffer += data

        while True:
            start = buffer.find(bytes([SOH]))
            if start == -1:
                buffer = b""
                break
            end = buffer.find(bytes([ETX]), start + 1)
            if end == -1:
                if start > 0:
                    buffer = buffer[start:]
                break

            frame = buffer[start:end + 1]
            buffer = buffer[end + 1:]

            stx_index = frame.find(bytes([STX]), 1, -1)
            if stx_index == -1:
                print("[PARSE] Fant SOH/ETX men ingen STX, skipper frame")
                continue
            if stx_index + 2 >= len(frame) - 1:
                print("[PARSE] Frame for kort til nyttelast, skipper")
                continue

            payload = frame[stx_index + 2:-1]
            if len(payload) >= 2:
                try:
                    nid = (payload[0] - 48) * 10 + (payload[1] - 48)
                except Exception:
                    nid = -1
                print(f"[RAW] nid={nid} len={len(payload)} payload={payload!r}")
            else:
                print(f"[RAW] payload for kort: {payload!r}")

            apply_bodet_message(0, payload)


def start_bodet_server():
    host = CONFIG["listen_host"]
    port = CONFIG["listen_port"]

    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((host, port))
    srv.listen(1)

    print(f"[TCP] Lytter på {host}:{port} for Scorepad (Protocol TV) ...")

    while True:
        print("[TCP] Venter på tilkobling fra Scorepad ...")
        try:
            conn, addr = srv.accept()
        except Exception as e:
            print(f"[TCP] accept() FEIL: {e}")
            continue

        print(f"[TCP] Scorepad tilkoblet fra {addr}")
        try:
            parse_stream_and_apply(conn)
        except Exception as e:
            print(f"[TCP] ERROR i parse_stream_and_apply: {e}")
        finally:
            conn.close()
            print("[TCP] Forbindelse lukket, venter på ny ...")

# ==========================================================
#  GUI FOR OVERRIDES
# ==========================================================

class OverrideGUI:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title("Bodet → vMix gateway")

        self._build_widgets()

    def _build_widgets(self):
        frm = ttk.Frame(self.root, padding=10)
        frm.grid(row=0, column=0, sticky="nsew")

        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(0, weight=1)

        # Team name overrides
        ttk.Label(frm, text="Home team name (TV):").grid(row=0, column=0, sticky="w")
        self.home_name_var = tk.StringVar(value=OVERRIDES.home_name)
        ttk.Entry(frm, textvariable=self.home_name_var, width=25).grid(row=0, column=1, sticky="ew")

        ttk.Label(frm, text="Away team name (TV):").grid(row=1, column=0, sticky="w")
        self.away_name_var = tk.StringVar(value=OVERRIDES.away_name)
        ttk.Entry(frm, textvariable=self.away_name_var, width=25).grid(row=1, column=1, sticky="ew")

        self.force_team_var = tk.BooleanVar(value=OVERRIDES.force_team_names)
        ttk.Checkbutton(frm, text="Use custom team names", variable=self.force_team_var)\
            .grid(row=2, column=0, columnspan=2, sticky="w", pady=(0, 10))

        # Player overrides (for fremtidig bruk)
        sep = ttk.Separator(frm, orient="horizontal")
        sep.grid(row=3, column=0, columnspan=3, sticky="ew", pady=5)

        ttk.Label(frm, text="Player #").grid(row=4, column=0, sticky="w")
        self.player_num_var = tk.StringVar()
        ttk.Entry(frm, textvariable=self.player_num_var, width=5).grid(row=4, column=1, sticky="w")

        ttk.Label(frm, text="Player name (æøå ok):").grid(row=5, column=0, sticky="w")
        self.player_name_var = tk.StringVar()
        ttk.Entry(frm, textvariable=self.player_name_var, width=25).grid(row=5, column=1, sticky="ew")

        btn_home = ttk.Button(frm, text="Set HOME player", command=self.set_home_player)
        btn_home.grid(row=6, column=0, pady=2, sticky="w")

        btn_away = ttk.Button(frm, text="Set AWAY player", command=self.set_away_player)
        btn_away.grid(row=6, column=1, pady=2, sticky="w")

        self.force_player_var = tk.BooleanVar(value=OVERRIDES.force_player_names)
        ttk.Checkbutton(frm, text="Use custom player names (for future overlays)",
                        variable=self.force_player_var)\
            .grid(row=7, column=0, columnspan=2, sticky="w")

        # Listbokser
        ttk.Label(frm, text="HOME overrides:").grid(row=8, column=0, sticky="w", pady=(10, 0))
        self.list_home = tk.Listbox(frm, height=6, width=30)
        self.list_home.grid(row=9, column=0, columnspan=2, sticky="ew")

        ttk.Label(frm, text="AWAY overrides:").grid(row=10, column=0, sticky="w", pady=(10, 0))
        self.list_away = tk.Listbox(frm, height=6, width=30)
        self.list_away.grid(row=11, column=0, columnspan=2, sticky="ew")

        ttk.Button(frm, text="Apply names now", command=self.apply_names).grid(
            row=12, column=0, columnspan=2, pady=10
        )

        self.refresh_lists()

    def refresh_lists(self):
        self.list_home.delete(0, tk.END)
        for num, name in sorted(OVERRIDES.players_home.items()):
            self.list_home.insert(tk.END, f"{num}: {name}")

        self.list_away.delete(0, tk.END)
        for num, name in sorted(OVERRIDES.players_away.items()):
            self.list_away.insert(tk.END, f"{num}: {name}")

    def set_home_player(self):
        try:
            num = int(self.player_num_var.get())
        except ValueError:
            return
        name = self.player_name_var.get().strip()
        if not name:
            return
        OVERRIDES.players_home[num] = name
        self.refresh_lists()

    def set_away_player(self):
        try:
            num = int(self.player_num_var.get())
        except ValueError:
            return
        name = self.player_name_var.get().strip()
        if not name:
            return
        OVERRIDES.players_away[num] = name
        self.refresh_lists()

    def apply_names(self):
        OVERRIDES.home_name = self.home_name_var.get().strip()
        OVERRIDES.away_name = self.away_name_var.get().strip()
        OVERRIDES.force_team_names = self.force_team_var.get()
        OVERRIDES.force_player_names = self.force_player_var.get()

        with state_lock:
            VMIX.update_from_state(STATE)

    def run(self):
        self.root.mainloop()

# ==========================================================
#  DEBUG-PRINTER
# ==========================================================

def debug_printer():
    while True:
        time.sleep(5)
        with state_lock:
            print("\n--- STATE ---")
            print(
                f"CLOCK: {STATE.clock}  (period {STATE.period})  "
                f"SHOT: {STATE.shot_clock}  RUN: {'Y' if STATE.clock_running else 'N'}  "
                f"SHOT_RUN: {'Y' if STATE.shot_running else 'N'}"
            )
            print(
                f"HOME: {STATE.home.name}  {STATE.home.score} pts  "
                f"F:{STATE.home.fouls}  TO:{STATE.to_home}"
            )
            print(
                f"AWAY: {STATE.away.name}  {STATE.away.score} pts  "
                f"F:{STATE.away.fouls}  TO:{STATE.to_away}"
            )

# ==========================================================
#  MAIN
# ==========================================================

if __name__ == "__main__":
    print("[MAIN] Bodet → vMix gateway m/GUI starter...")

    # Init: nullstill fouls-grafikk ved start (0 feil)
    update_team_fouls_visual("A", 0)
    update_team_fouls_visual("B", 0)

    threading.Thread(target=debug_printer, daemon=True).start()
    threading.Thread(target=start_bodet_server, daemon=True).start()
    threading.Thread(target=clock_ticker, daemon=True).start()

    gui = OverrideGUI()
    gui.run()

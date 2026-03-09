#!/usr/bin/env python3
"""
Sinhala Courtroom Audio Diarization Client
Raspberry Pi UI for recording, transcribing, editing, and exporting court sessions.
Includes I2C LCD status display support via RPLCD.
"""

import tkinter as tk
from tkinter import ttk, messagebox, filedialog
import threading
import queue
import wave
import json
import os
import io
import time
import datetime
import tempfile
import pyaudio
import numpy as np
import requests
from pathlib import Path

# PDF export
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import cm
from reportlab.lib import colors
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, HRFlowable
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont

# ── Configuration ────────────────────────────────────────────────────────────
API_URL = os.environ.get("DIARIZATION_API_URL", "http://localhost:8000")
API_KEY = os.environ.get("SINLLAMA_API_KEY", "sinllama-default-key-change-me")
SESSIONS_DIR = Path(os.environ.get("SESSIONS_DIR", "./sessions"))
SESSIONS_DIR.mkdir(parents=True, exist_ok=True)

# I2C LCD configuration
LCD_ENABLED   = os.environ.get("LCD_ENABLED", "true").lower() == "true"
LCD_I2C_ADDR  = int(os.environ.get("LCD_I2C_ADDRESS", "0x27"), 16)
LCD_COLS      = int(os.environ.get("LCD_COLS", "16"))
LCD_ROWS      = int(os.environ.get("LCD_ROWS", "2"))

# Audio settings
SAMPLE_RATE = 16000                                               # target rate for the diarization API
RECORD_RATE = int(os.environ.get("RECORD_SAMPLE_RATE", "44100"))  # native mic rate (common: 44100, 48000)
CHANNELS = 1
FORMAT = pyaudio.paInt16
CHUNK = 1024

# Audio device selection (run with list_audio_devices() to find indices)
_idev = os.environ.get("INPUT_DEVICE_INDEX")
_odev = os.environ.get("OUTPUT_DEVICE_INDEX")
INPUT_DEVICE_INDEX  = int(_idev) if _idev is not None else None
OUTPUT_DEVICE_INDEX = int(_odev) if _odev is not None else None

# Speaker colors for UI
SPEAKER_COLORS = {
    "SPEAKER_00": "#1a73e8",
    "SPEAKER_01": "#e84e1a",
    "SPEAKER_02": "#1ae87a",
    "SPEAKER_03": "#e8d91a",
}

SPEAKER_LABELS = {
    "SPEAKER_00": "දිසාපති (Judge)",
    "SPEAKER_01": "නීතිඥ 1 (Counsel 1)",
    "SPEAKER_02": "නීතිඥ 2 (Counsel 2)",
    "SPEAKER_03": "සාක්ෂිකරු (Witness)",
}

# ── Sinhala font support ─────────────────────────────────────────────────────
SINHALA_FONT_PATH = os.environ.get("SINHALA_FONT_PATH", "./fonts/NotoSansSinhala-Regular.ttf")
SINHALA_FONT_BOLD = os.environ.get("SINHALA_FONT_BOLD", "./fonts/NotoSansSinhala-Bold.ttf")


def register_sinhala_font():
    """Register Sinhala font for PDF generation."""
    try:
        if os.path.exists(SINHALA_FONT_PATH):
            pdfmetrics.registerFont(TTFont("NotoSinhala", SINHALA_FONT_PATH))
            if os.path.exists(SINHALA_FONT_BOLD):
                pdfmetrics.registerFont(TTFont("NotoSinhala-Bold", SINHALA_FONT_BOLD))
            return True
    except Exception:
        pass
    return False


# ── I2C LCD Display ───────────────────────────────────────────────────────────

class LCDDisplay:
    """
    Wrapper around RPLCD CharLCD over I2C.
    Falls back to console logging if hardware / library is unavailable,
    so the app runs on non-Pi machines without changes.

    Supports both 16x2 and 20x4 LCDs (set LCD_COLS / LCD_ROWS env vars).

    Typical I2C addresses:  0x27  (PCF8574)  or  0x3F  (PCF8574A)
    """

    # Custom char indices stored in LCD CGRAM
    _CHAR_REC  = 0   # filled circle  ●
    _CHAR_TICK = 1   # tick mark      ✓
    _CHAR_NOTE = 2   # eighth note

    # 5×8 pixel bitmaps for custom characters
    _BITMAP_REC  = [0x00, 0x0E, 0x1F, 0x1F, 0x1F, 0x0E, 0x00, 0x00]
    _BITMAP_TICK = [0x00, 0x01, 0x03, 0x16, 0x1C, 0x08, 0x00, 0x00]
    _BITMAP_NOTE = [0x01, 0x03, 0x05, 0x09, 0x09, 0x0B, 0x1B, 0x18]

    def __init__(self):
        self._lcd = None
        self._lock = threading.Lock()
        self._blink_thread = None
        self._blink_stop = threading.Event()

        if not LCD_ENABLED:
            print("[LCD] Disabled via config.")
            return
        try:
            from RPLCD.i2c import CharLCD
            self._lcd = CharLCD(
                i2c_expander="PCF8574",
                address=LCD_I2C_ADDR,
                port=1,
                cols=LCD_COLS,
                rows=LCD_ROWS,
                dotsize=8,
                auto_linebreaks=False,
            )
            self._lcd.create_char(self._CHAR_REC,  self._BITMAP_REC)
            self._lcd.create_char(self._CHAR_TICK, self._BITMAP_TICK)
            self._lcd.create_char(self._CHAR_NOTE, self._BITMAP_NOTE)
            self._lcd.clear()
            self._write("SinLlama Court", "Initialising...")
            print(f"[LCD] Initialised at I2C 0x{LCD_I2C_ADDR:02X}, {LCD_COLS}x{LCD_ROWS}")
        except ImportError:
            print("[LCD] RPLCD not installed — running without LCD.")
        except Exception as e:
            print(f"[LCD] Init failed ({e}) — running without LCD.")

    # ── Public state methods ──────────────────────────────────────────────────

    def show_ready(self):
        self._stop_blink()
        self._write("SinLlama Court  ", "Ready           ")

    def show_recording(self):
        """Start a blinking ● REC animation updated every second."""
        self._stop_blink()
        self._blink_stop.clear()
        self._blink_thread = threading.Thread(
            target=self._blink_rec, daemon=True
        )
        self._blink_thread.start()

    def update_timer(self, elapsed_seconds: int):
        """Called every second during recording to refresh timer on LCD."""
        if not self._lcd:
            return
        m, s = divmod(elapsed_seconds, 60)
        line2 = f"Time: {m:02d}:{s:02d}     "[:LCD_COLS]
        with self._lock:
            try:
                self._lcd.cursor_pos = (1, 0)
                self._lcd.write_string(line2)
            except Exception:
                pass

    def show_saved(self, duration_s: float):
        self._stop_blink()
        dur = f"{duration_s:.1f}s"
        self._write(
            chr(self._CHAR_TICK) + " Rec Saved      ",
            f"Duration: {dur}     ",
        )

    def show_uploading(self):
        self._stop_blink()
        self._write("Processing...   ", "Uploading audio ")

    def show_analysing(self):
        self._write("Processing...   ", "Analysing AI... ")

    def show_result(self, num_speakers: int, accuracy: float):
        self._stop_blink()
        spk = f"Spkrs:{num_speakers}"
        acc = f"Acc:{accuracy:.1f}%"
        line1 = (chr(self._CHAR_TICK) + " Done! " + spk)[:LCD_COLS]
        line2 = acc[:LCD_COLS]
        # Extra lines for 20x4
        if LCD_ROWS >= 4 and self._lcd:
            with self._lock:
                try:
                    self._lcd.clear()
                    self._lcd.cursor_pos = (0, 0)
                    self._lcd.write_string(line1.ljust(LCD_COLS))
                    self._lcd.cursor_pos = (1, 0)
                    self._lcd.write_string(line2.ljust(LCD_COLS))
                    self._lcd.cursor_pos = (2, 0)
                    self._lcd.write_string("Transcript ready".ljust(LCD_COLS))
                    self._lcd.cursor_pos = (3, 0)
                    self._lcd.write_string("Export: see UI  ".ljust(LCD_COLS))
                except Exception:
                    pass
            return
        self._write(line1, line2)

    def show_playing(self):
        self._write(
            chr(self._CHAR_NOTE) + " Playing Audio  ",
            "                ",
        )

    def show_play_done(self):
        self._write("SinLlama Court  ", "Playback done   ")

    def show_exporting(self):
        self._write("Exporting PDF...", "Please wait...  ")

    def show_exported(self, filename: str):
        short = filename[-LCD_COLS:] if len(filename) > LCD_COLS else filename.ljust(LCD_COLS)
        self._write("PDF Exported!   ", short)

    def show_session_loaded(self, case_no: str):
        label = (case_no or "---")[:LCD_COLS - 9]
        self._write("Session Loaded  ", f"Case: {label}".ljust(LCD_COLS))

    def show_error(self, short_msg: str = ""):
        self._stop_blink()
        line2 = short_msg[:LCD_COLS].ljust(LCD_COLS) if short_msg else "Check screen... "
        self._write("! ERROR !       ", line2)

    def show_case_info(self, case_number: str, court_name: str):
        """Display on rows 2+3 of a 20x4 LCD when idle."""
        if not self._lcd or LCD_ROWS < 4:
            return
        with self._lock:
            try:
                self._lcd.cursor_pos = (2, 0)
                self._lcd.write_string(f"Case:{case_number}"[:LCD_COLS].ljust(LCD_COLS))
                self._lcd.cursor_pos = (3, 0)
                self._lcd.write_string(court_name[:LCD_COLS].ljust(LCD_COLS))
            except Exception:
                pass

    def clear(self):
        self._stop_blink()
        if self._lcd:
            with self._lock:
                try:
                    self._lcd.clear()
                except Exception:
                    pass

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _write(self, line1: str, line2: str = ""):
        """Write up to two lines, padded to LCD_COLS chars."""
        if not self._lcd:
            print(f"[LCD] {line1.strip()} | {line2.strip()}")
            return
        with self._lock:
            try:
                self._lcd.clear()
                self._lcd.cursor_pos = (0, 0)
                self._lcd.write_string(line1[:LCD_COLS].ljust(LCD_COLS))
                if LCD_ROWS >= 2 and line2:
                    self._lcd.cursor_pos = (1, 0)
                    self._lcd.write_string(line2[:LCD_COLS].ljust(LCD_COLS))
            except Exception as e:
                print(f"[LCD] Write error: {e}")

    def _blink_rec(self):
        """Alternates ● REC / empty on line 1 while recording."""
        toggle = True
        while not self._blink_stop.is_set():
            icon = chr(self._CHAR_REC) if toggle else " "
            line1 = f"{icon} RECORDING      "[:LCD_COLS]
            if self._lcd:
                with self._lock:
                    try:
                        self._lcd.cursor_pos = (0, 0)
                        self._lcd.write_string(line1.ljust(LCD_COLS))
                    except Exception:
                        pass
            toggle = not toggle
            self._blink_stop.wait(timeout=0.8)

    def _stop_blink(self):
        self._blink_stop.set()
        if self._blink_thread and self._blink_thread.is_alive():
            self._blink_thread.join(timeout=1.5)
        self._blink_thread = None


# ── Session Storage ──────────────────────────────────────────────────────────

class Session:
    def __init__(self, session_id=None, case_number="", judge_name="", court_name=""):
        self.session_id = session_id or datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        self.case_number = case_number
        self.judge_name = judge_name
        self.court_name = court_name
        self.created_at = datetime.datetime.now().isoformat()
        self.audio_file = None
        self.result = None
        self.edited_segments = []  # user-edited text

    def save(self):
        path = SESSIONS_DIR / f"{self.session_id}.json"
        data = {
            "session_id": self.session_id,
            "case_number": self.case_number,
            "judge_name": self.judge_name,
            "court_name": self.court_name,
            "created_at": self.created_at,
            "audio_file": self.audio_file,
            "result": self.result,
            "edited_segments": self.edited_segments,
        }
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    @classmethod
    def load(cls, session_id):
        path = SESSIONS_DIR / f"{session_id}.json"
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        s = cls(session_id=data["session_id"])
        s.__dict__.update(data)
        return s

    @classmethod
    def list_sessions(cls):
        sessions = []
        for p in sorted(SESSIONS_DIR.glob("*.json"), reverse=True):
            try:
                with open(p, encoding="utf-8") as f:
                    data = json.load(f)
                sessions.append(data)
            except Exception:
                pass
        return sessions


# ── Audio Recorder ───────────────────────────────────────────────────────────

class AudioRecorder:
    def __init__(self):
        self.pa = pyaudio.PyAudio()
        self.stream = None
        self.frames = []
        self.recording = False
        self.duration = 0.0

    def start(self):
        self.frames = []
        self.recording = True
        self.start_time = time.time()
        self.stream = self.pa.open(
            format=FORMAT,
            channels=CHANNELS,
            rate=RECORD_RATE,
            input=True,
            input_device_index=INPUT_DEVICE_INDEX,
            frames_per_buffer=CHUNK,
            stream_callback=self._callback,
        )
        self.stream.start_stream()

    def _callback(self, in_data, frame_count, time_info, status):
        if self.recording:
            self.frames.append(in_data)
        return (None, pyaudio.paContinue)

    def stop(self):
        self.recording = False
        self.duration = time.time() - self.start_time
        if self.stream:
            self.stream.stop_stream()
            self.stream.close()
            self.stream = None

    def save(self, path):
        """Save raw audio at native RECORD_RATE — no processing applied."""
        with wave.open(path, "wb") as wf:
            wf.setnchannels(CHANNELS)
            wf.setsampwidth(self.pa.get_sample_size(FORMAT))
            wf.setframerate(RECORD_RATE)
            wf.writeframes(b"".join(self.frames))

    def __del__(self):
        self.pa.terminate()


# ── Audio Player ─────────────────────────────────────────────────────────────

class AudioPlayer:
    def __init__(self):
        self.pa = pyaudio.PyAudio()
        self._thread = None
        self.playing = False

    def play(self, path, on_done=None):
        if self.playing:
            self.stop()
        self.playing = True
        self._thread = threading.Thread(target=self._play_thread, args=(path, on_done), daemon=True)
        self._thread.start()

    def _play_thread(self, path, on_done):
        try:
            with wave.open(path, "rb") as wf:
                stream = self.pa.open(
                    format=self.pa.get_format_from_width(wf.getsampwidth()),
                    channels=wf.getnchannels(),
                    rate=wf.getframerate(),
                    output=True,
                    output_device_index=OUTPUT_DEVICE_INDEX,
                )
                data = wf.readframes(CHUNK)
                while data and self.playing:
                    stream.write(data)
                    data = wf.readframes(CHUNK)
                stream.stop_stream()
                stream.close()
        except Exception as e:
            print(f"Playback error: {e}")
        finally:
            self.playing = False
            if on_done:
                on_done()

    def stop(self):
        self.playing = False
        if self._thread:
            self._thread.join(timeout=1)

    def __del__(self):
        self.pa.terminate()


# ── API Client ───────────────────────────────────────────────────────────────

def _resample_wav_to_bytes(audio_path, target_rate=SAMPLE_RATE):
    """Read a WAV file and return in-memory bytes resampled to target_rate."""
    with wave.open(audio_path, "rb") as wf:
        src_rate = wf.getframerate()
        src_channels = wf.getnchannels()
        raw = wf.readframes(wf.getnframes())

    samples = np.frombuffer(raw, dtype=np.int16).astype(np.float32)

    # Mix down to mono if stereo
    if src_channels > 1:
        samples = samples.reshape(-1, src_channels).mean(axis=1)

    # Resample if needed
    if src_rate != target_rate:
        target_len = int(len(samples) * target_rate / src_rate)
        samples = np.interp(
            np.linspace(0, len(samples) - 1, target_len),
            np.arange(len(samples)),
            samples,
        )

    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(target_rate)
        wf.writeframes(samples.astype(np.int16).tobytes())
    buf.seek(0)
    return buf


def submit_audio(audio_path, on_progress=None):
    """Send audio file to diarization endpoint and return result JSON."""
    headers = {"X-API-Key": API_KEY}
    if on_progress:
        on_progress("Uploading audio...")
    api_buf = _resample_wav_to_bytes(audio_path, target_rate=SAMPLE_RATE)
    files = {"audio": (os.path.basename(audio_path), api_buf, "audio/wav")}
    resp = requests.post(
        f"{API_URL}/diarize",
        headers=headers,
        files=files,
        timeout=300,
    )
    resp.raise_for_status()
    return resp.json()


# ── PDF Export ───────────────────────────────────────────────────────────────

def export_pdf(session: Session, output_path: str):
    has_sinhala_font = register_sinhala_font()
    body_font = "NotoSinhala" if has_sinhala_font else "Helvetica"
    bold_font = "NotoSinhala-Bold" if has_sinhala_font else "Helvetica-Bold"

    doc = SimpleDocTemplate(
        output_path,
        pagesize=A4,
        rightMargin=2 * cm,
        leftMargin=2 * cm,
        topMargin=2 * cm,
        bottomMargin=2 * cm,
    )

    styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        "Title", fontName=bold_font, fontSize=16, alignment=1, spaceAfter=6
    )
    subtitle_style = ParagraphStyle(
        "Subtitle", fontName=body_font, fontSize=11, alignment=1, spaceAfter=4, textColor=colors.grey
    )
    meta_style = ParagraphStyle(
        "Meta", fontName=body_font, fontSize=10, spaceAfter=3
    )
    speaker_style = ParagraphStyle(
        "Speaker", fontName=bold_font, fontSize=10, spaceAfter=1, textColor=colors.HexColor("#1a73e8")
    )
    text_style = ParagraphStyle(
        "Text", fontName=body_font, fontSize=11, spaceAfter=8, leading=18
    )
    small_style = ParagraphStyle(
        "Small", fontName=body_font, fontSize=8, textColor=colors.grey, spaceAfter=6
    )

    story = []

    # Header
    story.append(Paragraph("ශ්‍රී ලංකා අධිකරණ ශ්‍රව්‍ය ලේඛනය", title_style))
    story.append(Paragraph("Sri Lanka Court Audio Transcript", subtitle_style))
    story.append(HRFlowable(width="100%", thickness=1, color=colors.HexColor("#cccccc")))
    story.append(Spacer(1, 0.3 * cm))

    # Metadata table
    meta_data = [
        ["නඩු අංකය / Case No:", session.case_number or "—"],
        ["අධිකරණය / Court:", session.court_name or "—"],
        ["දිසාපති / Judge:", session.judge_name or "—"],
        ["දිනය / Date:", session.created_at[:10] if session.created_at else "—"],
        ["වේලාව / Time:", session.created_at[11:19] if session.created_at else "—"],
    ]
    meta_table = Table(meta_data, colWidths=[5 * cm, 12 * cm])
    meta_table.setStyle(TableStyle([
        ("FONTNAME", (0, 0), (0, -1), bold_font),
        ("FONTNAME", (1, 0), (1, -1), body_font),
        ("FONTSIZE", (0, 0), (-1, -1), 10),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("TEXTCOLOR", (0, 0), (0, -1), colors.HexColor("#555555")),
    ]))
    story.append(meta_table)
    story.append(Spacer(1, 0.4 * cm))

    # Statistics
    if session.result:
        r = session.result
        stat_data = [
            ["වාක්‍ය ගණන", "කථිකයන් ගණන", "නිරවද්‍යතාව", "කාලය"],
            [
                str(r.get("total_segments", "—")),
                str(r.get("num_speakers", "—")),
                f"{r.get('overall_accuracy_pct', 0):.1f}%",
                f"{r.get('audio_duration_s', 0):.1f}s",
            ],
        ]
        stat_table = Table(stat_data, colWidths=[4.25 * cm] * 4)
        stat_table.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1a73e8")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("FONTNAME", (0, 0), (-1, 0), bold_font),
            ("FONTNAME", (0, 1), (-1, 1), body_font),
            ("FONTSIZE", (0, 0), (-1, -1), 10),
            ("ALIGN", (0, 0), (-1, -1), "CENTER"),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
            ("TOPPADDING", (0, 0), (-1, -1), 6),
            ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#dddddd")),
        ]))
        story.append(stat_table)
        story.append(Spacer(1, 0.5 * cm))

    story.append(HRFlowable(width="100%", thickness=1, color=colors.HexColor("#cccccc")))
    story.append(Spacer(1, 0.3 * cm))
    story.append(Paragraph("ශ්‍රව්‍ය ලේඛනය / Transcript", ParagraphStyle(
        "SectionTitle", fontName=bold_font, fontSize=13, spaceAfter=10
    )))

    # Segments
    segments = session.result.get("segments", []) if session.result else []
    edited = {i: t for i, t in enumerate(session.edited_segments) if t}

    for i, seg in enumerate(segments):
        speaker = seg.get("speaker", "UNKNOWN")
        label = SPEAKER_LABELS.get(speaker, speaker)
        start = seg.get("start", 0)
        end = seg.get("end", 0)
        accuracy = seg.get("segment_accuracy_pct", 0)

        # Use edited text if available, else AI-corrected, else raw
        text = edited.get(i) or seg.get("ai_corrected_text") or seg.get("asr_text", "")

        color = SPEAKER_COLORS.get(speaker, "#333333")
        sp_style = ParagraphStyle(
            f"Sp_{i}", fontName=bold_font, fontSize=10, spaceAfter=1,
            textColor=colors.HexColor(color)
        )
        story.append(Paragraph(f"{label}", sp_style))
        story.append(Paragraph(
            f"[{start:.1f}s – {end:.1f}s] | නිරවද්‍යතාව: {accuracy:.1f}%",
            small_style,
        ))
        story.append(Paragraph(text, text_style))

    # Footer note
    story.append(HRFlowable(width="100%", thickness=0.5, color=colors.HexColor("#cccccc")))
    story.append(Spacer(1, 0.2 * cm))
    story.append(Paragraph(
        f"Generated by SinLlama Courtroom Diarization System | {datetime.datetime.now().strftime('%Y-%m-%d %H:%M')}",
        ParagraphStyle("Footer", fontName=body_font, fontSize=8, textColor=colors.grey, alignment=1),
    ))

    doc.build(story)


# ── Main Application ─────────────────────────────────────────────────────────

class CourtroomApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("SinLlama — Courtroom Diarization System")
        self.geometry("1024x768")
        self.configure(bg="#f0f4f8")
        self.resizable(True, True)

        self.recorder = AudioRecorder()
        self.player = AudioPlayer()
        self.lcd = LCDDisplay()
        self.session: Session | None = None
        self.timer_running = False
        self._timer_id = None
        self._elapsed = 0
        self._queue = queue.Queue()
        self.segment_editors = []  # list of tk.Text widgets per segment

        self._build_ui()
        self._poll_queue()
        self.lcd.show_ready()

    # ── UI Build ─────────────────────────────────────────────────────────────

    def _build_ui(self):
        # Top bar
        top = tk.Frame(self, bg="#1a73e8", pady=10)
        top.pack(fill="x")
        tk.Label(top, text="⚖ SinLlama Courtroom Diarization", font=("Helvetica", 18, "bold"),
                 bg="#1a73e8", fg="white").pack(side="left", padx=20)
        tk.Button(top, text="📂 Sessions", command=self._open_sessions,
                  bg="#0d5abf", fg="white", relief="flat", padx=10).pack(side="right", padx=10)

        # Notebook tabs
        self.nb = ttk.Notebook(self)
        self.nb.pack(fill="both", expand=True, padx=10, pady=5)

        self.record_tab = tk.Frame(self.nb, bg="#f0f4f8")
        self.transcript_tab = tk.Frame(self.nb, bg="#f0f4f8")
        self.nb.add(self.record_tab, text="  🎙 Record  ")
        self.nb.add(self.transcript_tab, text="  📝 Transcript  ")

        self._build_record_tab()
        self._build_transcript_tab()

        # Status bar
        self.status_var = tk.StringVar(value="Ready")
        status_bar = tk.Label(self, textvariable=self.status_var, bg="#dde3ea",
                              anchor="w", padx=10, font=("Helvetica", 9))
        status_bar.pack(fill="x", side="bottom")

    def _build_record_tab(self):
        tab = self.record_tab

        # Case info frame
        info_frame = tk.LabelFrame(tab, text=" Session Info ", bg="#f0f4f8",
                                   font=("Helvetica", 10, "bold"), pady=8)
        info_frame.pack(fill="x", padx=20, pady=(15, 5))

        fields = [
            ("නඩු අංකය / Case No:", "case_number"),
            ("අධිකරණය / Court:", "court_name"),
            ("දිසාපති / Judge:", "judge_name"),
        ]
        self._info_vars = {}
        for row, (label, key) in enumerate(fields):
            tk.Label(info_frame, text=label, bg="#f0f4f8", font=("Helvetica", 10)).grid(
                row=row, column=0, sticky="w", padx=10, pady=3)
            var = tk.StringVar()
            self._info_vars[key] = var
            tk.Entry(info_frame, textvariable=var, width=35, font=("Helvetica", 10)).grid(
                row=row, column=1, padx=10, pady=3, sticky="w")

        # Timer display
        timer_frame = tk.Frame(tab, bg="#f0f4f8")
        timer_frame.pack(pady=20)
        self.timer_label = tk.Label(timer_frame, text="00:00", font=("Helvetica", 60, "bold"),
                                    bg="#f0f4f8", fg="#333")
        self.timer_label.pack()
        self.rec_indicator = tk.Label(timer_frame, text="", font=("Helvetica", 12),
                                      bg="#f0f4f8", fg="red")
        self.rec_indicator.pack()

        # Control buttons
        btn_frame = tk.Frame(tab, bg="#f0f4f8")
        btn_frame.pack(pady=10)

        self.btn_record = tk.Button(btn_frame, text="🎙 Start Recording", font=("Helvetica", 14, "bold"),
                                    command=self._toggle_record, bg="#e84e1a", fg="white",
                                    relief="flat", padx=20, pady=12, cursor="hand2")
        self.btn_record.pack(side="left", padx=10)

        self.btn_play = tk.Button(btn_frame, text="▶ Play Recording", font=("Helvetica", 12),
                                  command=self._play_recording, bg="#1a73e8", fg="white",
                                  relief="flat", padx=16, pady=12, cursor="hand2", state="disabled")
        self.btn_play.pack(side="left", padx=10)

        self.btn_submit = tk.Button(btn_frame, text="📤 Send & Transcribe", font=("Helvetica", 12),
                                    command=self._submit_audio, bg="#0d8a3c", fg="white",
                                    relief="flat", padx=16, pady=12, cursor="hand2", state="disabled")
        self.btn_submit.pack(side="left", padx=10)

        # Progress
        self.progress = ttk.Progressbar(tab, mode="indeterminate", length=400)
        self.progress.pack(pady=8)
        self.progress_label = tk.Label(tab, text="", bg="#f0f4f8", font=("Helvetica", 10), fg="#555")
        self.progress_label.pack()

        # Upload existing audio
        tk.Label(tab, text="— or —", bg="#f0f4f8", fg="#999").pack(pady=4)
        tk.Button(tab, text="📁 Upload Audio File", command=self._upload_file,
                  bg="#555", fg="white", relief="flat", padx=12, pady=8,
                  font=("Helvetica", 11), cursor="hand2").pack()

    def _build_transcript_tab(self):
        tab = self.transcript_tab

        # Toolbar
        toolbar = tk.Frame(tab, bg="#e8edf2", pady=6)
        toolbar.pack(fill="x")

        tk.Button(toolbar, text="▶ Replay Audio", command=self._play_recording,
                  bg="#1a73e8", fg="white", relief="flat", padx=10, pady=4,
                  font=("Helvetica", 10), cursor="hand2").pack(side="left", padx=8)

        tk.Button(toolbar, text="💾 Save Edits", command=self._save_edits,
                  bg="#0d8a3c", fg="white", relief="flat", padx=10, pady=4,
                  font=("Helvetica", 10), cursor="hand2").pack(side="left", padx=4)

        tk.Button(toolbar, text="📄 Export PDF", command=self._export_pdf,
                  bg="#9c27b0", fg="white", relief="flat", padx=10, pady=4,
                  font=("Helvetica", 10), cursor="hand2").pack(side="left", padx=4)

        # Stats bar
        self.stats_frame = tk.Frame(tab, bg="#dde3ea", pady=5)
        self.stats_frame.pack(fill="x")
        self.stats_label = tk.Label(self.stats_frame, text="No transcript loaded",
                                    bg="#dde3ea", font=("Helvetica", 10))
        self.stats_label.pack()

        # Scrollable segments area
        canvas_frame = tk.Frame(tab, bg="#f0f4f8")
        canvas_frame.pack(fill="both", expand=True)

        self.canvas = tk.Canvas(canvas_frame, bg="#f0f4f8", highlightthickness=0)
        scrollbar = ttk.Scrollbar(canvas_frame, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=scrollbar.set)

        scrollbar.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)

        self.segments_frame = tk.Frame(self.canvas, bg="#f0f4f8")
        self.canvas_window = self.canvas.create_window((0, 0), window=self.segments_frame, anchor="nw")

        self.segments_frame.bind("<Configure>", self._on_frame_configure)
        self.canvas.bind("<Configure>", self._on_canvas_configure)
        self.canvas.bind_all("<MouseWheel>", self._on_mousewheel)

    # ── Timer ─────────────────────────────────────────────────────────────────

    def _start_timer(self):
        self._elapsed = 0
        self.timer_running = True
        self._tick()

    def _tick(self):
        if self.timer_running:
            m, s = divmod(int(self._elapsed), 60)
            self.timer_label.config(text=f"{m:02d}:{s:02d}")
            self.lcd.update_timer(int(self._elapsed))
            self._elapsed += 1
            self._timer_id = self.after(1000, self._tick)

    def _stop_timer(self):
        self.timer_running = False
        if self._timer_id:
            self.after_cancel(self._timer_id)

    # ── Recording ─────────────────────────────────────────────────────────────

    def _toggle_record(self):
        if not self.recorder.recording:
            self._start_recording()
        else:
            self._stop_recording()

    def _start_recording(self):
        if self.session is None:
            self.session = Session(
                case_number=self._info_vars["case_number"].get(),
                judge_name=self._info_vars["judge_name"].get(),
                court_name=self._info_vars["court_name"].get(),
            )
        self.recorder.start()
        self._start_timer()
        self.btn_record.config(text="⏹ Stop Recording", bg="#b71c1c")
        self.rec_indicator.config(text="● RECORDING")
        self.btn_play.config(state="disabled")
        self.btn_submit.config(state="disabled")
        self._set_status("Recording...")
        self.lcd.show_recording()
        self.lcd.show_case_info(
            self.session.case_number, self.session.court_name
        )

    def _stop_recording(self):
        self.recorder.stop()
        self._stop_timer()
        self.btn_record.config(text="🎙 Start Recording", bg="#e84e1a")
        self.rec_indicator.config(text="")

        audio_path = str(SESSIONS_DIR / f"{self.session.session_id}.wav")
        self.recorder.save(audio_path)
        self.session.audio_file = audio_path
        self.session.save()

        self.btn_play.config(state="normal")
        self.btn_submit.config(state="normal")
        self._set_status(f"Recording saved: {audio_path}  ({self.recorder.duration:.1f}s)")
        self.lcd.show_saved(self.recorder.duration)

    # ── Playback ──────────────────────────────────────────────────────────────

    def _play_recording(self):
        if self.session and self.session.audio_file and os.path.exists(self.session.audio_file):
            self.btn_play.config(state="disabled", text="▶ Playing...")
            self.lcd.show_playing()
            self.player.play(self.session.audio_file, on_done=self._on_play_done)
        else:
            messagebox.showwarning("No Audio", "No recording available for this session.")

    def _on_play_done(self):
        self.btn_play.config(state="normal", text="▶ Play Recording")
        self.lcd.show_play_done()

    # ── Upload existing audio ─────────────────────────────────────────────────

    def _upload_file(self):
        path = filedialog.askopenfilename(
            title="Select Audio File",
            filetypes=[("Audio Files", "*.wav *.mp3 *.ogg *.flac *.m4a"), ("All Files", "*.*")]
        )
        if not path:
            return
        self.session = Session(
            case_number=self._info_vars["case_number"].get(),
            judge_name=self._info_vars["judge_name"].get(),
            court_name=self._info_vars["court_name"].get(),
        )
        self.session.audio_file = path
        self.session.save()
        self.btn_play.config(state="normal")
        self.btn_submit.config(state="normal")
        self._set_status(f"File loaded: {path}")
        self.lcd.show_saved(0.0)  # show "saved" state for uploaded file

    # ── API Submission ────────────────────────────────────────────────────────

    def _submit_audio(self):
        if not self.session or not self.session.audio_file:
            messagebox.showerror("No Audio", "Please record or upload audio first.")
            return
        self.btn_submit.config(state="disabled")
        self.progress.start()
        threading.Thread(target=self._submit_thread, daemon=True).start()

    def _submit_thread(self):
        try:
            self._queue.put(("status", "Uploading audio to server..."))
            self.lcd.show_uploading()
            result = submit_audio(self.session.audio_file)
            self._queue.put(("status", "Analysing with AI..."))
            self.lcd.show_analysing()
            self.session.result = result
            # Initialize edited_segments matching segment count
            n = len(result.get("segments", []))
            self.session.edited_segments = [""] * n
            self.session.save()
            self._queue.put(("result", result))
        except Exception as e:
            self._queue.put(("error", str(e)))

    # ── Queue polling ─────────────────────────────────────────────────────────

    def _poll_queue(self):
        try:
            while True:
                msg_type, data = self._queue.get_nowait()
                if msg_type == "status":
                    self._set_status(data)
                    self.progress_label.config(text=data)
                elif msg_type == "result":
                    self.progress.stop()
                    self.btn_submit.config(state="normal")
                    self.progress_label.config(text="Transcription complete!")
                    self._set_status("Transcription complete")
                    self._display_result(data)
                    self.nb.select(self.transcript_tab)
                    self.lcd.show_result(
                        data.get("num_speakers", 0),
                        data.get("overall_accuracy_pct", 0.0),
                    )
                elif msg_type == "error":
                    self.progress.stop()
                    self.btn_submit.config(state="normal")
                    self.progress_label.config(text="")
                    messagebox.showerror("Error", f"Transcription failed:\n{data}")
                    self._set_status(f"Error: {data}")
                    self.lcd.show_error(str(data)[:LCD_COLS])
        except queue.Empty:
            pass
        self.after(200, self._poll_queue)

    # ── Transcript Display ────────────────────────────────────────────────────

    def _display_result(self, result):
        # Clear existing
        for w in self.segments_frame.winfo_children():
            w.destroy()
        self.segment_editors = []

        # Stats
        n_spk = result.get("num_speakers", 0)
        n_seg = result.get("total_segments", 0)
        acc = result.get("overall_accuracy_pct", 0)
        dur = result.get("audio_duration_s", 0)
        total_w = result.get("total_words", 0)
        corr_w = result.get("total_corrected_words", 0)
        diar_ms = result.get("diarization_time_ms", 0)
        asr_ms = result.get("transcription_time_ms", 0)
        llm_ms = result.get("ai_optimization_time_ms", 0)
        self.stats_label.config(
            text=(
                f"Speakers: {n_spk}  |  Segments: {n_seg}  |  "
                f"Accuracy: {acc:.1f}%  |  Duration: {dur:.1f}s  |  "
                f"Words: {total_w}  |  AI corrections: {corr_w}  |  "
                f"⏱ diar {diar_ms/1000:.1f}s  asr {asr_ms/1000:.1f}s  llm {llm_ms/1000:.1f}s"
            )
        )

        # Legend
        legend = tk.Frame(self.segments_frame, bg="#f0f4f8", pady=4)
        legend.pack(fill="x", padx=15, pady=(4, 2))
        tk.Label(legend, text="Word confidence:", font=("Helvetica", 8, "bold"),
                 bg="#f0f4f8", fg="#555").pack(side="left", padx=(4, 6))
        for dot, desc in (
            ("#212121", "High"),
            ("#e67e00", "Medium"),
            ("#bf360c", "Low"),
            ("#c62828", "Garbled"),
            ("#0d47a1", "AI-corrected [orig→new]"),
        ):
            tk.Label(legend, text="■", font=("Helvetica", 10), bg="#f0f4f8", fg=dot).pack(side="left")
            tk.Label(legend, text=f" {desc}  ", font=("Helvetica", 8), bg="#f0f4f8", fg="#555").pack(side="left")

        segments = result.get("segments", [])
        edited = self.session.edited_segments if self.session else []

        for i, seg in enumerate(segments):
            self._add_segment_card(i, seg, edited[i] if i < len(edited) else "")

    def _add_segment_card(self, index, seg, edited_text):
        speaker = seg.get("speaker", "UNKNOWN")
        label = SPEAKER_LABELS.get(speaker, speaker)
        color = SPEAKER_COLORS.get(speaker, "#333333")
        start = seg.get("start", 0)
        end = seg.get("end", 0)
        accuracy = seg.get("segment_accuracy_pct", 0)
        asr_text = seg.get("asr_text", "")
        ai_text = seg.get("ai_corrected_text", "")
        word_scores = seg.get("word_scores", [])
        total_words = seg.get("total_words", 0)
        corrected_words = seg.get("corrected_words", 0)
        current_text = edited_text or ai_text or asr_text

        acc_color = "#0d8a3c" if accuracy >= 90 else ("#e67e00" if accuracy >= 70 else "#c62828")

        card = tk.Frame(self.segments_frame, bg="white", pady=0, padx=0,
                        relief="flat", bd=0, highlightbackground="#dde3ea", highlightthickness=1)
        card.pack(fill="x", padx=15, pady=6)

        tk.Frame(card, bg=color, width=5).pack(side="left", fill="y")

        content = tk.Frame(card, bg="white")
        content.pack(side="left", fill="both", expand=True, padx=12, pady=8)

        # ── Header ──
        header = tk.Frame(content, bg="white")
        header.pack(fill="x")
        tk.Label(header, text=label, font=("Helvetica", 11, "bold"),
                 bg="white", fg=color).pack(side="left")
        tk.Label(header, text=f"  [{start:.1f}s – {end:.1f}s]",
                 font=("Helvetica", 9), bg="white", fg="#888").pack(side="left")
        badges = tk.Frame(header, bg="white")
        badges.pack(side="right")
        tk.Label(badges, text=f" {total_words}w ",
                 font=("Helvetica", 8), bg="#546e7a", fg="white", padx=3, pady=1).pack(side="left", padx=2)
        if corrected_words > 0:
            tk.Label(badges, text=f" ✎ {corrected_words} corrected ",
                     font=("Helvetica", 8), bg="#1565c0", fg="white", padx=3, pady=1).pack(side="left", padx=2)
        tk.Label(badges, text=f" AI accuracy: {accuracy:.0f}% ",
                 font=("Helvetica", 8, "bold"), bg=acc_color, fg="white", padx=4, pady=1).pack(side="left", padx=2)

        ttk.Separator(content, orient="horizontal").pack(fill="x", pady=(6, 5))

        # ── Raw ASR row ──
        tk.Label(content, text="📝 Raw ASR:", font=("Helvetica", 9, "bold"),
                 bg="white", fg="#616161").pack(anchor="w")
        asr_box = tk.Text(content, height=2, font=("Helvetica", 10),
                          bg="#fafafa", relief="flat", wrap="word",
                          highlightbackground="#e0e0e0", highlightthickness=1,
                          padx=6, pady=4, fg="#424242", cursor="arrow")
        asr_box.insert("1.0", asr_text)
        asr_box.config(state="disabled")
        asr_box.pack(fill="x", pady=(2, 8))

        # ── AI Optimized row with per-word probabilities ──
        ai_hdr = tk.Frame(content, bg="white")
        ai_hdr.pack(fill="x", pady=(0, 2))
        tk.Label(ai_hdr, text=f"🤖 AI Optimized  (accuracy: {accuracy:.1f}%",
                 font=("Helvetica", 9, "bold"), bg="white", fg="#1565c0").pack(side="left")
        tk.Label(ai_hdr, text=f"  |  {total_words} words",
                 font=("Helvetica", 9), bg="white", fg="#888").pack(side="left")
        if corrected_words > 0:
            tk.Label(ai_hdr, text=f"  |  ✎ {corrected_words} AI-corrected)",
                     font=("Helvetica", 9), bg="white", fg="#1565c0").pack(side="left")
        else:
            tk.Label(ai_hdr, text=")", font=("Helvetica", 9), bg="white", fg="#888").pack(side="left")

        word_text = tk.Text(content, height=3, font=("Helvetica", 11),
                            bg="#f0f7ff", relief="flat", wrap="word",
                            highlightbackground="#90caf9", highlightthickness=1,
                            padx=6, pady=5, cursor="arrow")
        word_text.tag_configure("high",    foreground="#212121")
        word_text.tag_configure("high_p",  foreground="#9e9e9e", font=("Helvetica", 7))
        word_text.tag_configure("med",     foreground="#e67e00")
        word_text.tag_configure("med_p",   foreground="#e67e00", font=("Helvetica", 7))
        word_text.tag_configure("low",     foreground="#bf360c")
        word_text.tag_configure("low_p",   foreground="#bf360c", font=("Helvetica", 7))
        word_text.tag_configure("garbled", foreground="#c62828", underline=True)
        word_text.tag_configure("garb_p",  foreground="#c62828", font=("Helvetica", 7))
        word_text.tag_configure("fixed",   foreground="#0d47a1", background="#e3f2fd")
        word_text.tag_configure("fixed_p", foreground="#1565c0", font=("Helvetica", 7))

        if word_scores:
            for i, ws in enumerate(word_scores):
                word = ws.get("word", "")
                orig = ws.get("original_word", word)
                prob = ws.get("probability", 1.0)
                meaningful = ws.get("is_meaningful", True)
                was_corrected = ws.get("was_corrected", False)

                if i > 0:
                    word_text.insert("end", " ")

                if was_corrected:
                    pct = f"{prob * 100:.2f}%" if prob < 0.05 else f"{prob * 100:.0f}%"
                    word_text.insert("end", f"[{orig}→{word}]", "fixed")
                    word_text.insert("end", f"({pct})", "fixed_p")
                elif not meaningful or prob < 0.0001:
                    word_text.insert("end", word, "garbled")
                    word_text.insert("end", f"({prob * 100:.4f}%)", "garb_p")
                elif prob >= 0.05:
                    word_text.insert("end", word, "high")
                    word_text.insert("end", f"({prob * 100:.0f}%)", "high_p")
                elif prob >= 0.001:
                    word_text.insert("end", word, "med")
                    word_text.insert("end", f"({prob * 100:.2f}%)", "med_p")
                else:
                    word_text.insert("end", word, "low")
                    word_text.insert("end", f"({prob * 100:.3f}%)", "low_p")
        else:
            word_text.insert("end", ai_text or asr_text)

        word_text.config(state="disabled")
        word_text.pack(fill="x", pady=(0, 6))

        # ── Editable correction ──
        tk.Label(content, text="✏ Edit / Correct:", font=("Helvetica", 9, "bold"),
                 bg="white", fg="#555").pack(anchor="w")
        text_widget = tk.Text(content, height=2, font=("Helvetica", 11),
                              bg="#fffde7", relief="flat", wrap="word",
                              highlightbackground="#f9a825", highlightthickness=1,
                              padx=6, pady=4)
        text_widget.insert("1.0", current_text)
        text_widget.pack(fill="x", pady=(2, 0))
        self.segment_editors.append(text_widget)

    # ── Edit & Save ───────────────────────────────────────────────────────────

    def _save_edits(self):
        if not self.session:
            return
        edits = []
        for widget in self.segment_editors:
            edits.append(widget.get("1.0", "end-1c").strip())
        self.session.edited_segments = edits
        self.session.save()
        self._set_status("Edits saved.")
        self.lcd.show_session_loaded(self.session.case_number)
        messagebox.showinfo("Saved", "Transcript edits saved successfully.")

    # ── PDF Export ────────────────────────────────────────────────────────────

    def _export_pdf(self):
        if not self.session or not self.session.result:
            messagebox.showerror("No Data", "No transcript to export.")
            return
        self._save_edits()
        path = filedialog.asksaveasfilename(
            defaultextension=".pdf",
            filetypes=[("PDF Files", "*.pdf")],
            initialfile=f"court_transcript_{self.session.session_id}.pdf",
        )
        if not path:
            return
        self.lcd.show_exporting()
        try:
            export_pdf(self.session, path)
            messagebox.showinfo("Exported", f"PDF saved to:\n{path}")
            self._set_status(f"PDF exported: {path}")
            self.lcd.show_exported(os.path.basename(path))
        except Exception as e:
            messagebox.showerror("Export Failed", str(e))
            self.lcd.show_error("PDF export fail")

    # ── Sessions Browser ──────────────────────────────────────────────────────

    def _open_sessions(self):
        win = tk.Toplevel(self)
        win.title("Past Sessions")
        win.geometry("700x450")
        win.configure(bg="#f0f4f8")

        tk.Label(win, text="Past Sessions", font=("Helvetica", 14, "bold"),
                 bg="#f0f4f8").pack(pady=10)

        cols = ("Date", "Case No", "Court", "Judge", "Segments")
        tree = ttk.Treeview(win, columns=cols, show="headings", height=15)
        for col in cols:
            tree.heading(col, text=col)
            tree.column(col, width=120)
        tree.pack(fill="both", expand=True, padx=10)

        sessions = Session.list_sessions()
        session_ids = []
        for s in sessions:
            created = s.get("created_at", "")[:16].replace("T", " ")
            n_seg = len(s.get("result", {}).get("segments", [])) if s.get("result") else "—"
            tree.insert("", "end", values=(
                created,
                s.get("case_number", "—"),
                s.get("court_name", "—"),
                s.get("judge_name", "—"),
                n_seg,
            ))
            session_ids.append(s.get("session_id"))

        def on_load():
            sel = tree.selection()
            if not sel:
                return
            idx = tree.index(sel[0])
            sid = session_ids[idx]
            try:
                self.session = Session.load(sid)
                if self.session.result:
                    self._display_result(self.session.result)
                    self.nb.select(self.transcript_tab)
                    self.lcd.show_result(
                        self.session.result.get("num_speakers", 0),
                        self.session.result.get("overall_accuracy_pct", 0.0),
                    )
                else:
                    self.lcd.show_session_loaded(self.session.case_number)
                self._set_status(f"Session loaded: {sid}")
            except Exception as e:
                messagebox.showerror("Load Error", str(e))
                self.lcd.show_error("Load failed")
            win.destroy()

        btn_frame = tk.Frame(win, bg="#f0f4f8")
        btn_frame.pack(pady=8)
        tk.Button(btn_frame, text="Load Selected", command=on_load,
                  bg="#1a73e8", fg="white", relief="flat", padx=12, pady=6).pack(side="left", padx=5)
        tk.Button(btn_frame, text="Close", command=win.destroy,
                  bg="#555", fg="white", relief="flat", padx=12, pady=6).pack(side="left")

    # ── Scroll helpers ────────────────────────────────────────────────────────

    def _on_frame_configure(self, event):
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))

    def _on_canvas_configure(self, event):
        self.canvas.itemconfig(self.canvas_window, width=event.width)

    def _on_mousewheel(self, event):
        self.canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

    # ── Utility ───────────────────────────────────────────────────────────────

    def _set_status(self, msg):
        self.status_var.set(msg)


# ── Entry point ───────────────────────────────────────────────────────────────

def list_audio_devices():
    pa = pyaudio.PyAudio()
    print("\n=== PyAudio Devices ===")
    for i in range(pa.get_device_count()):
        info = pa.get_device_info_by_index(i)
        print(
            f"  [{i}] {info['name']}"
            f"  in={info['maxInputChannels']}"
            f"  out={info['maxOutputChannels']}"
            f"  rate={int(info['defaultSampleRate'])}Hz"
        )
    print(f"\n  Active INPUT_DEVICE_INDEX  = {INPUT_DEVICE_INDEX}")
    print(f"  Active OUTPUT_DEVICE_INDEX = {OUTPUT_DEVICE_INDEX}")
    print("========================\n")
    pa.terminate()


if __name__ == "__main__":
    list_audio_devices()
    app = CourtroomApp()
    app.mainloop()

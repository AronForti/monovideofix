#!/usr/bin/env python3
"""
video_judder_gui.py
===================
Applicazione GUI (PyQt5) con supporto Drag & Drop singolo e Batch (multi-file):
1. Permette di trascinare uno o più file video (o intere cartelle) in coda.
2. Esegue automaticamente l'analisi veloce per ciascun video stimando frame
   mancanti (salti/cadenza periodica), frame duplicati e cambi scena.
3. Imposta di default FPS (24/25/30/60) e Codec (h264/h265/prores422hq)
   corrispondenti a ciascun video in ingresso, con possibilità di modificarli
   singolarmente o applicarli a tutto il batch.
4. Esporta tutti i video in batch con ricostruzione Optical Flow a 16-bit e
   suffisso "_xxfps".
"""

import ctypes
import json
import os
import shutil
import subprocess
import sys
import threading
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

import cv2
import numpy as np
from PyQt5.QtCore import QPointF, QRectF, Qt, QThread, pyqtSignal
from PyQt5.QtGui import (
    QColor,
    QDragEnterEvent,
    QDropEvent,
    QFont,
    QIcon,
    QImage,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
    QPolygonF,
)
from PyQt5.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QButtonGroup,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)


VIDEO_EXTENSIONS = {".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v", ".mpg", ".mpeg"}
_FFMPEG_CACHE_LOCK = threading.Lock()
_RESOLVED_BINS: dict[str, str] = {}


@dataclass
class BatchVideoItem:
    """Rappresenta un singolo file video all'interno della coda batch."""

    path: str
    status: str = "In attesa analisi..."
    meta: dict | None = None
    analysis: dict | None = None
    target_fps: int = 24
    target_codec: str = "h265"
    output_path: str = ""
    report_text: str = ""
    exported: bool = False
    error_msg: str = ""

    def update_output_path(self):
        in_path = Path(self.path)
        ext = ".mov" if self.target_codec == "prores422hq" else ".mp4"
        self.output_path = str(in_path.with_name(f"{in_path.stem}_{self.target_fps}fps{ext}"))


def _get_persistent_bin_cache_dir() -> Path:
    """Restituisce la cartella cache persistente per i binari FFmpeg su Windows."""
    local_app_data = os.environ.get("LOCALAPPDATA")
    base = Path(local_app_data) if local_app_data else (Path.home() / "AppData" / "Local")
    cache_dir = base / "MonoVideoFix" / "bin"
    cache_dir.mkdir(parents=True, exist_ok=True)
    return cache_dir


def get_ffmpeg_bin(tool_name: str) -> str:
    """
    Trova ffmpeg.exe / ffprobe.exe con questa priorità per massimizzare la velocità di avvio:
    1. Cartella accanto all'eseguibile / script locale.
    2. Sistema operativo (PATH di Windows tramite shutil.which) -> nessuna estrazione se già presente!
    3. Cartella cache persistente (%LOCALAPPDATA%\\MonoVideoFix\\bin).
    4. Se assente ovunque, estrae on-demand dal bundle (embedded_ffmpeg.zip o _MEIPASS) nella cache persistente.
    """
    if tool_name in _RESOLVED_BINS:
        return _RESOLVED_BINS[tool_name]

    with _FFMPEG_CACHE_LOCK:
        if tool_name in _RESOLVED_BINS:
            return _RESOLVED_BINS[tool_name]

        exe_name = f"{tool_name}.exe" if sys.platform == "win32" else tool_name

        # 1. Cartella locale accanto all'eseguibile o allo script
        local_dirs = []
        if getattr(sys, "frozen", False):
            local_dirs.append(Path(sys.executable).parent)
            local_dirs.append(Path(sys.executable).parent / "_internal")
        local_dirs.append(Path(__file__).resolve().parent)

        for d in local_dirs:
            candidate = d / exe_name
            if candidate.is_file():
                _RESOLVED_BINS[tool_name] = str(candidate)
                return _RESOLVED_BINS[tool_name]

        # 2. Controlla se ffmpeg/ffprobe è già installato nel PC (PATH di sistema)
        sys_bin = shutil.which(exe_name) or shutil.which(tool_name)
        if sys_bin and Path(sys_bin).is_file():
            _RESOLVED_BINS[tool_name] = str(Path(sys_bin).resolve())
            return _RESOLVED_BINS[tool_name]

        # 3. Controlla la cache persistente su disco (%LOCALAPPDATA%\MonoVideoFix\bin)
        cache_dir = _get_persistent_bin_cache_dir()
        cached_exe = cache_dir / exe_name
        if cached_exe.is_file() and cached_exe.stat().st_size > 1_000_000:
            _RESOLVED_BINS[tool_name] = str(cached_exe)
            return _RESOLVED_BINS[tool_name]

        # 4. Estrazione on-demand direttamente dal singolo file .exe (o _MEIPASS)
        #    Viene eseguita SOLO se ffmpeg/ffprobe non è già nel PC o nella cache!
        if getattr(sys, "frozen", False):
            exe_file = Path(sys.executable)
            if exe_file.is_file() and zipfile.is_zipfile(exe_file):
                with zipfile.ZipFile(exe_file, "r") as zf:
                    if exe_name in zf.namelist():
                        zf.extract(exe_name, path=cache_dir)
                        if cached_exe.is_file():
                            _RESOLVED_BINS[tool_name] = str(cached_exe)
                            return _RESOLVED_BINS[tool_name]

            meipass = Path(getattr(sys, "_MEIPASS", "")) if hasattr(sys, "_MEIPASS") else None
            if meipass and (meipass / exe_name).is_file():
                shutil.copy2(meipass / exe_name, cached_exe)
                _RESOLVED_BINS[tool_name] = str(cached_exe)
                return _RESOLVED_BINS[tool_name]

        return tool_name


def get_subprocess_kwargs() -> dict:
    """Nasconde la finestra console di ffmpeg/ffprobe su Windows."""
    kwargs = {}
    if sys.platform == "win32":
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        kwargs["startupinfo"] = si
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    return kwargs


def probe_video(video_path: str) -> dict:
    """Estrae metadati completi del video e verifica l'eventuale presenza di audio."""
    cmd = [
        get_ffmpeg_bin("ffprobe"),
        "-v", "error",
        "-show_entries",
        "stream=index,codec_type,codec_name,profile,width,height,r_frame_rate,avg_frame_rate,nb_frames,duration,pix_fmt,color_space,color_transfer,color_primaries,color_range:format=duration",
        "-of", "json",
        str(video_path),
    ]
    raw = subprocess.check_output(cmd, text=True, **get_subprocess_kwargs())
    data = json.loads(raw)

    streams = data.get("streams", [])
    v_stream = next((s for s in streams if s.get("codec_type") == "video"), None)
    if v_stream is None:
        raise ValueError("Nessuno stream video trovato nel file.")

    has_audio = any(s.get("codec_type") == "audio" for s in streams)

    r_fps = v_stream.get("r_frame_rate", "24/1")
    num, den = map(int, r_fps.split("/"))
    fps = num / den if den else 24.0

    duration = float(
        v_stream.get("duration")
        or data.get("format", {}).get("duration")
        or 0.0
    )
    nb_frames = int(v_stream.get("nb_frames") or round(duration * fps))

    codec_raw = (v_stream.get("codec_name") or "").lower()
    if codec_raw in ("hevc", "h265"):
        detected_codec = "h265"
    elif codec_raw in ("prores", "apch", "apcn", "apcs", "apco", "ap4h"):
        detected_codec = "prores422hq"
    else:
        detected_codec = "h264"

    allowed_fps = [24, 25, 30, 60]
    closest_fps = min(allowed_fps, key=lambda x: abs(x - fps))

    return {
        "path": str(video_path),
        "width": int(v_stream["width"]),
        "height": int(v_stream["height"]),
        "fps": fps,
        "fps_str": r_fps,
        "closest_fps": closest_fps,
        "nb_frames": nb_frames,
        "duration": duration,
        "codec_raw": codec_raw,
        "detected_codec": detected_codec,
        "pix_fmt": v_stream.get("pix_fmt") or "yuv420p",
        "color_space": v_stream.get("color_space") or "bt709",
        "color_transfer": v_stream.get("color_transfer") or "bt709",
        "color_primaries": v_stream.get("color_primaries") or "bt709",
        "color_range": v_stream.get("color_range") or "tv",
        "has_audio": has_audio,
    }


def build_report_text(meta: dict, analysis: dict) -> str:
    """Formatta il report dettagliato dell'analisi per un singolo file."""
    missing = analysis["missing_indices"]
    duplicates = analysis["duplicate_indices"]
    cuts = analysis["scene_cuts"]
    period = analysis["cadence_period"]

    lines = [
        f"File       : {meta['path']}",
        f"Sorgente   : {meta['width']}x{meta['height']} @ {meta['fps']:.3f} fps | "
        f"Codec: {meta['codec_raw']} ({meta['pix_fmt']}) | Frame: {analysis['num_frames']}",
    ]
    if cuts:
        cuts_str = ", ".join(f"{c}→{c+1} ({(c+1)/meta['fps']:.2f}s)" for c in cuts)
        lines.append(f"Cambi scena: {cuts_str}")
    lines.append("-" * 68)

    if missing:
        if period:
            lines.append(
                f"[!] RILEVATA CADENZA REGOLARE DI FRAME MANCANTI: 1 ogni {period} frame "
                f"({len(missing)} frame mancanti totali)"
            )
        else:
            lines.append(f"[!] Rilevati {len(missing)} frame mancanti (salti di movimento):")

        ratios = analysis["ratios"]
        for idx in missing:
            t_sec = (idx + 1) / meta["fps"]
            lines.append(
                f"    • Transizione frame #{idx:3d} → #{idx+1:3d}  (t = {t_sec:5.2f}s)  |  "
                f"Salto movimento: {ratios[idx]:.2f}x rispetto ai frame adiacenti"
            )
    else:
        lines.append("[✓] Nessun frame mancante rilevato.")

    if duplicates:
        lines.append("-" * 68)
        lines.append(f"[!] Rilevati {len(duplicates)} frame duplicati / congelati:")
        for idx in duplicates:
            t_sec = (idx + 1) / meta["fps"]
            lines.append(f"    • Frame duplicato #{idx:3d} → #{idx+1:3d}  (t = {t_sec:5.2f}s)")
    else:
        lines.append("[✓] Nessun frame duplicato rilevato.")

    return "\n".join(lines)


def interpolate_midpoint_16bit(
    frame0_u16: np.ndarray,
    frame1_u16: np.ndarray,
    alpha: float = 0.5,
) -> np.ndarray:
    """
    Ricostruisce un frame intermedio a 16-bit (uint16 RGB) al tempo `alpha`
    usando DISOpticalFlow bidirezionale con pesi di occlusione normalizzati.
    """
    f0_u8 = (frame0_u16 >> 8).astype(np.uint8)
    f1_u8 = (frame1_u16 >> 8).astype(np.uint8)
    g0 = cv2.cvtColor(f0_u8, cv2.COLOR_RGB2GRAY)
    g1 = cv2.cvtColor(f1_u8, cv2.COLOR_RGB2GRAY)

    dis = cv2.DISOpticalFlow.create(cv2.DISOPTICAL_FLOW_PRESET_MEDIUM)
    dis.setFinestScale(1)
    dis.setPatchSize(16)
    dis.setPatchStride(4)

    flow01 = dis.calc(g0, g1, None)
    flow10 = dis.calc(g1, g0, None)

    h, w = g0.shape
    grid_x, grid_y = np.meshgrid(
        np.arange(w, dtype=np.float32),
        np.arange(h, dtype=np.float32),
    )

    map0_x = grid_x + alpha * flow10[..., 0]
    map0_y = grid_y + alpha * flow10[..., 1]
    map1_x = grid_x + (1.0 - alpha) * flow01[..., 0]
    map1_y = grid_y + (1.0 - alpha) * flow01[..., 1]

    f0_flt = frame0_u16.astype(np.float32)
    f1_flt = frame1_u16.astype(np.float32)

    w0 = cv2.remap(f0_flt, map0_x, map0_y, cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)
    w1 = cv2.remap(f1_flt, map1_x, map1_y, cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)

    flow01_warped = cv2.remap(flow01, map0_x, map0_y, cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)
    flow10_warped = cv2.remap(flow10, map1_x, map1_y, cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)

    err0 = np.hypot(flow10[..., 0] + flow01_warped[..., 0], flow10[..., 1] + flow01_warped[..., 1])
    err1 = np.hypot(flow01[..., 0] + flow10_warped[..., 0], flow01[..., 1] + flow10_warped[..., 1])

    wt0 = (1.0 - alpha) * np.exp(-0.1 * err0) + 1e-2
    wt1 = alpha * np.exp(-0.1 * err1) + 1e-2
    alpha0 = (wt0 / (wt0 + wt1))[..., None]

    blended = w0 * alpha0 + w1 * (1.0 - alpha0)
    return np.clip(np.round(blended), 0, 65535).astype(np.uint16)


class BatchAnalysisWorker(QThread):
    """Analizza in sequenza tutti i video in coda che non sono ancora stati analizzati."""

    item_started = pyqtSignal(int)
    item_progress = pyqtSignal(int, int, str)
    item_finished = pyqtSignal(int, dict, dict)
    item_error = pyqtSignal(int, str)
    all_finished = pyqtSignal()

    def __init__(self, items: list[BatchVideoItem]):
        super().__init__()
        self.items = items

    def run(self):
        for idx, item in enumerate(self.items):
            if item.analysis is not None or item.error_msg:
                continue
            self.item_started.emit(idx)
            try:
                meta, analysis = self._analyze_single(idx, item.path)
                self.item_finished.emit(idx, meta, analysis)
            except Exception as exc:
                self.item_error.emit(idx, str(exc))
        self.all_finished.emit()

    def _analyze_single(self, item_idx: int, video_path: str) -> tuple[dict, dict]:
        self.item_progress.emit(item_idx, 5, "Lettura metadati...")
        meta = probe_video(video_path)

        W, H = 480, 480
        frame_size = W * H
        total_expected = max(meta["nb_frames"], 1)

        cmd = [
            get_ffmpeg_bin("ffmpeg"),
            "-v", "error",
            "-i", video_path,
            "-vf", f"scale={W}:{H}",
            "-pix_fmt", "gray",
            "-f", "rawvideo",
            "-",
        ]
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, **get_subprocess_kwargs())
        frames = []
        while True:
            buf = proc.stdout.read(frame_size)
            if len(buf) < frame_size:
                break
            frames.append(np.frombuffer(buf, dtype=np.uint8).reshape((H, W)))
            if len(frames) % 25 == 0:
                pct = min(50, 10 + int(40 * len(frames) / total_expected))
                self.item_progress.emit(item_idx, pct, f"Decodifica {len(frames)}/{total_expected}...")
        proc.stdout.close()
        proc.wait()

        n = len(frames)
        if n < 3:
            raise ValueError("Troppi pochi fotogrammi per l'analisi.")

        mad_scores = []
        p90_disp = []
        med_disp = []

        for i in range(n - 1):
            g0, g1 = frames[i], frames[i + 1]
            mad = float(np.mean(np.abs(g1.astype(np.float32) - g0.astype(np.float32))))
            mad_scores.append(mad)

            pts0 = cv2.goodFeaturesToTrack(g0, maxCorners=300, qualityLevel=0.01, minDistance=8)
            if pts0 is not None and len(pts0) > 8:
                pts1, st, _ = cv2.calcOpticalFlowPyrLK(
                    g0, g1, pts0, None, winSize=(15, 15), maxLevel=2
                )
                mask = st.ravel() == 1
                if np.sum(mask) > 5:
                    d = np.linalg.norm(pts1[mask] - pts0[mask], axis=1)
                    med_disp.append(float(np.median(d)))
                    p90_disp.append(float(np.percentile(d, 90)))
                else:
                    med_disp.append(0.0)
                    p90_disp.append(0.0)
            else:
                med_disp.append(0.0)
                p90_disp.append(0.0)

            if i % 20 == 0:
                pct = min(95, 50 + int(45 * i / (n - 1)))
                self.item_progress.emit(item_idx, pct, f"Optical Flow {i}/{n-1}...")

        median_global_mad = float(np.median(mad_scores)) if mad_scores else 1.0
        cut_thresh = max(20.0, median_global_mad * 8.0)
        scene_cuts = [i for i, m in enumerate(mad_scores) if m > cut_thresh]
        cut_set = set(scene_cuts)

        ratios = []
        mad_ratios = []
        local_refs = []
        for i in range(n - 1):
            if i in cut_set:
                ratios.append(1.0)
                mad_ratios.append(1.0)
                local_refs.append(p90_disp[i])
                continue
            neigh_p90 = [
                p90_disp[j]
                for j in range(max(0, i - 6), min(n - 1, i + 7))
                if j != i and j not in cut_set
            ]
            neigh_mad = [
                mad_scores[j]
                for j in range(max(0, i - 6), min(n - 1, i + 7))
                if j != i and j not in cut_set
            ]
            ref_p90 = float(np.percentile(neigh_p90, 45)) if neigh_p90 else 1.0
            ref_mad = float(np.percentile(neigh_mad, 45)) if neigh_mad else 1.0
            local_refs.append(ref_p90)
            ratios.append(p90_disp[i] / max(ref_p90, 1e-4))
            mad_ratios.append(mad_scores[i] / max(ref_mad, 1e-4))

        best_period = None
        best_phase = None
        best_period_drops = []
        candidate_periods = [24, 25, 30, 20, 15, 12, 10, 8, 6, 5, 4]
        for period in candidate_periods:
            if n < period * 2:
                continue
            for phase in range(period):
                idxs = [i for i in range(phase, n - 1, period) if i not in cut_set]
                if len(idxs) < 2:
                    continue
                phase_ratios = [ratios[i] for i in idxs]
                high_count = sum(1 for r in phase_ratios if r >= 1.38)
                med_r = float(np.median(phase_ratios))
                if med_r >= 1.55 and high_count >= max(2, int(0.75 * len(idxs))):
                    best_period = period
                    best_phase = phase
                    best_period_drops = idxs
                    break
            if best_period is not None:
                break

        missing_set = set(best_period_drops)
        for i in range(n - 1):
            if i in cut_set or i in missing_set:
                continue
            if local_refs[i] > 0.25 and ratios[i] > 1.85 and mad_ratios[i] > 1.35:
                if (i - 1) not in missing_set and (i + 1) not in missing_set:
                    missing_set.add(i)

        missing_indices = sorted(missing_set)

        duplicate_indices = []
        for i in range(n - 1):
            if i in cut_set or i in missing_set:
                continue
            if local_refs[i] > 0.35 and ratios[i] < 0.20 and mad_ratios[i] < 0.22 and mad_scores[i] < 0.40:
                duplicate_indices.append(i)

        analysis = {
            "num_frames": n,
            "mad_scores": mad_scores,
            "med_disp": med_disp,
            "p90_disp": p90_disp,
            "ratios": ratios,
            "scene_cuts": scene_cuts,
            "missing_indices": missing_indices,
            "duplicate_indices": duplicate_indices,
            "cadence_period": best_period,
            "cadence_phase": best_phase,
        }
        return meta, analysis


class BatchExportWorker(QThread):
    """Esporta in sequenza tutti i video analizzati nella coda batch."""

    item_started = pyqtSignal(int)
    item_progress = pyqtSignal(int, int, str)
    item_finished = pyqtSignal(int, str, int, int)
    item_error = pyqtSignal(int, str)
    all_finished = pyqtSignal(int, int)

    def __init__(self, items: list[BatchVideoItem]):
        super().__init__()
        self.items = items

    def run(self):
        success_count = 0
        total_to_export = sum(1 for it in self.items if it.meta and it.analysis)
        for idx, item in enumerate(self.items):
            if not item.meta or not item.analysis:
                continue
            self.item_started.emit(idx)
            try:
                written, fixed = self._export_single(idx, item)
                success_count += 1
                self.item_finished.emit(idx, item.output_path, written, fixed)
            except Exception as exc:
                self.item_error.emit(idx, str(exc))
        self.all_finished.emit(success_count, total_to_export)

    def _export_single(self, item_idx: int, item: BatchVideoItem) -> tuple[int, int]:
        meta = item.meta
        analysis = item.analysis
        target_fps = item.target_fps
        target_codec = item.target_codec
        item.update_output_path()
        output_path = item.output_path

        input_path = meta["path"]
        w, h = meta["width"], meta["height"]
        n_total = analysis["num_frames"]
        frame_bytes = w * h * 3 * 2

        missing_set = set(analysis["missing_indices"])
        duplicate_set = set(analysis["duplicate_indices"])
        ratios = analysis["ratios"]

        replace_dup_set = set()
        drop_dup_set = set()
        for d_idx in duplicate_set:
            if d_idx + 1 < len(ratios) and ratios[d_idx + 1] > 1.50:
                replace_dup_set.add(d_idx + 1)
            else:
                drop_dup_set.add(d_idx + 1)

        repaired_count = n_total + len(missing_set) - len(drop_dup_set)
        orig_duration = meta["duration"] or (n_total / meta["fps"])
        natural_fps = repaired_count / max(orig_duration, 1e-3)

        direct_1to1 = abs(target_fps - natural_fps) <= 2.0 or (
            target_fps in (24, 25) and round(meta["fps"]) in (24, 25)
        )
        pipe_in_fps = f"{target_fps}/1" if direct_1to1 else f"{natural_fps:.6f}"

        decode_cmd = [
            get_ffmpeg_bin("ffmpeg"),
            "-v", "error",
            "-i", input_path,
            "-f", "rawvideo",
            "-pix_fmt", "rgb48le",
            "-",
        ]

        encode_cmd = [
            get_ffmpeg_bin("ffmpeg"),
            "-y",
            "-v", "error",
            "-f", "rawvideo",
            "-pix_fmt", "rgb48le",
            "-s", f"{w}x{h}",
            "-r", pipe_in_fps,
            "-i", "-",
        ]

        if meta.get("has_audio"):
            encode_cmd.extend(["-i", input_path, "-map", "0:v:0", "-map", "1:a:0?"])

        if not direct_1to1:
            encode_cmd.extend([
                "-vf",
                f"framerate=fps={target_fps}:interp_start=0:interp_end=255:scene=8.2",
            ])

        if target_codec == "h264":
            encode_cmd.extend([
                "-c:v", "libx264",
                "-preset", "medium",
                "-crf", "15",
                "-pix_fmt", "yuv420p",
                "-color_primaries", meta["color_primaries"],
                "-color_trc", meta["color_transfer"],
                "-colorspace", meta["color_space"],
                "-color_range", meta["color_range"],
                "-movflags", "+faststart",
            ])
        elif target_codec == "h265":
            pix_fmt = meta["pix_fmt"] if "10" in meta["pix_fmt"] else "yuv420p10le"
            encode_cmd.extend([
                "-c:v", "libx265",
                "-preset", "medium",
                "-crf", "15",
                "-pix_fmt", pix_fmt,
                "-color_primaries", meta["color_primaries"],
                "-color_trc", meta["color_transfer"],
                "-colorspace", meta["color_space"],
                "-color_range", meta["color_range"],
                "-tag:v", "hvc1",
                "-x265-params", "log-level=error",
                "-movflags", "+faststart",
            ])
        elif target_codec == "prores422hq":
            encode_cmd.extend([
                "-c:v", "prores_ks",
                "-profile:v", "3",
                "-vendor", "apl0",
                "-bits_per_mb", "8000",
                "-pix_fmt", "yuv422p10le",
                "-color_primaries", meta["color_primaries"],
                "-color_trc", meta["color_transfer"],
                "-colorspace", meta["color_space"],
                "-color_range", meta["color_range"],
            ])

        if meta.get("has_audio"):
            if target_codec == "prores422hq":
                encode_cmd.extend(["-c:a", "pcm_s16le"])
            else:
                encode_cmd.extend(["-c:a", "aac", "-b:a", "192k"])

        encode_cmd.append(output_path)

        decoder = subprocess.Popen(decode_cmd, stdout=subprocess.PIPE, **get_subprocess_kwargs())
        encoder = subprocess.Popen(
            encode_cmd,
            stdin=subprocess.PIPE,
            stderr=subprocess.PIPE,
            **get_subprocess_kwargs(),
        )

        def read_one_frame():
            raw = decoder.stdout.read(frame_bytes)
            if len(raw) < frame_bytes:
                return None, None
            arr = np.frombuffer(raw, dtype=np.uint16).reshape((h, w, 3))
            return raw, arr

        reconstructed_count = 0
        written_count = 0

        prev_raw, prev_arr = read_one_frame()
        last_written_arr = prev_arr
        idx = 0
        pending_replace = False

        while prev_raw is not None:
            next_raw, next_arr = read_one_frame()

            if idx in drop_dup_set:
                reconstructed_count += 1
            elif pending_replace and next_arr is not None:
                mid_arr = interpolate_midpoint_16bit(last_written_arr, next_arr, alpha=0.5)
                encoder.stdin.write(mid_arr.tobytes())
                last_written_arr = mid_arr
                written_count += 1
                reconstructed_count += 1
                pending_replace = False
            else:
                encoder.stdin.write(prev_raw)
                last_written_arr = prev_arr
                written_count += 1

            if (idx + 1) in replace_dup_set:
                pending_replace = True

            if idx in missing_set and next_arr is not None:
                pct = min(96, int(100 * idx / max(n_total, 1)))
                self.item_progress.emit(item_idx, pct, f"Optical Flow #{idx}→#{idx+1}...")
                mid_arr = interpolate_midpoint_16bit(prev_arr, next_arr, alpha=0.5)
                encoder.stdin.write(mid_arr.tobytes())
                last_written_arr = mid_arr
                written_count += 1
                reconstructed_count += 1

            prev_raw, prev_arr = next_raw, next_arr
            idx += 1

            if idx % 10 == 0 or idx == n_total:
                pct = min(98, int(100 * idx / max(n_total, 1)))
                self.item_progress.emit(item_idx, pct, f"Frame {idx}/{n_total}...")

        if decoder.stdout:
            decoder.stdout.close()
        decoder.wait()

        if encoder.stdin:
            encoder.stdin.close()
        stderr_out = encoder.stderr.read().decode("utf-8", errors="replace") if encoder.stderr else ""
        retcode = encoder.wait()

        if retcode != 0:
            raise RuntimeError(f"Errore FFmpeg:\n{stderr_out}")

        return written_count, reconstructed_count


def render_monovideofix_pixmap(size: int = 256) -> QPixmap:
    """Disegna l'icona vettoriale MONOGRID (Blu Cobalto #0047FF + Bianco Puro #FFFFFF)."""
    img = QImage(size, size, QImage.Format_ARGB32)
    img.fill(Qt.transparent)
    p = QPainter(img)
    p.setRenderHint(QPainter.Antialiasing, True)

    s = float(size)
    margin = s * 0.04
    radius = s * 0.22
    bg_rect = QRectF(margin, margin, s - 2 * margin, s - 2 * margin)
    p.setPen(Qt.NoPen)
    p.setBrush(QColor("#0047FF"))
    p.drawRoundedRect(bg_rect, radius, radius)

    outer_m = s * 0.20
    thick = s * 0.095
    frame_path = QPainterPath()
    frame_path.addRect(QRectF(outer_m, outer_m, s - 2 * outer_m, s - 2 * outer_m))
    inner_m = outer_m + thick
    inner_path = QPainterPath()
    inner_path.addRect(QRectF(inner_m, inner_m, s - 2 * inner_m, s - 2 * inner_m))
    ring_path = frame_path.subtracted(inner_path)

    p.setBrush(QColor("#FFFFFF"))
    p.drawPath(ring_path)

    poly = QPolygonF([
        QPointF(s * 0.405, s * 0.355),
        QPointF(s * 0.645, s * 0.500),
        QPointF(s * 0.405, s * 0.645),
    ])
    p.drawPolygon(poly)
    p.end()
    return QPixmap.fromImage(img)


def create_app_icon() -> QIcon:
    icon = QIcon()
    for sz in (16, 32, 48, 64, 128, 256):
        icon.addPixmap(render_monovideofix_pixmap(sz))
    return icon


class DropAreaWidget(QWidget):
    """Area Drag & Drop multi-file cliccabile in stile MONOGRID su sfondo Blu."""

    files_dropped = pyqtSignal(list)

    def __init__(self):
        super().__init__()
        self.setAcceptDrops(True)
        self.setCursor(Qt.PointingHandCursor)
        self.setMinimumHeight(96)
        self.setObjectName("DropArea")
        self._hovered = False
        self._dragging = False

        layout = QVBoxLayout(self)
        layout.setAlignment(Qt.AlignCenter)
        layout.setSpacing(4)

        self.icon_label = QLabel("Trascina qui uno o più video (anche in Batch)")
        self.icon_label.setAlignment(Qt.AlignCenter)
        self.icon_label.setStyleSheet(
            "background: transparent; border: none; font-size: 16px; font-weight: 800; color: #FFFFFF;"
        )

        self.sub_label = QLabel(
            "oppure clicca per selezionare più file contemporaneamente (.mp4, .mov, .mkv, .avi)"
        )
        self.sub_label.setAlignment(Qt.AlignCenter)
        self.sub_label.setStyleSheet(
            "background: transparent; border: none; font-size: 12px; font-weight: 600; color: rgba(255, 255, 255, 0.85);"
        )

        layout.addWidget(self.icon_label)
        layout.addWidget(self.sub_label)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)

        bg_color = (
            QColor(255, 255, 255, 45)
            if self._dragging
            else (QColor(255, 255, 255, 25) if self._hovered else QColor("#0035C9"))
        )
        border_color = (
            QColor("#FFFFFF") if (self._dragging or self._hovered) else QColor(255, 255, 255, 190)
        )

        rect = QRectF(self.rect()).adjusted(1.5, 1.5, -1.5, -1.5)
        painter.setPen(Qt.NoPen)
        painter.setBrush(bg_color)
        painter.drawRoundedRect(rect, 10.0, 10.0)

        pen = QPen(border_color, 2.0, Qt.DashLine)
        pen.setDashPattern([5, 4])
        painter.setPen(pen)
        painter.setBrush(Qt.NoBrush)
        painter.drawRoundedRect(rect, 10.0, 10.0)

    def enterEvent(self, event):
        self._hovered = True
        self.update()

    def leaveEvent(self, event):
        self._hovered = False
        self.update()

    @staticmethod
    def _extract_video_paths(urls) -> list[str]:
        found = []
        for url in urls:
            p = Path(url.toLocalFile())
            if p.is_dir():
                for child in sorted(p.rglob("*")):
                    if child.is_file() and child.suffix.lower() in VIDEO_EXTENSIONS:
                        found.append(str(child))
            elif p.is_file() and p.suffix.lower() in VIDEO_EXTENSIONS:
                found.append(str(p))
        return found

    def dragEnterEvent(self, event: QDragEnterEvent):
        if event.mimeData().hasUrls():
            paths = self._extract_video_paths(event.mimeData().urls())
            if paths:
                self._dragging = True
                self.update()
                event.acceptProposedAction()
                return
        event.ignore()

    def dragLeaveEvent(self, event):
        self._dragging = False
        self.update()

    def dropEvent(self, event: QDropEvent):
        self._dragging = False
        self.update()
        paths = self._extract_video_paths(event.mimeData().urls())
        if paths:
            self.files_dropped.emit(paths)
            event.acceptProposedAction()

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            files, _ = QFileDialog.getOpenFileNames(
                self,
                "Seleziona uno o più Video",
                str(Path.home() / "Downloads"),
                "Video (*.mp4 *.mov *.mkv *.avi *.webm *.m4v);;Tutti i file (*.*)",
            )
            if files:
                self.files_dropped.emit(files)


class VideoJudderFixerWindow(QMainWindow):
    """Finestra principale MONOVIDEOFIX v0.7 (Sfondo Blu Cobalto MONOGRID con superfici Bianche e testo Nero)."""

    def __init__(self, initial_videos: list[str] | None = None):
        super().__init__()
        self.setWindowTitle("MONOVIDEOFIX v0.7")
        self.setWindowIcon(create_app_icon())
        self.setMinimumSize(820, 800)
        self.setAcceptDrops(True)

        self.batch_items: list[BatchVideoItem] = []
        self.analysis_worker: BatchAnalysisWorker | None = None
        self.export_worker: BatchExportWorker | None = None
        self._updating_controls = False
        self._user_customized_export = False

        self._build_ui()
        self._apply_theme()

        if initial_videos:
            valid = [v for v in initial_videos if Path(v).exists()]
            if valid:
                self.add_videos_to_batch(valid)

    def dragEnterEvent(self, event: QDragEnterEvent):
        if event.mimeData().hasUrls():
            paths = DropAreaWidget._extract_video_paths(event.mimeData().urls())
            if paths:
                self.drop_area._dragging = True
                self.drop_area.update()
                event.acceptProposedAction()
                return
        event.ignore()

    def dragLeaveEvent(self, event):
        self.drop_area._dragging = False
        self.drop_area.update()

    def dropEvent(self, event: QDropEvent):
        self.drop_area._dragging = False
        self.drop_area.update()
        paths = DropAreaWidget._extract_video_paths(event.mimeData().urls())
        if paths:
            self.add_videos_to_batch(paths)
            event.acceptProposedAction()

    def _build_ui(self):
        central = QWidget()
        central.setObjectName("CentralWidget")
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(20, 16, 20, 18)
        root.setSpacing(12)

        # 1. Area Drag & Drop
        self.drop_area = DropAreaWidget()
        self.drop_area.files_dropped.connect(self.add_videos_to_batch)
        root.addWidget(self.drop_area)

        # 2. Card Coda Batch (Tabella File)
        batch_card = QFrame()
        batch_card.setObjectName("Card")
        batch_layout = QVBoxLayout(batch_card)
        batch_layout.setContentsMargins(16, 12, 16, 12)
        batch_layout.setSpacing(8)

        batch_header = QHBoxLayout()
        self.queue_title_lbl = QLabel("CODA BATCH (0 VIDEO)")
        self.queue_title_lbl.setStyleSheet(
            "background: transparent; font-size: 11px; font-weight: 800; color: #FFFFFF; letter-spacing: 1px;"
        )
        batch_header.addWidget(self.queue_title_lbl)
        batch_header.addStretch()
        batch_layout.addLayout(batch_header)

        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels([
            "",
            "File Video",
            "Sorgente",
            "Frame Mancanti",
            "Duplicati",
        ])
        self.table.setAcceptDrops(False)
        self.table.viewport().setAcceptDrops(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.setShowGrid(False)
        self.table.setAlternatingRowColors(False)
        self.table.setMinimumHeight(140)

        hdr = self.table.horizontalHeader()
        hdr.setMinimumSectionSize(28)
        hdr.setSectionResizeMode(0, QHeaderView.Fixed)
        self.table.setColumnWidth(0, 32)
        hdr.setSectionResizeMode(1, QHeaderView.Stretch)
        hdr.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        hdr.setSectionResizeMode(3, QHeaderView.ResizeToContents)
        hdr.setSectionResizeMode(4, QHeaderView.ResizeToContents)

        self.table.itemSelectionChanged.connect(self._on_table_selection_changed)
        batch_layout.addWidget(self.table)
        root.addWidget(batch_card)

        # 3. Barra dello stato / analisi subito sotto il blocco dei file batch
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_bar.setTextVisible(True)
        self.progress_bar.setFormat("Pronto")
        root.addWidget(self.progress_bar)

        # 4. Card Dettaglio Analisi Veloce (del video selezionato)
        analysis_card = QFrame()
        analysis_card.setObjectName("Card")
        card_layout = QVBoxLayout(analysis_card)
        card_layout.setContentsMargins(16, 12, 16, 12)
        card_layout.setSpacing(8)

        header_row = QHBoxLayout()
        title_lbl = QLabel("DETTAGLIO ANALISI VELOCE (VIDEO SELEZIONATO)")
        title_lbl.setStyleSheet(
            "background: transparent; font-size: 11px; font-weight: 800; color: #FFFFFF; letter-spacing: 1px;"
        )
        self.meta_badge = QLabel("Nessun video selezionato")
        self.meta_badge.setStyleSheet(
            "background: transparent; font-size: 12px; color: rgba(255, 255, 255, 0.90); font-weight: 600;"
        )
        header_row.addWidget(title_lbl)
        header_row.addStretch()
        header_row.addWidget(self.meta_badge)
        card_layout.addLayout(header_row)

        metrics_row = QHBoxLayout()
        metrics_row.setSpacing(10)

        self.missing_box_val, missing_frame = self._create_metric_box("FRAME MANCANTI STIMATI", "—", "#0047FF")
        self.dup_box_val, dup_frame = self._create_metric_box("FRAME DUPLICATI STIMATI", "—", "#0047FF")
        self.cuts_box_val, cuts_frame = self._create_metric_box("CAMBI SCENA", "—", "#000000")

        metrics_row.addWidget(missing_frame)
        metrics_row.addWidget(dup_frame)
        metrics_row.addWidget(cuts_frame)
        card_layout.addLayout(metrics_row)

        self.log_box = QTextEdit()
        self.log_box.setReadOnly(True)
        self.log_box.setAcceptDrops(False)
        self.log_box.viewport().setAcceptDrops(False)
        self.log_box.setMinimumHeight(105)
        self.log_box.setPlaceholderText(
            "Trascina uno o più video in qualsiasi punto della finestra: ciascun file verrà analizzato automaticamente..."
        )
        card_layout.addWidget(self.log_box)
        root.addWidget(analysis_card)

        # 5. Card Impostazioni di Export (applicate di default a tutti i video caricati)
        export_card = QFrame()
        export_card.setObjectName("Card")
        exp_layout = QVBoxLayout(export_card)
        exp_layout.setContentsMargins(16, 12, 16, 12)
        exp_layout.setSpacing(10)

        exp_top = QHBoxLayout()
        exp_title = QLabel("IMPOSTAZIONI DI ESPORTAZIONE")
        exp_title.setStyleSheet(
            "background: transparent; font-size: 11px; font-weight: 800; color: #FFFFFF; letter-spacing: 1px;"
        )
        exp_top.addWidget(exp_title)
        exp_top.addStretch()
        exp_layout.addLayout(exp_top)

        # Selettore FPS: 24 / 25 / 30 / 60
        fps_row = QHBoxLayout()
        fps_lbl = QLabel("FPS di Export:")
        fps_lbl.setStyleSheet(
            "background: transparent; font-size: 13px; font-weight: 700; color: #FFFFFF; min-width: 130px;"
        )
        fps_row.addWidget(fps_lbl)

        self.fps_group = QButtonGroup(self)
        self.fps_Group_buttons = {}
        for fps_val in (24, 25, 30, 60):
            btn = QPushButton(f"{fps_val} FPS")
            btn.setCheckable(True)
            btn.setObjectName("OptionBtn")
            btn.setCursor(Qt.PointingHandCursor)
            self.fps_group.addButton(btn, fps_val)
            self.fps_Group_buttons[fps_val] = btn
            fps_row.addWidget(btn)
        self.fps_Group_buttons[24].setChecked(True)
        self.fps_group.buttonClicked.connect(self._on_user_option_changed)
        exp_layout.addLayout(fps_row)

        # Selettore Codec: h264 / h265 / prores422hq
        codec_row = QHBoxLayout()
        codec_lbl = QLabel("Codec di Output:")
        codec_lbl.setStyleSheet(
            "background: transparent; font-size: 13px; font-weight: 700; color: #FFFFFF; min-width: 130px;"
        )
        codec_row.addWidget(codec_lbl)

        self.codec_group = QButtonGroup(self)
        self.codec_buttons = {}
        codec_specs = [
            ("h264", "H.264 (.mp4)"),
            ("h265", "H.265 / HEVC (.mp4)"),
            ("prores422hq", "ProRes 422 HQ (.mov)"),
        ]
        for idx_c, (c_key, c_label) in enumerate(codec_specs):
            btn = QPushButton(c_label)
            btn.setCheckable(True)
            btn.setObjectName("OptionBtn")
            btn.setCursor(Qt.PointingHandCursor)
            btn.setProperty("codec_key", c_key)
            self.codec_group.addButton(btn, idx_c)
            self.codec_buttons[c_key] = btn
            codec_row.addWidget(btn)
        self.codec_buttons["h265"].setChecked(True)
        self.codec_group.buttonClicked.connect(self._on_user_option_changed)
        exp_layout.addLayout(codec_row)

        self.out_preview_lbl = QLabel("Output: —")
        self.out_preview_lbl.setStyleSheet("background: transparent; font-size: 12px; color: rgba(255, 255, 255, 0.90);")
        exp_layout.addWidget(self.out_preview_lbl)

        root.addWidget(export_card)

        # 6. Tasto ESPORTA
        self.export_btn = QPushButton("ESPORTA")
        self.export_btn.setObjectName("ExportBtn")
        self.export_btn.setMinimumHeight(50)
        self.export_btn.setCursor(Qt.PointingHandCursor)
        self.export_btn.setEnabled(False)
        self.export_btn.clicked.connect(self.start_batch_export)
        root.addWidget(self.export_btn)

    def _create_metric_box(self, title: str, value: str, accent_color: str):
        box = QFrame()
        box.setObjectName("MetricBox")
        lay = QVBoxLayout(box)
        lay.setContentsMargins(12, 7, 12, 7)
        lay.setSpacing(2)

        t_lbl = QLabel(title)
        t_lbl.setStyleSheet(
            "background: transparent; border: none; font-size: 10px; font-weight: 800; color: #334155;"
        )

        v_lbl = QLabel(value)
        v_lbl.setStyleSheet(
            f"background: transparent; border: none; font-size: 17px; font-weight: 900; color: {accent_color};"
        )

        lay.addWidget(t_lbl)
        lay.addWidget(v_lbl)
        return v_lbl, box

    def _apply_theme(self):
        self.setStyleSheet(
            """
            QMainWindow, QWidget#CentralWidget {
                background-color: #0047FF;
                color: #FFFFFF;
                font-family: 'Segoe UI', sans-serif;
            }
            QLabel {
                background-color: transparent;
                border: none;
                color: #FFFFFF;
                font-family: 'Segoe UI', sans-serif;
            }
            QFrame#Card {
                background-color: #0035C9;
                border: 1px solid rgba(255, 255, 255, 0.30);
                border-radius: 10px;
            }
            QFrame#MetricBox {
                background-color: #FFFFFF;
                border: none;
                border-radius: 8px;
            }
            QTableWidget {
                background-color: #FFFFFF;
                border: none;
                border-radius: 8px;
                color: #000000;
                font-size: 12px;
                selection-background-color: #E2EBFF;
                selection-color: #0047FF;
                outline: none;
            }
            QTableWidget::item:selected {
                background-color: #E2EBFF;
                color: #0047FF;
                font-weight: 800;
            }
            QHeaderView::section {
                background-color: #F0F5FF;
                color: #0047FF;
                font-size: 11px;
                font-weight: 800;
                padding: 6px 8px;
                border: none;
                border-bottom: 1px solid #D8E2F0;
            }
            QTextEdit {
                background-color: #FFFFFF;
                border: none;
                border-radius: 8px;
                padding: 8px;
                color: #000000;
                font-family: 'Consolas', monospace;
                font-size: 12px;
            }
            QPushButton#OptionBtn {
                background-color: rgba(255, 255, 255, 0.14);
                border: 1px solid rgba(255, 255, 255, 0.40);
                border-radius: 7px;
                padding: 8px 14px;
                color: #FFFFFF;
                font-size: 13px;
                font-weight: 700;
            }
            QPushButton#OptionBtn:hover {
                border-color: #FFFFFF;
                color: #FFFFFF;
                background-color: rgba(255, 255, 255, 0.25);
            }
            QPushButton#OptionBtn:checked {
                background-color: #FFFFFF;
                border-color: #FFFFFF;
                color: #0047FF;
                font-weight: 900;
            }
            QPushButton#RowRemoveBtn {
                background-color: transparent;
                border: none;
                border-radius: 4px;
                color: #64748B;
                font-size: 13px;
                font-weight: 900;
                padding: 0px;
            }
            QPushButton#RowRemoveBtn:hover {
                background-color: #FEE2E2;
                color: #DC2626;
            }
            QPushButton#ExportBtn {
                background-color: #FFFFFF;
                border: none;
                border-radius: 8px;
                color: #0047FF;
                font-size: 16px;
                font-weight: 900;
                letter-spacing: 1px;
            }
            QPushButton#ExportBtn:hover {
                background-color: #E2EBFF;
                color: #0035C9;
            }
            QPushButton#ExportBtn:disabled {
                background-color: rgba(255, 255, 255, 0.30);
                color: rgba(255, 255, 255, 0.65);
            }
            QProgressBar {
                background-color: #FFFFFF;
                border: none;
                border-radius: 7px;
                height: 24px;
                text-align: center;
                color: #000000;
                font-size: 12px;
                font-weight: 800;
            }
            QProgressBar::chunk {
                background-color: #93C5FD;
                border-radius: 6px;
            }
            """
        )

    def get_selected_fps(self) -> int:
        return self.fps_group.checkedId()

    def get_selected_codec(self) -> str:
        btn = self.codec_group.checkedButton()
        return btn.property("codec_key") if btn else "h265"

    def get_selected_row(self) -> int:
        rows = self.table.selectionModel().selectedRows()
        return rows[0].row() if rows else -1

    def add_videos_to_batch(self, video_paths: list[str]):
        if self.export_worker and self.export_worker.isRunning():
            return

        existing_paths = {os.path.normcase(os.path.abspath(it.path)) for it in self.batch_items}
        added_any = False
        cur_fps = self.get_selected_fps()
        cur_codec = self.get_selected_codec()

        for p in video_paths:
            norm_p = os.path.normcase(os.path.abspath(p))
            if norm_p in existing_paths:
                continue
            existing_paths.add(norm_p)
            item = BatchVideoItem(path=p, target_fps=cur_fps, target_codec=cur_codec)
            item.update_output_path()
            self.batch_items.append(item)
            self._append_table_row(item)
            added_any = True

        self._update_queue_header()
        if self.table.rowCount() > 0 and self.get_selected_row() == -1:
            self.table.selectRow(0)

        if added_any:
            self._start_pending_analyses()

    def _create_row_remove_widget(self, item: BatchVideoItem) -> QWidget:
        container = QWidget()
        container.setStyleSheet("background: transparent;")
        lay = QHBoxLayout(container)
        lay.setContentsMargins(4, 2, 4, 2)
        lay.setAlignment(Qt.AlignCenter)

        btn = QPushButton("✕")
        btn.setObjectName("RowRemoveBtn")
        btn.setFixedSize(22, 22)
        btn.setCursor(Qt.PointingHandCursor)
        btn.setToolTip(f"Rimuovi {Path(item.path).name}")
        btn.clicked.connect(lambda _checked=False, it=item: self.remove_video_item(it))
        lay.addWidget(btn)
        return container

    def _append_table_row(self, item: BatchVideoItem):
        row = self.table.rowCount()
        self.table.insertRow(row)
        self.table.setCellWidget(row, 0, self._create_row_remove_widget(item))
        self._refresh_table_row(row)

    def _refresh_table_row(self, row: int):
        if row < 0 or row >= len(self.batch_items):
            return
        item = self.batch_items[row]
        if self.table.cellWidget(row, 0) is None:
            self.table.setCellWidget(row, 0, self._create_row_remove_widget(item))

        name_it = QTableWidgetItem(Path(item.path).name)
        name_it.setToolTip(item.path)

        if item.meta:
            src_str = f"{item.meta['width']}x{item.meta['height']} • {item.meta['fps']:.0f}fps • {item.meta['codec_raw'].upper()}"
        else:
            src_str = "—"

        if item.analysis:
            m_cnt = len(item.analysis["missing_indices"])
            period = item.analysis["cadence_period"]
            miss_str = f"{m_cnt} (ogni {period}f)" if (m_cnt and period) else str(m_cnt)
            dup_str = str(len(item.analysis["duplicate_indices"]))
        else:
            miss_str = "—"
            dup_str = "—"

        cells = [
            name_it,
            QTableWidgetItem(src_str),
            QTableWidgetItem(miss_str),
            QTableWidgetItem(dup_str),
        ]
        for idx_c, cell in enumerate(cells):
            col = idx_c + 1
            if col > 1:
                cell.setTextAlignment(Qt.AlignCenter)
            self.table.setItem(row, col, cell)

    def _update_queue_header(self):
        n = len(self.batch_items)
        self.queue_title_lbl.setText(f"CODA BATCH ({n} VIDEO)")
        ready_count = sum(1 for it in self.batch_items if it.meta and it.analysis)
        if n <= 1:
            self.export_btn.setText("ESPORTA")
        else:
            self.export_btn.setText(f"ESPORTA TUTTI ({ready_count} VIDEO)")

        busy = (self.analysis_worker and self.analysis_worker.isRunning()) or (
            self.export_worker and self.export_worker.isRunning()
        )
        self.export_btn.setEnabled(ready_count > 0 and not busy)

    def remove_video_item(self, item: BatchVideoItem):
        if (self.analysis_worker and self.analysis_worker.isRunning()) or (
            self.export_worker and self.export_worker.isRunning()
        ):
            return
        try:
            row = self.batch_items.index(item)
        except ValueError:
            return
        self.batch_items.pop(row)
        self.table.removeRow(row)
        self._update_queue_header()
        if self.table.rowCount() > 0:
            self.table.selectRow(min(row, self.table.rowCount() - 1))
        else:
            self._user_customized_export = False
            self._clear_details_panel()
            self.progress_bar.setValue(0)
            self.progress_bar.setFormat("Pronto")

    def _clear_details_panel(self):
        self.meta_badge.setText("Nessun video selezionato")
        self.missing_box_val.setText("—")
        self.dup_box_val.setText("—")
        self.cuts_box_val.setText("—")
        self.log_box.clear()
        self.out_preview_lbl.setText("Output: —")

    def _on_table_selection_changed(self):
        row = self.get_selected_row()
        if row < 0 or row >= len(self.batch_items):
            self._clear_details_panel()
            return

        item = self.batch_items[row]
        self.out_preview_lbl.setText(f"File di output: {item.output_path}")

        if item.meta and item.analysis:
            meta = item.meta
            analysis = item.analysis
            self.meta_badge.setText(
                f"{Path(item.path).name}  •  {meta['width']}x{meta['height']}  •  "
                f"{meta['fps']:.2f} fps  •  {meta['codec_raw'].upper()} ({meta['pix_fmt']})"
            )
            missing = analysis["missing_indices"]
            duplicates = analysis["duplicate_indices"]
            cuts = analysis["scene_cuts"]
            period = analysis["cadence_period"]
            self.missing_box_val.setText(f"{len(missing)}  (ogni {period}f)" if period else str(len(missing)))
            self.dup_box_val.setText(str(len(duplicates)))
            self.cuts_box_val.setText(str(len(cuts)))
            self.log_box.setPlainText(item.report_text)
        elif item.error_msg:
            self.meta_badge.setText(f"{Path(item.path).name} — Errore")
            self.missing_box_val.setText("Err")
            self.dup_box_val.setText("Err")
            self.cuts_box_val.setText("Err")
            self.log_box.setPlainText(f"Errore durante l'analisi:\n{item.error_msg}")
        else:
            self.meta_badge.setText(f"{Path(item.path).name} — {item.status}")
            self.missing_box_val.setText("...")
            self.dup_box_val.setText("...")
            self.cuts_box_val.setText("...")
            self.log_box.setPlainText(f"In attesa di analisi per:\n{item.path}")

    def _on_user_option_changed(self):
        if self._updating_controls:
            return
        self._user_customized_export = True
        self.apply_settings_to_all()

    def apply_settings_to_all(self):
        fps_val = self.get_selected_fps()
        codec_val = self.get_selected_codec()
        for row, item in enumerate(self.batch_items):
            item.target_fps = fps_val
            item.target_codec = codec_val
            item.update_output_path()
            self._refresh_table_row(row)
        sel = self.get_selected_row()
        if 0 <= sel < len(self.batch_items):
            self.out_preview_lbl.setText(f"File di output: {self.batch_items[sel].output_path}")

    def _start_pending_analyses(self):
        if self.analysis_worker and self.analysis_worker.isRunning():
            return
        self.export_btn.setEnabled(False)
        self.analysis_worker = BatchAnalysisWorker(self.batch_items)
        self.analysis_worker.item_started.connect(self._on_analysis_item_started)
        self.analysis_worker.item_progress.connect(self._on_analysis_item_progress)
        self.analysis_worker.item_finished.connect(self._on_analysis_item_finished)
        self.analysis_worker.item_error.connect(self._on_analysis_item_error)
        self.analysis_worker.all_finished.connect(self._on_analysis_all_finished)
        self.analysis_worker.start()

    def _on_analysis_item_started(self, idx: int):
        if idx < len(self.batch_items):
            self.batch_items[idx].status = "Analisi in corso..."
            self._refresh_table_row(idx)
            self.table.selectRow(idx)

    def _on_analysis_item_progress(self, idx: int, pct: int, msg: str):
        total = len(self.batch_items)
        if idx < total:
            self.batch_items[idx].status = f"Analisi {pct}%"
            self._refresh_table_row(idx)
            name = Path(self.batch_items[idx].path).name
            self.progress_bar.setValue(pct)
            self.progress_bar.setFormat(f"[{idx+1}/{total}] {name}: {msg} ({pct}%)")

    def _on_analysis_item_finished(self, idx: int, meta: dict, analysis: dict):
        if idx >= len(self.batch_items):
            return
        item = self.batch_items[idx]
        item.meta = meta
        item.analysis = analysis

        # Se è il primo video analizzato e l'utente non ha ancora cambiato manualmente
        # le impostazioni, sincronizza i selettori globali con il formato del video e applicalo a tutta la coda
        if idx == 0 and not self._user_customized_export:
            self._updating_controls = True
            if meta["closest_fps"] in self.fps_Group_buttons:
                self.fps_Group_buttons[meta["closest_fps"]].setChecked(True)
            if meta["detected_codec"] in self.codec_buttons:
                self.codec_buttons[meta["detected_codec"]].setChecked(True)
            self._updating_controls = False

        item.report_text = build_report_text(meta, analysis)
        item.status = "Pronto"
        self.apply_settings_to_all()
        if self.get_selected_row() == idx:
            self._on_table_selection_changed()

    def _on_analysis_item_error(self, idx: int, err: str):
        if idx < len(self.batch_items):
            item = self.batch_items[idx]
            item.error_msg = err
            item.status = "Errore"
            self._refresh_table_row(idx)
            if self.get_selected_row() == idx:
                self._on_table_selection_changed()

    def _on_analysis_all_finished(self):
        # Controlla se altri file sono stati trascinati mentre l'analisi era in corso
        if any(it.analysis is None and not it.error_msg for it in self.batch_items):
            self._start_pending_analyses()
            return
        self._update_queue_header()
        self.progress_bar.setValue(100)
        self.progress_bar.setFormat(f"Analisi completata ({len(self.batch_items)} video pronti)")

    def start_batch_export(self):
        if (self.analysis_worker and self.analysis_worker.isRunning()) or (
            self.export_worker and self.export_worker.isRunning()
        ):
            return
        ready = [it for it in self.batch_items if it.meta and it.analysis]
        if not ready:
            return

        # Garantisce che tutti i video in coda usino le impostazioni di export selezionate
        self.apply_settings_to_all()

        self.export_btn.setEnabled(False)
        self.drop_area.setEnabled(False)

        self.export_worker = BatchExportWorker(self.batch_items)
        self.export_worker.item_started.connect(self._on_export_item_started)
        self.export_worker.item_progress.connect(self._on_export_item_progress)
        self.export_worker.item_finished.connect(self._on_export_item_finished)
        self.export_worker.item_error.connect(self._on_export_item_error)
        self.export_worker.all_finished.connect(self._on_export_all_finished)
        self.export_worker.start()

    def _on_export_item_started(self, idx: int):
        if idx < len(self.batch_items):
            self.batch_items[idx].exported = False
            self.batch_items[idx].status = "Esportazione 0%"
            self._refresh_table_row(idx)
            self.table.selectRow(idx)

    def _on_export_item_progress(self, idx: int, pct: int, msg: str):
        total = len(self.batch_items)
        if idx < total:
            self.batch_items[idx].status = f"Export {pct}%"
            self._refresh_table_row(idx)
            name = Path(self.batch_items[idx].path).name
            self.progress_bar.setValue(pct)
            self.progress_bar.setFormat(f"Esportazione [{idx+1}/{total}] {name}: {msg} ({pct}%)")

    def _on_export_item_finished(self, idx: int, out_path: str, written: int, fixed: int):
        if idx < len(self.batch_items):
            item = self.batch_items[idx]
            item.exported = True
            item.status = "✓ Completato"
            item.report_text += (
                f"\n\n[✓] ESPORTAZIONE COMPLETATA!\n"
                f"    • File salvato     : {out_path}\n"
                f"    • Anomalie corrette: {fixed} frame\n"
                f"    • Frame esportati  : {written} @ {item.target_fps} FPS ({item.target_codec.upper()})"
            )
            self._refresh_table_row(idx)
            if self.get_selected_row() == idx:
                self._on_table_selection_changed()

    def _on_export_item_error(self, idx: int, err: str):
        if idx < len(self.batch_items):
            item = self.batch_items[idx]
            item.error_msg = err
            item.status = "Errore Export"
            self._refresh_table_row(idx)

    def _on_export_all_finished(self, success_count: int, total_count: int):
        self.drop_area.setEnabled(True)
        self._update_queue_header()
        self.progress_bar.setValue(100)
        self.progress_bar.setFormat(f"Batch completato: {success_count}/{total_count} video esportati")

        out_list = "\n".join(
            f"• {Path(it.output_path).name}" for it in self.batch_items if it.exported
        )
        QMessageBox.information(
            self,
            "Esportazione Batch Completata",
            f"Esportati con successo {success_count}/{total_count} video!\n\n{out_list}",
        )


def _warmup_ffmpeg_async():
    """Controlla/prepara ffmpeg e ffprobe in background appena aperta la finestra."""
    try:
        get_ffmpeg_bin("ffmpeg")
        get_ffmpeg_bin("ffprobe")
    except Exception:
        pass


def main():
    if sys.platform == "win32":
        try:
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("monogrid.monovideofix.v0.7")
        except Exception:
            pass
    app = QApplication(sys.argv)
    app.setWindowIcon(create_app_icon())
    initial_files = sys.argv[1:] if len(sys.argv) > 1 else None
    win = VideoJudderFixerWindow(initial_videos=initial_files)
    win.show()
    threading.Thread(target=_warmup_ffmpeg_async, daemon=True).start()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()

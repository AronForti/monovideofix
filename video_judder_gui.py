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
from PyQt5.QtCore import QPointF, QRectF, Qt, QThread, QTimer, pyqtSignal
from PyQt5.QtGui import (
    QBrush,
    QColor,
    QDragEnterEvent,
    QDropEvent,
    QFont,
    QIcon,
    QImage,
    QKeySequence,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
    QPolygonF,
)
from PyQt5.QtWidgets import (
    QSizePolicy,
    QAbstractItemView,
    QApplication,
    QButtonGroup,
    QDialog,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QShortcut,
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

    plan = build_correction_plan(analysis)
    lines.append("-" * 68)
    lines.append(
        f"Conteggio frame: Originale {plan['source_count']} → Corretto {plan['output_count']}  "
        f"(+{len(plan['missing_set'])} mancanti ricostruiti, "
        f"−{len(plan['drop_dup_set'])} duplicati rimossi, "
        f"{len(plan['replace_dup_set'])} duplicati sostituiti)"
    )

    return "\n".join(lines)


def build_correction_plan(analysis: dict) -> dict:
    """
    Unica fonte di verità per l'export, l'anteprima e i report:
    - missing_set     : transizioni i→i+1 in cui manca un frame → si INSERISCE 1 frame interpolato (+1)
    - drop_dup_set    : frame duplicati puri → il frame ripetuto viene RIMOSSO (−1)
    - replace_dup_set : frame ripetuti al posto di quello mancante (duplicato seguito da salto)
                        → il frame viene SOSTITUITO da uno interpolato (conteggio invariato)
    """
    n_total = analysis["num_frames"]
    missing_set = set(analysis.get("missing_indices", []))
    ratios = analysis.get("ratios", [])

    replace_dup_set: set[int] = set()
    drop_dup_set: set[int] = set()
    for d_idx in analysis.get("duplicate_indices", []):
        if d_idx + 1 < len(ratios) and ratios[d_idx + 1] > 1.50:
            replace_dup_set.add(d_idx + 1)
        else:
            drop_dup_set.add(d_idx + 1)

    # Un frame mancante viene inserito solo se esiste il frame successivo
    inserted = sum(1 for i in missing_set if i + 1 < n_total)
    return {
        "missing_set": missing_set,
        "replace_dup_set": replace_dup_set,
        "drop_dup_set": drop_dup_set,
        "source_count": n_total,
        "output_count": n_total + inserted - len(drop_dup_set),
    }


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
            # Valuta TUTTE le fasi e tiene la migliore (non la prima che supera la soglia):
            # altrimenti una fase sfasata di 1 frame può vincere solo perché viene provata prima.
            best_score = None
            for phase in range(period):
                idxs = [i for i in range(phase, n - 1, period) if i not in cut_set]
                if len(idxs) < 2:
                    continue
                phase_ratios = [ratios[i] for i in idxs]
                high_count = sum(1 for r in phase_ratios if r >= 1.38)
                med_r = float(np.median(phase_ratios))
                if med_r >= 1.55 and high_count >= max(2, int(0.75 * len(idxs))):
                    score = (high_count, med_r)
                    if best_score is None or score > best_score:
                        best_score = score
                        best_phase = phase
            if best_score is not None:
                best_period = period
                break

        if best_period is not None:
            # Aggancio di fase: ogni salto atteso viene cercato a ±2 frame dalla posizione prevista
            # e la cadenza si ri-aggancia al picco reale (un duplicato o un taglio può spostarla di 1 frame).
            # Dove la scena si muove ma non c'è alcun salto, NON manca nessun frame → non si inserisce nulla.
            # Solo nelle scene quasi statiche (salto non misurabile) ci si fida della cadenza.
            pos = best_phase
            while pos < n - 1:
                window = [j for j in range(max(0, pos - 2), min(n - 1, pos + 3)) if j not in cut_set]
                peak = max(window, key=lambda j: ratios[j]) if window else None
                if peak is not None and ratios[peak] >= 1.38:
                    best_period_drops.append(peak)
                    pos = peak + period
                else:
                    if pos not in cut_set and local_refs[pos] <= 0.25:
                        best_period_drops.append(pos)
                    pos += period

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
    item_finished = pyqtSignal(int, str, int, int, int)
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
                written, fixed, removed = self._export_single(idx, item)
                success_count += 1
                self.item_finished.emit(idx, item.output_path, written, fixed, removed)
            except Exception as exc:
                self.item_error.emit(idx, str(exc))
        self.all_finished.emit(success_count, total_to_export)

    def _export_single(self, item_idx: int, item: BatchVideoItem) -> tuple[int, int, int]:
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

        plan = build_correction_plan(analysis)
        missing_set = plan["missing_set"]
        replace_dup_set = plan["replace_dup_set"]
        drop_dup_set = plan["drop_dup_set"]

        repaired_count = plan["output_count"]
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
        removed_count = 0
        written_count = 0

        prev_raw, prev_arr = read_one_frame()
        last_written_arr = prev_arr
        idx = 0
        pending_replace = False

        while prev_raw is not None:
            next_raw, next_arr = read_one_frame()

            if idx in drop_dup_set:
                removed_count += 1
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

        return written_count, reconstructed_count, removed_count


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


class PreviewGenerationWorker(QThread):
    """Genera i fotogrammi affiancati Prima (Originale) / Dopo (Corretto Optical Flow) per il singolo errore selezionato."""

    progress = pyqtSignal(int, str)
    finished_preview = pyqtSignal(dict)
    error = pyqtSignal(str)

    def __init__(
        self,
        item: BatchVideoItem,
        start_frame: int,
        num_frames: int = 24,
        focus_anomaly_frame: int | None = None,
        focus_anomaly_type: str | None = None,
    ):
        super().__init__()
        self.item = item
        self.start_frame = start_frame
        self.num_frames = num_frames
        self.focus_anomaly_frame = focus_anomaly_frame
        self.focus_anomaly_type = focus_anomaly_type

    def run(self):
        try:
            meta = self.item.meta
            analysis = self.item.analysis
            w_src, h_src = meta["width"], meta["height"]
            scale = min(1.0, 1120.0 / max(w_src, 1), 700.0 / max(h_src, 1))
            pw = max(64, int(round(w_src * scale / 2)) * 2)
            ph = max(64, int(round(h_src * scale / 2)) * 2)

            fps = meta["fps"] or 24.0
            n_total = analysis["num_frames"]
            s_frame = max(0, min(self.start_frame, max(0, n_total - 4)))
            count = min(self.num_frames, n_total - s_frame)
            start_sec = s_frame / fps

            self.progress.emit(10, "Estrazione fotogrammi del momento selezionato...")
            cmd = [
                get_ffmpeg_bin("ffmpeg"),
                "-v", "error",
                "-ss", f"{start_sec:.4f}",
                "-i", meta["path"],
                "-frames:v", str(count),
                "-vf", f"scale={pw}:{ph}",
                "-f", "rawvideo",
                "-pix_fmt", "rgb24",
                "-",
            ]
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, **get_subprocess_kwargs())
            frame_bytes = pw * ph * 3
            orig_frames: list[np.ndarray] = []
            while len(orig_frames) < count:
                raw = proc.stdout.read(frame_bytes)
                if len(raw) < frame_bytes:
                    break
                arr = np.frombuffer(raw, dtype=np.uint8).reshape((ph, pw, 3)).copy()
                orig_frames.append(arr)
            if proc.stdout:
                proc.stdout.close()
            proc.wait()

            if len(orig_frames) < 3:
                raise RuntimeError("Impossibile decodificare abbastanza fotogrammi per la preview.")

            # Isola esclusivamente il singolo errore associato alla thumbnail selezionata (1 thumbnail = 1 errore)
            if self.focus_anomaly_frame is not None:
                if self.focus_anomaly_type == "duplicate":
                    missing_set = set()
                    duplicate_set = {self.focus_anomaly_frame}
                else:
                    missing_set = {self.focus_anomaly_frame}
                    duplicate_set = set()
            else:
                missing_set = set()
                duplicate_set = set()

            plan = build_correction_plan({
                **analysis,
                "missing_indices": sorted(missing_set),
                "duplicate_indices": sorted(duplicate_set),
            })
            replace_dup_set = plan["replace_dup_set"]
            drop_dup_set = plan["drop_dup_set"]

            # Numero (0-based) del primo frame di questa finestra nel video esportato reale
            full_plan = build_correction_plan(analysis)
            out_offset = (
                s_frame
                + sum(1 for i in full_plan["missing_set"] if i < s_frame and i + 1 < n_total)
                - sum(1 for d in full_plan["drop_dup_set"] if d < s_frame)
            )

            # Le due corsie restano allineate nel TEMPO: dove un fotogramma non esiste
            # (mancante nel sorgente / rimosso nell'output) si usa uno slot vuoto (None),
            # mai una copia o un frame inventato. Così i conteggi reali restano diversi.
            orig_slots: list[np.ndarray | None] = []
            orig_status: list[str] = []
            orig_numbers: list[int | None] = []
            fixed_slots: list[np.ndarray | None] = []
            fixed_status: list[str] = []
            fixed_numbers: list[int | None] = []
            out_counter = 0

            def push(o_frame, o_st, o_num, f_frame, f_st):
                nonlocal out_counter
                orig_slots.append(o_frame)
                orig_status.append(o_st)
                orig_numbers.append(o_num)
                fixed_slots.append(f_frame)
                fixed_status.append(f_st)
                if f_frame is not None:
                    fixed_numbers.append(out_offset + out_counter)
                    out_counter += 1
                else:
                    fixed_numbers.append(None)

            def interp8(a8: np.ndarray, b8: np.ndarray) -> np.ndarray:
                mid16 = interpolate_midpoint_16bit(
                    a8.astype(np.uint16) << 8,
                    b8.astype(np.uint16) << 8,
                    alpha=0.5,
                )
                return (mid16 >> 8).astype(np.uint8)

            n_seg = len(orig_frames)
            last_written = orig_frames[0]

            for k in range(n_seg):
                g_idx = s_frame + k
                cur_f = orig_frames[k]
                next_f = orig_frames[k + 1] if (k + 1 < n_seg) else None

                if g_idx in drop_dup_set:
                    # Duplicato puro: l'export lo ELIMINA → nella corsia Corretto lo slot resta vuoto
                    push(cur_f, "duplicate", g_idx, None, "removed_dup")
                elif g_idx in replace_dup_set and next_f is not None:
                    # Frame ripetuto al posto di quello mancante: viene SOSTITUITO (conteggio invariato)
                    self.progress.emit(
                        20 + int(65 * k / max(n_seg, 1)),
                        f"Ricostruzione Optical Flow frame ripetuto #{g_idx}...",
                    )
                    mid8 = interp8(last_written, next_f)
                    push(cur_f, "duplicate", g_idx, mid8, "reconstructed_dup")
                    last_written = mid8
                else:
                    push(cur_f, "normal", g_idx, cur_f, "normal")
                    last_written = cur_f

                if g_idx in missing_set and next_f is not None:
                    # Frame mancante: nel sorgente NON esiste → slot vuoto; l'export ne INSERISCE uno interpolato
                    self.progress.emit(
                        20 + int(65 * k / max(n_seg, 1)),
                        f"Ricostruzione Optical Flow frame mancante #{g_idx}→#{g_idx+1}...",
                    )
                    mid8 = interp8(cur_f, next_f)
                    push(None, "missing_gap", None, mid8, "reconstructed_missing")
                    last_written = mid8

            self.progress.emit(90, "Calcolo curva del movimento frame-by-frame...")

            def compute_deltas(slots: list[np.ndarray | None]) -> list[float | None]:
                """Delta di movimento rispetto al fotogramma REALE precedente; None per gli slot vuoti."""
                deltas: list[float | None] = []
                prev = None
                for fr in slots:
                    if fr is None:
                        deltas.append(None)
                        continue
                    if prev is None:
                        deltas.append(0.0)
                    else:
                        deltas.append(float(np.mean(np.abs(fr.astype(np.float32) - prev.astype(np.float32)))))
                    prev = fr
                real_idx = [i for i, d in enumerate(deltas) if d is not None]
                if len(real_idx) > 1:
                    deltas[real_idx[0]] = deltas[real_idx[1]]
                return deltas

            orig_deltas = compute_deltas(orig_slots)
            fixed_deltas = compute_deltas(fixed_slots)

            self.progress.emit(100, "Anteprima pronta!")
            self.finished_preview.emit({
                "start_frame": s_frame,
                "fps": fps,
                "target_fps": self.item.target_fps,
                "orig_frames": orig_slots,
                "orig_frame_numbers": orig_numbers,
                "orig_deltas": orig_deltas,
                "orig_status": orig_status,
                "fixed_frames": fixed_slots,
                "fixed_frame_numbers": fixed_numbers,
                "fixed_deltas": fixed_deltas,
                "fixed_status": fixed_status,
                "orig_real_count": sum(1 for f in orig_slots if f is not None),
                "fixed_real_count": sum(1 for f in fixed_slots if f is not None),
                "video_source_count": full_plan["source_count"],
                "video_output_count": full_plan["output_count"],
                "focus_anomaly_frame": self.focus_anomaly_frame,
                "focus_anomaly_type": self.focus_anomaly_type,
            })
        except Exception as exc:
            self.error.emit(str(exc))


class MotionTimelineWidget(QWidget):
    """
    Timeline a Celle Contigue Frame-by-Frame (Stile Editor Video Professionale):
    - Ogni fotogramma è rappresentato da un blocco/cella contigua a larghezza piena (separata solo da 1px),
      eliminando il vuoto bianco fuorviante tra un fotogramma e l'altro.
    - Allineamento 1:1 perfetto tra corsia ORIGINALE (sopra), corsia CORRETTO (sotto) e il cursore/colonna
      del fotogramma attivo: la selezione cade sempre esattamente sopra la cella e la barra del frame corrente.
    """

    frame_selected = pyqtSignal(int)

    def __init__(self):
        super().__init__()
        self.setMinimumHeight(124)
        self.setCursor(Qt.PointingHandCursor)
        self.orig_deltas: list[float] = []
        self.orig_status: list[str] = []
        self.fixed_deltas: list[float] = []
        self.fixed_status: list[str] = []
        self.orig_frame_numbers: list[int] = []
        self.current_step: int = 0
        self.total_steps: int = 1

    def set_data(
        self,
        orig_deltas: list[float],
        orig_status: list[str],
        fixed_deltas: list[float],
        fixed_status: list[str],
        orig_frame_numbers: list[int | None] | None = None,
    ):
        self.orig_deltas = orig_deltas
        self.orig_status = orig_status
        self.fixed_deltas = fixed_deltas
        self.fixed_status = fixed_status
        self.orig_frame_numbers = orig_frame_numbers or []
        self.total_steps = max(len(orig_deltas), len(fixed_deltas), 1)
        self.current_step = 0
        self.update()

    def set_step(self, step: int):
        self.current_step = max(0, min(step, self.total_steps - 1))
        self.update()

    def _step_from_x(self, x_pos: float) -> int:
        left_pad = 138.0
        right_pad = 12.0
        usable_w = max(10.0, self.width() - left_pad - right_pad)
        rel_x = max(0.0, min(usable_w - 0.001, x_pos - left_pad))
        slot_w = usable_w / max(self.total_steps, 1)
        return max(0, min(int(rel_x // slot_w), self.total_steps - 1))

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton and self.total_steps >= 1:
            self.frame_selected.emit(self._step_from_x(event.pos().x()))

    def mouseMoveEvent(self, event):
        if event.buttons() & Qt.LeftButton and self.total_steps >= 1:
            self.frame_selected.emit(self._step_from_x(event.pos().x()))

    @staticmethod
    def _draw_empty_slot(p: QPainter, cell: QRectF, color: QColor):
        """Slot vuoto (fotogramma assente): tratteggio diagonale, bordo tratteggiato e una X centrale."""
        inner = cell.adjusted(1.0, 1.0, -1.0, -1.0)
        hatch = QColor(color)
        hatch.setAlpha(90)
        p.setPen(Qt.NoPen)
        p.setBrush(QBrush(hatch, Qt.BDiagPattern))
        p.drawRoundedRect(inner, 2.5, 2.5)
        border = QColor(color)
        border.setAlpha(170)
        p.setPen(QPen(border, 1.2, Qt.DashLine))
        p.setBrush(Qt.NoBrush)
        p.drawRoundedRect(inner, 2.5, 2.5)
        if cell.width() >= 12.0:
            p.setPen(color)
            p.setFont(QFont("Segoe UI", 9, QFont.ExtraBold))
            p.drawText(cell, Qt.AlignCenter, "\u2715")

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)

        rect = QRectF(self.rect())
        p.setPen(Qt.NoPen)
        p.setBrush(QColor("#FFFFFF"))
        p.drawRoundedRect(rect, 8.0, 8.0)

        if not self.orig_deltas and not self.fixed_deltas:
            p.setPen(QColor("#64748B"))
            p.setFont(QFont("Segoe UI", 10, QFont.Bold))
            p.drawText(rect, Qt.AlignCenter, "Generazione della timeline dei fotogrammi in corso...")
            p.end()
            return

        left_pad = 138.0
        right_pad = 12.0
        header_h = 20.0
        w = rect.width()
        h = rect.height()
        usable_w = max(10.0, w - left_pad - right_pad)
        n = max(len(self.orig_deltas), len(self.fixed_deltas), 1)
        slot_w = usable_w / n

        all_vals = [v for v in (self.orig_deltas + self.fixed_deltas) if v is not None] or [1.0]
        max_val = max(float(np.percentile(all_vals, 96)) * 1.30, 1.0)

        tracks_top = header_h + 2.0
        tracks_bottom = h - 4.0
        mid_y = tracks_top + (tracks_bottom - tracks_top) * 0.50

        # Sfondo griglia celle contigue (elimina l'effetto "barre isolate nel vuoto bianco")
        p.setPen(Qt.NoPen)
        p.setBrush(QColor("#E2E8F0"))
        p.drawRoundedRect(QRectF(left_pad - 1.0, 4.0, usable_w + 2.0, h - 8.0), 6.0, 6.0)

        # Etichette corsie a sinistra
        p.setFont(QFont("Segoe UI", 8, QFont.Bold))
        p.setPen(QColor("#64748B"))
        p.drawText(
            QRectF(10, 3, left_pad - 14, header_h - 2),
            Qt.AlignLeft | Qt.AlignVCenter,
            "FRAME #",
        )

        p.setFont(QFont("Segoe UI", 8, QFont.ExtraBold))
        p.setPen(QColor("#DC2626"))
        p.drawText(
            QRectF(10, tracks_top + 2, left_pad - 14, mid_y - tracks_top - 4),
            Qt.AlignLeft | Qt.AlignVCenter,
            "ORIGINALE\nRosso = Errore\n\u2715 = frame assente",
        )
        p.setPen(QColor("#16A34A"))
        p.drawText(
            QRectF(10, mid_y + 2, left_pad - 14, tracks_bottom - mid_y - 4),
            Qt.AlignLeft | Qt.AlignVCenter,
            "CORRETTO\nVerde = Ricostruito\n\u2715 = frame rimosso",
        )

        # Disegna ogni fotogramma come una cella contigua (gap di appena 1.5px tra fotogrammi adiacenti)
        gap = 1.5
        for i in range(n):
            cell_x = left_pad + i * slot_w + gap * 0.5
            cell_w = max(4.0, slot_w - gap)
            is_cur = (i == self.current_step)

            st_o = self.orig_status[i] if i < len(self.orig_status) else "normal"
            st_f = self.fixed_status[i] if i < len(self.fixed_status) else "normal"
            val_o = self.orig_deltas[i] if i < len(self.orig_deltas) else 0.0
            val_f = self.fixed_deltas[i] if i < len(self.fixed_deltas) else 0.0
            f_num = self.orig_frame_numbers[i] if i < len(self.orig_frame_numbers) else (i + 1)
            is_err_col = st_o in ("duplicate", "missing_jump", "missing_gap") or st_f == "removed_dup"

            # 1. Cella Header con numero del fotogramma
            hdr_rect = QRectF(cell_x, 5.0, cell_w, header_h - 3.0)
            p.setPen(Qt.NoPen)
            if is_cur:
                p.setBrush(QColor("#0047FF"))
            elif is_err_col:
                p.setBrush(QColor("#FEE2E2"))
            else:
                p.setBrush(QColor("#F1F5F9"))
            p.drawRoundedRect(hdr_rect, 3.0, 3.0)

            if cell_w >= 20.0:
                p.setFont(QFont("Segoe UI", 7, QFont.ExtraBold if is_cur else QFont.Bold))
                if is_cur:
                    p.setPen(QColor("#FFFFFF"))
                elif is_err_col:
                    p.setPen(QColor("#DC2626"))
                else:
                    p.setPen(QColor("#475569"))
                p.drawText(hdr_rect, Qt.AlignCenter, "\u2715" if f_num is None else f"#{f_num}")

            # 2. Cella Corsia Superiore (ORIGINALE)
            orig_cell = QRectF(cell_x, tracks_top + 1.0, cell_w, (mid_y - tracks_top) - 2.0)
            p.setPen(Qt.NoPen)
            if st_o in ("duplicate", "missing_jump"):
                p.setBrush(QColor("#FEF2F2"))
            elif is_cur:
                p.setBrush(QColor("#EFF6FF"))
            else:
                p.setBrush(QColor("#F8FAFC"))
            p.drawRoundedRect(orig_cell, 3.0, 3.0)

            # Riempimento movimento Originale dentro la cella (a tutta larghezza della cella!)
            inner_pad = 2.0
            max_bar_h_o = max(4.0, orig_cell.height() - 4.0)
            orig_empty = st_o == "missing_gap" or val_o is None
            if orig_empty:
                # Slot vuoto: nel sorgente questo fotogramma NON esiste
                self._draw_empty_slot(p, orig_cell, QColor("#EA580C"))
                bar_h_o, bar_col_o = 0.0, QColor("#EA580C")
            elif st_o == "duplicate":
                bar_h_o = max_bar_h_o
                bar_col_o = QColor("#DC2626")
            elif st_o == "missing_jump":
                bar_h_o = max_bar_h_o
                bar_col_o = QColor("#EA580C")
            else:
                bar_h_o = max(4.0, min(1.0, val_o / max_val) * max_bar_h_o)
                bar_col_o = QColor("#64748B") if is_cur else QColor("#94A3B8")

            if not orig_empty:
                bar_rect_o = QRectF(
                    orig_cell.left() + inner_pad,
                    orig_cell.bottom() - 2.0 - bar_h_o,
                    max(2.0, orig_cell.width() - inner_pad * 2.0),
                    bar_h_o,
                )
                p.setPen(Qt.NoPen)
                p.setBrush(bar_col_o)
                p.drawRoundedRect(bar_rect_o, 2.5, 2.5)

            # 3. Cella Corsia Inferiore (CORRETTO)
            fixed_cell = QRectF(cell_x, mid_y + 1.0, cell_w, (tracks_bottom - mid_y) - 2.0)
            p.setPen(Qt.NoPen)
            if st_f in ("reconstructed_missing", "reconstructed_dup"):
                p.setBrush(QColor("#F0FDF4"))
            elif st_f == "removed_dup":
                p.setBrush(QColor("#FEF2F2"))
            elif is_cur:
                p.setBrush(QColor("#EFF6FF"))
            else:
                p.setBrush(QColor("#F8FAFC"))
            p.drawRoundedRect(fixed_cell, 3.0, 3.0)

            if st_f == "removed_dup" or val_f is None:
                # Slot vuoto: il duplicato viene eliminato dal video esportato
                self._draw_empty_slot(p, fixed_cell, QColor("#DC2626"))
                continue

            max_bar_h_f = max(4.0, fixed_cell.height() - 4.0)
            bar_h_f = max(4.0, min(1.0, val_f / max_val) * max_bar_h_f)
            if st_f in ("reconstructed_missing", "reconstructed_dup"):
                bar_col_f = QColor("#16A34A")
                bar_h_f = max(bar_h_f, max_bar_h_f * 0.65)
            else:
                bar_col_f = QColor("#0047FF") if is_cur else QColor("#3B82F6")

            bar_rect_f = QRectF(
                fixed_cell.left() + inner_pad,
                fixed_cell.bottom() - 2.0 - bar_h_f,
                max(2.0, fixed_cell.width() - inner_pad * 2.0),
                bar_h_f,
            )
            p.setBrush(bar_col_f)
            p.drawRoundedRect(bar_rect_f, 2.5, 2.5)

        # 4. Riquadro di selezione del Frame Corrente perfettamente sovrapposto alla cella attiva
        if 0 <= self.current_step < n:
            cur_x = left_pad + self.current_step * slot_w + gap * 0.5
            cur_w = max(4.0, slot_w - gap)
            sel_rect = QRectF(cur_x, 5.0, cur_w, h - 10.0)
            p.setBrush(Qt.NoBrush)
            p.setPen(QPen(QColor("#0047FF"), 2.4))
            p.drawRoundedRect(sel_rect, 4.0, 4.0)

        p.end()


class AnomalyThumbnailsWorker(QThread):
    """Estrae in background le miniature (thumbnail) dei vari momenti in cui si verifica un frame duplicato o mancante."""

    thumb_ready = pyqtSignal(int, int, QImage)

    def __init__(self, video_path: str, fps: float, anomaly_frames: list[int]):
        super().__init__()
        self.video_path = video_path
        self.fps = fps or 24.0
        self.anomaly_frames = anomaly_frames
        self._abort = False

    def stop(self):
        self._abort = True

    def run(self):
        tw, th = 112, 64
        frame_bytes = tw * th * 3
        for pos, f_idx in enumerate(self.anomaly_frames):
            if self._abort:
                break
            t_sec = f_idx / self.fps
            cmd = [
                get_ffmpeg_bin("ffmpeg"),
                "-v", "error",
                "-ss", f"{t_sec:.4f}",
                "-i", self.video_path,
                "-frames:v", "1",
                "-vf", f"scale={tw}:{th}:force_original_aspect_ratio=decrease,pad={tw}:{th}:(ow-iw)/2:(oh-ih)/2:color=black",
                "-f", "rawvideo",
                "-pix_fmt", "rgb24",
                "-",
            ]
            try:
                raw = subprocess.check_output(cmd, **get_subprocess_kwargs())
                if len(raw) >= frame_bytes:
                    arr = np.frombuffer(raw[:frame_bytes], dtype=np.uint8).reshape((th, tw, 3)).copy()
                    qimg = QImage(arr.data, tw, th, tw * 3, QImage.Format_RGB888).copy()
                    self.thumb_ready.emit(pos, f_idx, qimg)
            except Exception:
                pass


class AnomalyThumbnailCard(QFrame):
    """Card cliccabile con miniatura del momento esatto in cui avviene un frame duplicato o mancante."""

    clicked = pyqtSignal(int)

    def __init__(self, pos_idx: int, frame_idx: int, time_sec: float, anomaly_type: str):
        super().__init__()
        self.pos_idx = pos_idx
        self.frame_idx = frame_idx
        self.anomaly_type = anomaly_type
        self._selected = False

        self.setFixedSize(128, 100)
        self.setCursor(Qt.PointingHandCursor)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(6, 5, 6, 5)
        lay.setSpacing(3)

        self.img_lbl = QLabel("...")
        self.img_lbl.setFixedSize(114, 62)
        self.img_lbl.setAlignment(Qt.AlignCenter)
        self.img_lbl.setStyleSheet(
            "background-color: #E2E8F0; color: #64748B; font-size: 10px; font-weight: 700; border-radius: 4px;"
        )
        lay.addWidget(self.img_lbl, alignment=Qt.AlignCenter)

        tag_short = "DUPLICATO" if anomaly_type == "duplicate" else "MANCANTE"
        tag_color = "#DC2626" if anomaly_type == "duplicate" else "#D97706"
        self.info_lbl = QLabel(f"#{frame_idx} ({time_sec:.2f}s) \u2022 {tag_short}")
        self.info_lbl.setAlignment(Qt.AlignCenter)
        self.info_lbl.setFixedHeight(18)
        self.info_lbl.setStyleSheet(
            f"background: transparent; border: none; color: {tag_color}; font-size: 9px; font-weight: 800;"
        )
        lay.addWidget(self.info_lbl)

        self._apply_card_style()

    def set_thumbnail(self, qimg: QImage):
        pix = QPixmap.fromImage(qimg).scaled(
            self.img_lbl.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation
        )
        self.img_lbl.setText("")
        self.img_lbl.setPixmap(pix)

    def set_selected(self, selected: bool):
        if self._selected != selected:
            self._selected = selected
            self._apply_card_style()

    def _apply_card_style(self):
        if self._selected:
            self.setStyleSheet(
                "AnomalyThumbnailCard { background-color: #FFFFFF; border: 2px solid #0047FF; border-radius: 7px; }"
            )
        else:
            self.setStyleSheet(
                "AnomalyThumbnailCard { background-color: rgba(255, 255, 255, 0.88); border: 2px solid transparent; border-radius: 7px; }"
                "AnomalyThumbnailCard:hover { background-color: #FFFFFF; border: 2px solid #93C5FD; }"
            )

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.clicked.emit(self.pos_idx)


class SyncZoomVideoView(QWidget):
    """
    Visualizzatore video interattivo con Zoom (rotellina del mouse) e Pan/Trascinamento (click sinistro),
    progettato per sincronizzare in tempo reale ingrandimento e posizione con il frame di confronto:
    - Rotellina del mouse: Zoom progressivo (1.0x .. 8.0x) centrato esattamente sul punto sotto il cursore.
    - Trascinamento (Click sinistro + muovi): Sposta l'inquadratura quando zoomato (> 1.0x).
    - Doppio click sinistro (o tasto destro): Ripristina istantaneamente la vista al 100% (1.0x).
    - Qualsiasi modifica emette `view_changed(zoom, center_u, center_v)` per replicare all'istante
      lo stesso identico zoom e movimento sull'altro pannello (Prima <-> Dopo).
    """

    view_changed = pyqtSignal(float, float, float)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumSize(480, 290)
        self._qimg: QImage | None = None
        self._placeholder_text: str = "In caricamento..."
        self._zoom: float = 1.0
        self._min_zoom: float = 1.0
        self._max_zoom: float = 8.0
        # Coordinate normalizzate (0.0 .. 1.0) del centro dell'immagine attualmente visibile
        self._center_u: float = 0.5
        self._center_v: float = 0.5
        self._dragging: bool = False
        self._last_mouse_pos: QPointF = QPointF()
        self.setMouseTracking(True)
        self._update_cursor()

    def setText(self, text: str):
        self._placeholder_text = text
        self._qimg = None
        self.update()

    def set_frame_array(self, arr: np.ndarray):
        h, w, _ = arr.shape
        self._qimg = QImage(arr.data, w, h, w * 3, QImage.Format_RGB888).copy()
        self.update()

    def _clamp_center(self):
        if self._zoom <= 1.0001:
            self._zoom = 1.0
            self._center_u = 0.5
            self._center_v = 0.5
        else:
            half_span = 0.5 / self._zoom
            self._center_u = max(half_span, min(1.0 - half_span, self._center_u))
            self._center_v = max(half_span, min(1.0 - half_span, self._center_v))

    def _update_cursor(self):
        if self._dragging:
            self.setCursor(Qt.ClosedHandCursor)
        elif self._zoom > 1.001:
            self.setCursor(Qt.OpenHandCursor)
        else:
            self.setCursor(Qt.CrossCursor)

    def set_view_state(self, zoom: float, center_u: float, center_v: float, emit_signal: bool = False):
        self._zoom = max(self._min_zoom, min(self._max_zoom, float(zoom)))
        self._center_u = float(center_u)
        self._center_v = float(center_v)
        self._clamp_center()
        self._update_cursor()
        self.update()
        if emit_signal:
            self.view_changed.emit(self._zoom, self._center_u, self._center_v)

    def reset_zoom(self, emit_signal: bool = True):
        self.set_view_state(1.0, 0.5, 0.5, emit_signal=emit_signal)

    def step_zoom(self, factor: float, emit_signal: bool = True):
        new_zoom = max(self._min_zoom, min(self._max_zoom, self._zoom * factor))
        self.set_view_state(new_zoom, self._center_u, self._center_v, emit_signal=emit_signal)

    def zoom_level(self) -> float:
        return self._zoom

    def _compute_dest_rect(self) -> QRectF:
        w = max(1.0, float(self.width()))
        h = max(1.0, float(self.height()))
        if self._qimg is None or self._qimg.isNull():
            return QRectF(0.0, 0.0, w, h)
        iw = float(self._qimg.width())
        ih = float(self._qimg.height())
        scale = min(w / iw, h / ih)
        dw = iw * scale
        dh = ih * scale
        dx = (w - dw) * 0.5
        dy = (h - dh) * 0.5
        return QRectF(dx, dy, dw, dh)

    def wheelEvent(self, event):
        if self._qimg is None or self._qimg.isNull():
            return
        delta = event.angleDelta().y()
        if delta == 0:
            return
        factor = 1.22 if delta > 0 else (1.0 / 1.22)
        old_zoom = self._zoom
        new_zoom = max(self._min_zoom, min(self._max_zoom, old_zoom * factor))
        if abs(new_zoom - old_zoom) < 1e-4:
            return

        dest = self._compute_dest_rect()
        if dest.width() > 1.0 and dest.height() > 1.0:
            pos = event.pos()
            rel_x = max(0.0, min(1.0, (pos.x() - dest.left()) / dest.width()))
            rel_y = max(0.0, min(1.0, (pos.y() - dest.top()) / dest.height()))
            # Punto dell'immagine (0..1) attualmente sotto il cursore
            u_cursor = self._center_u + (rel_x - 0.5) / old_zoom
            v_cursor = self._center_v + (rel_y - 0.5) / old_zoom
            # Mantieni lo stesso punto dell'immagine ancorato sotto il cursore al nuovo zoom
            self._center_u = u_cursor - (rel_x - 0.5) / new_zoom
            self._center_v = v_cursor - (rel_y - 0.5) / new_zoom

        self._zoom = new_zoom
        self._clamp_center()
        self._update_cursor()
        self.update()
        self.view_changed.emit(self._zoom, self._center_u, self._center_v)
        event.accept()

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton and self._zoom > 1.001:
            self._dragging = True
            self._last_mouse_pos = QPointF(event.pos())
            self._update_cursor()
            event.accept()
        elif event.button() == Qt.RightButton and self._zoom > 1.001:
            self.reset_zoom(emit_signal=True)
            event.accept()
        else:
            super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._dragging and self._qimg is not None and not self._qimg.isNull():
            cur_pos = QPointF(event.pos())
            delta = cur_pos - self._last_mouse_pos
            self._last_mouse_pos = cur_pos
            dest = self._compute_dest_rect()
            if dest.width() > 1.0 and dest.height() > 1.0:
                du = -delta.x() / (dest.width() * self._zoom)
                dv = -delta.y() / (dest.height() * self._zoom)
                self._center_u += du
                self._center_v += dv
                self._clamp_center()
                self.update()
                self.view_changed.emit(self._zoom, self._center_u, self._center_v)
            event.accept()
        else:
            super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton and self._dragging:
            self._dragging = False
            self._update_cursor()
            event.accept()
        else:
            super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event):
        if event.button() == Qt.LeftButton:
            if self._zoom > 1.001:
                self.reset_zoom(emit_signal=True)
            elif self._qimg is not None and not self._qimg.isNull():
                # Doppio click a 1.0x -> ingrandisce a 2.5x nel punto cliccato
                dest = self._compute_dest_rect()
                new_zoom = 2.5
                if dest.width() > 1.0 and dest.height() > 1.0:
                    pos = event.pos()
                    rel_x = max(0.0, min(1.0, (pos.x() - dest.left()) / dest.width()))
                    rel_y = max(0.0, min(1.0, (pos.y() - dest.top()) / dest.height()))
                    self._center_u = rel_x
                    self._center_v = rel_y
                self.set_view_state(new_zoom, self._center_u, self._center_v, emit_signal=True)
            event.accept()
        else:
            super().mouseDoubleClickEvent(event)

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setRenderHint(QPainter.SmoothPixmapTransform, True)

        rect = QRectF(0.0, 0.0, float(self.width()), float(self.height()))
        bg_path = QPainterPath()
        bg_path.addRoundedRect(rect, 6.0, 6.0)
        p.fillPath(bg_path, QColor("#F8FAFC"))

        if self._qimg is None or self._qimg.isNull():
            p.setPen(QColor("#334155"))
            p.setFont(QFont("Segoe UI", 10, QFont.DemiBold))
            p.drawText(rect, Qt.AlignCenter, self._placeholder_text)
            return

        p.save()
        p.setClipPath(bg_path)

        iw = float(self._qimg.width())
        ih = float(self._qimg.height())
        src_w = iw / self._zoom
        src_h = ih / self._zoom
        src_x = self._center_u * iw - src_w * 0.5
        src_y = self._center_v * ih - src_h * 0.5
        src_rect = QRectF(src_x, src_y, src_w, src_h)
        dest_rect = self._compute_dest_rect()

        p.drawImage(dest_rect, self._qimg, src_rect)

        # Badge compatto in sovrimpressione quando lo zoom è attivo (> 1.0x)
        if self._zoom > 1.01:
            badge_text = f"🔍 {self._zoom:.1f}x Sync"
            p.setFont(QFont("Segoe UI", 8, QFont.Bold))
            fm = p.fontMetrics()
            bw = fm.horizontalAdvance(badge_text) + 14
            bh = 20
            bx = dest_rect.right() - bw - 8
            by = dest_rect.top() + 8
            b_rect = QRectF(bx, by, float(bw), float(bh))
            p.setPen(QPen(QColor("#0047FF"), 1.2))
            p.setBrush(QColor(255, 255, 255, 232))
            p.drawRoundedRect(b_rect, 5.0, 5.0)
            p.setPen(QColor("#0047FF"))
            p.drawText(b_rect, Qt.AlignCenter, badge_text)

        p.restore()


class PreviewComparisonDialog(QDialog):
    """
    Finestra di Anteprima Confronto Prima / Dopo (Opzione 1 con strumenti diagnostici):
    1. Rallentatore 'Microscopio' (0.25x / 0.5x / 1x) + Navigazione frame tramite frecce Sinistra/Destra.
    2. Galleria di Thumbnail cliccabili per ciascun momento in cui avviene un frame duplicato o mancante.
    3. Semaforo statico a zero jitter su Bordo & Badge (dimensioni fisse, nessun ricalcolo layout).
    4. Timeline 'Elettrocardiogramma' (Grafico del Movimento Frame-by-Frame cliccabile).
    5. Modalità 'Heat-Map Movimento' ad alta chiarezza (sovrapposizione termica dei pixel in movimento sul video desaturato).
    6. Zoom e Pan sincronizzati in tempo reale tra il frame Originale e il frame Corretto.
    """

    def __init__(self, parent: QWidget, item: BatchVideoItem):
        super().__init__(parent)
        self.item = item
        self.meta = item.meta
        self.analysis = item.analysis

        self.setWindowTitle(f"Anteprima & Verifica Correzione — {Path(item.path).name}")
        self.setWindowIcon(create_app_icon())
        self.setMinimumSize(1080, 830)

        self.preview_data: dict | None = None
        self.current_step: int = 0
        self.total_steps: int = 1
        self.speed_factor: float = 0.25  # Default 0.25x (Rallentatore Microscopio per rendere palese ogni scatto)
        self.diff_mode: bool = False
        self.worker: PreviewGenerationWorker | None = None
        self.thumb_worker: AnomalyThumbnailsWorker | None = None
        self._last_orig_state: str = ""
        self._last_fixed_state: str = ""
        self._focus_anomaly_frame: int | None = None

        missing_set = set(self.analysis.get("missing_indices", []))
        dup_set = set(self.analysis.get("duplicate_indices", []))
        self.anomaly_frames = sorted(missing_set | dup_set)
        self.anomaly_types = {
            f_idx: ("duplicate" if f_idx in dup_set else "missing_jump")
            for f_idx in self.anomaly_frames
        }
        self.current_anomaly_pos = 0
        if self.anomaly_frames:
            self._focus_anomaly_frame = self.anomaly_frames[0]
        self.thumb_cards: dict[int, AnomalyThumbnailCard] = {}

        self.timer = QTimer(self)
        self.timer.timeout.connect(self._advance_frame)

        self._build_ui()
        self._setup_shortcuts()
        self._start_thumbnails_worker()
        self._start_generation_for_current_segment()

    def _setup_shortcuts(self):
        self._sc_left = QShortcut(QKeySequence(Qt.Key_Left), self)
        self._sc_left.setContext(Qt.WindowShortcut)
        self._sc_left.activated.connect(lambda: self.step_manual(-1))

        self._sc_right = QShortcut(QKeySequence(Qt.Key_Right), self)
        self._sc_right.setContext(Qt.WindowShortcut)
        self._sc_right.activated.connect(lambda: self.step_manual(1))

        self._sc_space = QShortcut(QKeySequence(Qt.Key_Space), self)
        self._sc_space.setContext(Qt.WindowShortcut)
        self._sc_space.activated.connect(self.toggle_play)

    def _compute_segment_for_anomaly(self, pos_idx: int) -> tuple[int, int, int | None, str | None]:
        """
        Calcola l'intervallo di frame centrato esclusivamente sull'anomalia selezionata
        (evitando di sovrapporsi alle anomalie precedenti o successive) così che a ogni
        thumbnail corrisponda sempre 1 solo errore nella barra sottostante.
        """
        n_total = self.analysis["num_frames"]
        if not self.anomaly_frames:
            s_f = max(0, min(int(n_total * 0.25), max(0, n_total - 24)))
            return s_f, min(24, n_total - s_f), None, None

        pos = pos_idx % len(self.anomaly_frames)
        target = self.anomaly_frames[pos]
        a_type = self.anomaly_types.get(target, "missing_jump")

        half_win = 10  # ~20-21 fotogrammi centrati sul singolo errore
        low = max(0, target - half_win)
        high = min(n_total - 1, target + half_win)

        if pos > 0:
            prev_anom = self.anomaly_frames[pos - 1]
            mid_prev = (prev_anom + target) // 2 + 1
            low = max(low, min(mid_prev, target - 3))
        if pos + 1 < len(self.anomaly_frames):
            next_anom = self.anomaly_frames[pos + 1]
            mid_next = (target + next_anom) // 2
            high = min(high, max(mid_next, target + 3))

        low = max(0, min(low, max(0, n_total - 8)))
        count = max(8, min(n_total - low, high - low + 1))
        return low, count, target, a_type

    def _build_ui(self):
        self.setStyleSheet(
            """
            QDialog {
                background-color: #0047FF;
                color: #FFFFFF;
                font-family: 'Segoe UI', sans-serif;
            }
            QFrame#PreviewCard {
                background-color: #0035C9;
                border: 1px solid rgba(255, 255, 255, 0.30);
                border-radius: 10px;
            }
            QFrame#VideoPanel {
                background-color: #FFFFFF;
                border: 2px solid #E2E8F0;
                border-radius: 10px;
            }
            QScrollArea#ThumbScroll {
                background: transparent;
                border: none;
            }
            QScrollBar:horizontal {
                background: rgba(255, 255, 255, 0.15);
                height: 8px;
                border-radius: 4px;
            }
            QScrollBar::handle:horizontal {
                background: #FFFFFF;
                min-width: 30px;
                border-radius: 4px;
            }
            QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {
                width: 0px;
            }
            QPushButton#CtrlBtn {
                background-color: rgba(255, 255, 255, 0.16);
                border: 1px solid rgba(255, 255, 255, 0.45);
                border-radius: 7px;
                padding: 7px 13px;
                color: #FFFFFF;
                font-size: 12px;
                font-weight: 800;
            }
            QPushButton#CtrlBtn:hover {
                background-color: rgba(255, 255, 255, 0.28);
                border-color: #FFFFFF;
            }
            QPushButton#CtrlBtn:checked {
                background-color: #FFFFFF;
                border-color: #FFFFFF;
                color: #0047FF;
                font-weight: 900;
            }
            QPushButton#PrimaryCtrlBtn {
                background-color: #FFFFFF;
                border: none;
                border-radius: 7px;
                padding: 8px 18px;
                color: #0047FF;
                font-size: 13px;
                font-weight: 900;
            }
            QPushButton#PrimaryCtrlBtn:hover {
                background-color: #E2EBFF;
            }
            """
        )

        root = QVBoxLayout(self)
        root.setContentsMargins(18, 14, 18, 16)
        root.setSpacing(10)

        # Barra superiore informativa
        top_bar = QHBoxLayout()
        self.header_lbl = QLabel(
            f"VERIFICA VISIVA MICRO-SCATTI: {Path(self.item.path).name}  \u2022  Target: {self.item.target_fps} FPS"
        )
        self.header_lbl.setStyleSheet("font-size: 13px; font-weight: 900; color: #FFFFFF;")
        zoom_hint_lbl = QLabel("🔍 Rotellina: Zoom sincronizzato  •  Trascina: Sposta vista  •  Doppio click: Reset 100%")
        zoom_hint_lbl.setStyleSheet("font-size: 11px; font-weight: 700; color: #E2EBFF;")
        top_bar.addWidget(self.header_lbl)
        top_bar.addStretch()
        top_bar.addWidget(zoom_hint_lbl)
        root.addLayout(top_bar)

        # Riquadri Video Affiancati (Prima / Dopo) su superficie Bianca con Badge compatti a larghezza/altezza fissa (zero scatti UI)
        videos_row = QHBoxLayout()
        videos_row.setSpacing(14)

        # Pannello Sinistro: ORIGINALE
        self.orig_panel = QFrame()
        self.orig_panel.setObjectName("VideoPanel")
        orig_lay = QVBoxLayout(self.orig_panel)
        orig_lay.setContentsMargins(10, 8, 10, 8)
        orig_lay.setSpacing(6)

        orig_top = QHBoxLayout()
        orig_top.setContentsMargins(0, 0, 0, 0)
        orig_title = QLabel("PRIMA — VIDEO ORIGINALE")
        orig_title.setFixedHeight(22)
        orig_title.setStyleSheet("color: #0F172A; font-size: 11px; font-weight: 900;")
        self.orig_badge = QLabel("In caricamento...")
        self.orig_badge.setFixedSize(195, 22)
        self.orig_badge.setAlignment(Qt.AlignCenter)
        self.orig_badge.setStyleSheet(
            "background-color: #F1F5F9; color: #64748B; border: 1px solid #CBD5E1; "
            "font-size: 10px; font-weight: 700; border-radius: 4px;"
        )
        orig_top.addWidget(orig_title)
        orig_top.addStretch()
        orig_top.addWidget(self.orig_badge)
        orig_lay.addLayout(orig_top)

        self.orig_img_lbl = SyncZoomVideoView()
        self.orig_img_lbl.setToolTip(
            "Rotellina del mouse: Zoom avanti/indietro sincronizzato\n"
            "Click sinistro + trascina: Sposta l'inquadratura su entrambi i frame\n"
            "Doppio click (o tasto destro): Reset vista al 100%"
        )
        orig_lay.addWidget(self.orig_img_lbl, stretch=1)

        self.orig_meta_lbl = QLabel("Frame: —")
        self.orig_meta_lbl.setFixedHeight(18)
        self.orig_meta_lbl.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        self.orig_meta_lbl.setStyleSheet("color: #475569; font-size: 11px; font-weight: 700;")
        orig_lay.addWidget(self.orig_meta_lbl)

        # Pannello Destro: CORRETTO
        self.fixed_panel = QFrame()
        self.fixed_panel.setObjectName("VideoPanel")
        fixed_lay = QVBoxLayout(self.fixed_panel)
        fixed_lay.setContentsMargins(10, 8, 10, 8)
        fixed_lay.setSpacing(6)

        fixed_top = QHBoxLayout()
        fixed_top.setContentsMargins(0, 0, 0, 0)
        fixed_title = QLabel("DOPO — VIDEO CORRETTO (OPTICAL FLOW)")
        fixed_title.setFixedHeight(22)
        fixed_title.setStyleSheet("color: #0047FF; font-size: 11px; font-weight: 900;")
        self.fixed_badge = QLabel("In caricamento...")
        self.fixed_badge.setFixedSize(195, 22)
        self.fixed_badge.setAlignment(Qt.AlignCenter)
        self.fixed_badge.setStyleSheet(
            "background-color: #F8FAFC; color: #64748B; border: 1px solid #E2E8F0; "
            "font-size: 10px; font-weight: 700; border-radius: 4px;"
        )
        fixed_top.addWidget(fixed_title)
        fixed_top.addStretch()
        fixed_top.addWidget(self.fixed_badge)
        fixed_lay.addLayout(fixed_top)

        self.fixed_img_lbl = SyncZoomVideoView()
        self.fixed_img_lbl.setToolTip(
            "Rotellina del mouse: Zoom avanti/indietro sincronizzato\n"
            "Click sinistro + trascina: Sposta l'inquadratura su entrambi i frame\n"
            "Doppio click (o tasto destro): Reset vista al 100%"
        )
        fixed_lay.addWidget(self.fixed_img_lbl, stretch=1)

        # Sincronizzazione bidirezionale di Zoom e Movimento tra PRIMA e DOPO
        self.orig_img_lbl.view_changed.connect(self._on_orig_view_changed)
        self.fixed_img_lbl.view_changed.connect(self._on_fixed_view_changed)

        self.fixed_meta_lbl = QLabel("Frame: —")
        self.fixed_meta_lbl.setFixedHeight(18)
        self.fixed_meta_lbl.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        self.fixed_meta_lbl.setStyleSheet("color: #475569; font-size: 11px; font-weight: 700;")
        fixed_lay.addWidget(self.fixed_meta_lbl)

        videos_row.addWidget(self.orig_panel, stretch=1)
        videos_row.addWidget(self.fixed_panel, stretch=1)
        root.addLayout(videos_row, stretch=1)

        # Galleria Thumbnail dei momenti con Frame Duplicato o Mancante
        thumbs_card = QFrame()
        thumbs_card.setObjectName("PreviewCard")
        thumbs_lay = QVBoxLayout(thumbs_card)
        thumbs_lay.setContentsMargins(12, 8, 12, 8)
        thumbs_lay.setSpacing(6)

        thumbs_hdr = QHBoxLayout()
        n_anom = len(self.anomaly_frames)
        thumbs_title = QLabel(
            f"MOMENTI RILEVATI CON FRAME DUPLICATO O MANCANTE ({n_anom}) — Clicca su una miniatura per ispezionare il punto esatto"
            if n_anom > 0
            else "MOMENTI RILEVATI CON FRAME DUPLICATO O MANCANTE (Nessuna anomalia rilevata nel video)"
        )
        thumbs_title.setStyleSheet("font-size: 11px; font-weight: 800; color: #FFFFFF;")
        thumbs_hdr.addWidget(thumbs_title)
        thumbs_hdr.addStretch()
        thumbs_lay.addLayout(thumbs_hdr)

        self.thumb_scroll = QScrollArea()
        self.thumb_scroll.setObjectName("ThumbScroll")
        self.thumb_scroll.setWidgetResizable(True)
        self.thumb_scroll.setFixedHeight(114)
        self.thumb_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.thumb_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.thumb_scroll.setFocusPolicy(Qt.NoFocus)

        thumb_container = QWidget()
        thumb_container.setStyleSheet("background: transparent;")
        self.thumb_strip_lay = QHBoxLayout(thumb_container)
        self.thumb_strip_lay.setContentsMargins(2, 2, 2, 2)
        self.thumb_strip_lay.setSpacing(8)

        fps_src = self.meta.get("fps", 24.0) or 24.0
        if self.anomaly_frames:
            for pos_idx, f_idx in enumerate(self.anomaly_frames):
                t_sec = (f_idx + 1) / fps_src
                a_type = self.anomaly_types.get(f_idx, "missing_jump")
                card = AnomalyThumbnailCard(pos_idx, f_idx, t_sec, a_type)
                card.set_selected(pos_idx == self.current_anomaly_pos)
                card.clicked.connect(self._on_thumbnail_clicked)
                self.thumb_cards[pos_idx] = card
                self.thumb_strip_lay.addWidget(card)
        else:
            empty_lbl = QLabel("Nessun frame duplicato o mancante rilevato in questo video.")
            empty_lbl.setStyleSheet("color: rgba(255, 255, 255, 0.85); font-size: 12px; font-weight: 600;")
            self.thumb_strip_lay.addWidget(empty_lbl)

        self.thumb_strip_lay.addStretch()
        self.thumb_scroll.setWidget(thumb_container)
        thumbs_lay.addWidget(self.thumb_scroll)
        root.addWidget(thumbs_card)

        # Timeline 'Elettrocardiogramma' del Movimento
        timeline_card = QFrame()
        timeline_card.setObjectName("PreviewCard")
        tl_lay = QVBoxLayout(timeline_card)
        tl_lay.setContentsMargins(12, 8, 12, 10)
        tl_lay.setSpacing(5)

        tl_hdr = QHBoxLayout()
        tl_title = QLabel(
            "GRAFICO DEL MOVIMENTO FRAME-BY-FRAME (Clicca su una tacca rossa per saltare all'istante dello scatto)"
        )
        tl_title.setStyleSheet("font-size: 11px; font-weight: 800; color: #FFFFFF;")
        self.status_progress_lbl = QLabel("")
        self.status_progress_lbl.setStyleSheet("font-size: 11px; font-weight: 700; color: #E2EBFF;")
        tl_hdr.addWidget(tl_title)
        tl_hdr.addStretch()
        tl_hdr.addWidget(self.status_progress_lbl)
        tl_lay.addLayout(tl_hdr)

        self.timeline = MotionTimelineWidget()
        self.timeline.frame_selected.connect(self._jump_to_step)
        tl_lay.addWidget(self.timeline)

        self.counts_lbl = QLabel(self._video_counts_text())
        self.counts_lbl.setStyleSheet("font-size: 11px; font-weight: 700; color: #E2EBFF;")
        self.counts_lbl.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        self.counts_lbl.setToolTip(
            "Conteggi reali del video esportato.\n"
            "Le due corsie restano allineate nel tempo: dove un fotogramma non esiste\n"
            "(mancante nel sorgente o rimosso nell'output) compare uno slot vuoto \u2715."
        )
        tl_lay.addWidget(self.counts_lbl)
        root.addWidget(timeline_card)

        # Barra Controlli: Play/Pausa, Velocità Microscopio, Zoom Sincronizzato, Heat-Map Movimento
        ctrl_card = QFrame()
        ctrl_card.setObjectName("PreviewCard")
        ctrl_lay = QHBoxLayout(ctrl_card)
        ctrl_lay.setContentsMargins(12, 10, 12, 10)
        ctrl_lay.setSpacing(8)

        self.play_btn = QPushButton("\u23f8 Pausa")
        self.play_btn.setObjectName("PrimaryCtrlBtn")
        self.play_btn.setCursor(Qt.PointingHandCursor)
        self.play_btn.setFocusPolicy(Qt.NoFocus)
        self.play_btn.clicked.connect(self.toggle_play)
        ctrl_lay.addWidget(self.play_btn)

        ctrl_lay.addSpacing(10)
        spd_lbl = QLabel("Velocit\u00e0:")
        spd_lbl.setStyleSheet("font-size: 12px; font-weight: 800; color: #FFFFFF;")
        ctrl_lay.addWidget(spd_lbl)

        self.speed_group = QButtonGroup(self)
        for idx_s, (spd_val, spd_text) in enumerate([
            (0.25, "0.25x Microscopio"),
            (0.50, "0.5x Lento"),
            (1.00, "1x Reale"),
        ]):
            s_btn = QPushButton(spd_text)
            s_btn.setCheckable(True)
            s_btn.setObjectName("CtrlBtn")
            s_btn.setCursor(Qt.PointingHandCursor)
            s_btn.setFocusPolicy(Qt.NoFocus)
            s_btn.setProperty("speed_val", spd_val)
            if idx_s == 0:
                s_btn.setChecked(True)
            self.speed_group.addButton(s_btn, idx_s)
            ctrl_lay.addWidget(s_btn)
        self.speed_group.buttonClicked.connect(self._on_speed_changed)

        ctrl_lay.addSpacing(12)
        zoom_lbl = QLabel("Zoom:")
        zoom_lbl.setStyleSheet("font-size: 12px; font-weight: 800; color: #FFFFFF;")
        ctrl_lay.addWidget(zoom_lbl)

        self.zoom_out_btn = QPushButton("−")
        self.zoom_out_btn.setObjectName("CtrlBtn")
        self.zoom_out_btn.setCursor(Qt.PointingHandCursor)
        self.zoom_out_btn.setFocusPolicy(Qt.NoFocus)
        self.zoom_out_btn.setToolTip("Riduci zoom su entrambi i frame")
        self.zoom_out_btn.clicked.connect(lambda: self.orig_img_lbl.step_zoom(1.0 / 1.35, emit_signal=True))
        ctrl_lay.addWidget(self.zoom_out_btn)

        self.zoom_reset_btn = QPushButton("100%")
        self.zoom_reset_btn.setObjectName("CtrlBtn")
        self.zoom_reset_btn.setCursor(Qt.PointingHandCursor)
        self.zoom_reset_btn.setFocusPolicy(Qt.NoFocus)
        self.zoom_reset_btn.setToolTip("Ripristina zoom al 100% (1.0x) su entrambi i frame")
        self.zoom_reset_btn.clicked.connect(lambda: self.orig_img_lbl.reset_zoom(emit_signal=True))
        ctrl_lay.addWidget(self.zoom_reset_btn)

        self.zoom_in_btn = QPushButton("+")
        self.zoom_in_btn.setObjectName("CtrlBtn")
        self.zoom_in_btn.setCursor(Qt.PointingHandCursor)
        self.zoom_in_btn.setFocusPolicy(Qt.NoFocus)
        self.zoom_in_btn.setToolTip("Ingrandisci su entrambi i frame (oppure usa la rotellina del mouse)")
        self.zoom_in_btn.clicked.connect(lambda: self.orig_img_lbl.step_zoom(1.35, emit_signal=True))
        ctrl_lay.addWidget(self.zoom_in_btn)

        ctrl_lay.addStretch()

        self.diff_btn = QPushButton("\U0001f525 Heat-Map Movimento")
        self.diff_btn.setCheckable(True)
        self.diff_btn.setObjectName("CtrlBtn")
        self.diff_btn.setCursor(Qt.PointingHandCursor)
        self.diff_btn.setFocusPolicy(Qt.NoFocus)
        self.diff_btn.setToolTip(
            "Evidenzia con mappa termica (Blu \u2192 Ciano \u2192 Giallo \u2192 Rosso) lo spostamento reale rispetto al frame precedente:\n"
            "\u2022 Su un frame duplicato/congelato il movimento crolla a ZERO (immagine grigia ferma + banner congelamento)\n"
            "\u2022 Su un frame corretto i contorni in movimento restano sempre accesi e continui"
        )
        self.diff_btn.toggled.connect(self._on_diff_toggled)
        ctrl_lay.addWidget(self.diff_btn)

        root.addWidget(ctrl_card)

    def _on_orig_view_changed(self, zoom: float, center_u: float, center_v: float):
        self.fixed_img_lbl.set_view_state(zoom, center_u, center_v, emit_signal=False)
        self.zoom_reset_btn.setText(f"{int(round(zoom * 100))}%")

    def _on_fixed_view_changed(self, zoom: float, center_u: float, center_v: float):
        self.orig_img_lbl.set_view_state(zoom, center_u, center_v, emit_signal=False)
        self.zoom_reset_btn.setText(f"{int(round(zoom * 100))}%")

    def _start_thumbnails_worker(self):
        if not self.anomaly_frames:
            return
        self.thumb_worker = AnomalyThumbnailsWorker(
            self.meta["path"],
            self.meta.get("fps", 24.0) or 24.0,
            self.anomaly_frames,
        )
        self.thumb_worker.thumb_ready.connect(self._on_thumb_ready)
        self.thumb_worker.start()

    def _on_thumb_ready(self, pos_idx: int, _frame_idx: int, qimg: QImage):
        card = self.thumb_cards.get(pos_idx)
        if card:
            card.set_thumbnail(qimg)

    def _on_thumbnail_clicked(self, pos_idx: int):
        if pos_idx < 0 or pos_idx >= len(self.anomaly_frames):
            return
        self.current_anomaly_pos = pos_idx
        target_frame = self.anomaly_frames[pos_idx]
        for idx_c, card in self.thumb_cards.items():
            card.set_selected(idx_c == pos_idx)

        # Se la thumbnail selezionata è già caricata come unico errore corrente, salta al suo fotogramma
        if self.preview_data and self.preview_data.get("focus_anomaly_frame") == target_frame:
            s_frame = self.preview_data["start_frame"]
            anom_step = target_frame - s_frame + 1
            self._jump_to_step(anom_step)
            return

        self._focus_anomaly_frame = target_frame
        self._start_generation_for_current_segment()

    def _start_generation_for_current_segment(self):
        self.timer.stop()
        if self.worker and self.worker.isRunning():
            self.worker.wait()

        s_frame, num_f, focus_f, focus_t = self._compute_segment_for_anomaly(self.current_anomaly_pos)
        self._last_orig_state = ""
        self._last_fixed_state = ""
        self.orig_img_lbl.setText("Generazione anteprima Optical Flow in corso...")
        self.fixed_img_lbl.setText("Generazione anteprima Optical Flow in corso...")
        self.play_btn.setEnabled(False)

        self.worker = PreviewGenerationWorker(
            self.item,
            start_frame=s_frame,
            num_frames=num_f,
            focus_anomaly_frame=focus_f,
            focus_anomaly_type=focus_t,
        )
        self.worker.progress.connect(self._on_worker_progress)
        self.worker.finished_preview.connect(self._on_worker_finished)
        self.worker.error.connect(self._on_worker_error)
        self.worker.start()

    def _on_worker_progress(self, pct: int, msg: str):
        self.status_progress_lbl.setText(f"{msg} ({pct}%)")

    def _on_worker_error(self, err: str):
        self.status_progress_lbl.setText(f"Errore: {err}")
        self.orig_img_lbl.setText(f"Errore generazione preview:\n{err}")

    def _on_worker_finished(self, data: dict):
        self.preview_data = data
        focus_f = data.get("focus_anomaly_frame")
        if focus_f is not None:
            self.status_progress_lbl.setText(
                f"Errore #{self.current_anomaly_pos + 1}/{len(self.anomaly_frames)} (frame #{focus_f})  \u2022  "
                f"Finestra: #{data['start_frame']} \u2192 #{data['start_frame'] + data['orig_real_count'] - 1}"
            )
        else:
            self.status_progress_lbl.setText(
                f"Segmento: frame #{data['start_frame']} \u2192 #{data['start_frame'] + data['orig_real_count'] - 1}"
            )
        self.total_steps = max(len(data["orig_frames"]), len(data["fixed_frames"]), 1)

        if self._focus_anomaly_frame is not None:
            target_step = self._focus_anomaly_frame - data["start_frame"] + 1
            # Avvia la riproduzione pochi frame prima dell'errore così si osserva subito il passaggio
            rel_step = max(0, min(target_step - 3, self.total_steps - 1))
            self.current_step = rel_step
            self._focus_anomaly_frame = None
        else:
            self.current_step = 0

        self.timeline.set_data(
            data["orig_deltas"],
            data["orig_status"],
            data["fixed_deltas"],
            data["fixed_status"],
            data.get("orig_frame_numbers"),
        )
        self.counts_lbl.setText(self._video_counts_text())
        self.play_btn.setEnabled(True)
        self.play_btn.setText("\u23f8 Pausa")
        self._render_current_step()
        self._restart_timer()

    def _restart_timer(self):
        if not self.preview_data:
            return
        fps = self.preview_data.get("fps", 24.0) or 24.0
        interval_ms = max(16, int(round(1000.0 / (fps * self.speed_factor))))
        self.timer.start(interval_ms)

    def toggle_play(self):
        if self.timer.isActive():
            self.timer.stop()
            self.play_btn.setText("\u25b6 Riproduci")
        else:
            self.play_btn.setText("\u23f8 Pausa")
            self._restart_timer()

    def step_manual(self, delta: int):
        if not self.preview_data:
            return
        if self.timer.isActive():
            self.timer.stop()
            self.play_btn.setText("\u25b6 Riproduci")
        self.current_step = (self.current_step + delta) % self.total_steps
        self._render_current_step()

    def _jump_to_step(self, step: int):
        if not self.preview_data:
            return
        if self.timer.isActive():
            self.timer.stop()
            self.play_btn.setText("\u25b6 Riproduci")
        self.current_step = max(0, min(step, self.total_steps - 1))
        self._render_current_step()

    def _on_speed_changed(self, btn):
        self.speed_factor = float(btn.property("speed_val") or 0.25)
        if self.timer.isActive():
            self._restart_timer()

    def _on_diff_toggled(self, checked: bool):
        self.diff_mode = checked
        self._render_current_step()

    def _advance_frame(self):
        if not self.preview_data:
            return
        self.current_step = (self.current_step + 1) % self.total_steps
        self._render_current_step()

    @staticmethod
    def _make_diff_visual(cur_frame: np.ndarray, prev_frame: np.ndarray, status: str, delta_val: float) -> np.ndarray:
        """
        Genera una Heat-Map diagnostica chiara e immediata sovrapposta al video:
        - Sfondo: immagine del video desaturata e schiarita (così si riconosce sempre la scena).
        - Zone in movimento: colorate con mappa termica (Blu -> Ciano -> Giallo -> Rosso) proporzionale allo spostamento.
        - Se il frame è DUPLICATO / CONGELATO (movimento ~ 0), l'intera Heat-Map si spegne (grigio neutro)
          e compare un indicatore visivo in sovrimpressione '0% MOVIMENTO - FRAME FERMO'.
        """
        gray = cv2.cvtColor(cur_frame, cv2.COLOR_RGB2GRAY)
        base_val = np.clip(gray.astype(np.float32) * 0.45 + 135.0, 0, 255).astype(np.uint8)
        base_rgb = np.stack([base_val, base_val, base_val], axis=-1)

        diff_u8 = cv2.absdiff(cur_frame, prev_frame)
        diff_max = np.max(diff_u8, axis=2)
        diff_blur = cv2.GaussianBlur(diff_max, (5, 5), 0)

        norm_motion = np.clip((diff_blur.astype(np.float32) - 3.0) * 7.5, 0, 255).astype(np.uint8)
        heat_bgr = cv2.applyColorMap(norm_motion, cv2.COLORMAP_TURBO)
        heat_rgb = cv2.cvtColor(heat_bgr, cv2.COLOR_BGR2RGB)

        alpha = np.clip((diff_blur.astype(np.float32) - 3.0) / 26.0, 0.0, 0.88)[..., None]
        out = (base_rgb.astype(np.float32) * (1.0 - alpha) + heat_rgb.astype(np.float32) * alpha).astype(np.uint8)

        h, w, _ = out.shape
        bar_h = 8
        cv2.rectangle(out, (0, h - bar_h), (w, h), (226, 232, 240), -1)
        fill_ratio = min(1.0, delta_val / 12.0)
        fill_w = int(round(w * fill_ratio))

        if status == "duplicate" or delta_val < 0.18:
            cv2.rectangle(out, (2, 2), (w - 3, h - 3), (220, 38, 38), 3)
            overlay = out.copy()
            cv2.rectangle(overlay, (12, 12), (min(w - 12, 340), 44), (220, 38, 38), -1)
            out = cv2.addWeighted(overlay, 0.88, out, 0.12, 0)
            cv2.putText(
                out,
                "0% MOVIMENTO - FRAME FERMO",
                (22, 33),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.55,
                (255, 255, 255),
                2,
                cv2.LINE_AA,
            )
        elif status == "missing_jump":
            cv2.rectangle(out, (0, h - bar_h), (fill_w, h), (234, 88, 12), -1)
        elif status in ("reconstructed_missing", "reconstructed_dup"):
            cv2.rectangle(out, (0, h - bar_h), (fill_w, h), (22, 163, 74), -1)
        else:
            cv2.rectangle(out, (0, h - bar_h), (fill_w, h), (0, 71, 255), -1)

        return out

    def _update_panel_indicators(self, st_o: str, st_f: str):
        """
        Aggiorna testo e colori dei badge/bordi SOLO quando lo stato cambia e mantenendo
        spessori di bordo (2px pannello, 1px badge) e dimensioni (195x22) rigorosamente identici
        in ogni stato, eliminando qualsiasi micro-scatto o ricalcolo della UI.
        """
        if st_o != self._last_orig_state:
            self._last_orig_state = st_o
            if st_o == "duplicate":
                self.orig_panel.setStyleSheet(
                    "QFrame#VideoPanel { background-color: #FFFFFF; border: 2px solid #EF4444; border-radius: 10px; }"
                )
                self.orig_badge.setText("\u25cf Frame Duplicato (Blocco)")
                self.orig_badge.setStyleSheet(
                    "background-color: #FEF2F2; color: #DC2626; border: 1px solid #FECACA; "
                    "font-size: 10px; font-weight: 800; border-radius: 4px;"
                )
            elif st_o == "missing_gap":
                self.orig_panel.setStyleSheet(
                    "QFrame#VideoPanel { background-color: #FFFFFF; border: 2px solid #F59E0B; border-radius: 10px; }"
                )
                self.orig_badge.setText("\u2715 Frame Mancante")
                self.orig_badge.setStyleSheet(
                    "background-color: #FFFBEB; color: #B45309; border: 1px solid #FDE68A; "
                    "font-size: 10px; font-weight: 700; border-radius: 4px;"
                )
            elif st_o == "missing_jump":
                self.orig_panel.setStyleSheet(
                    "QFrame#VideoPanel { background-color: #FFFFFF; border: 2px solid #F59E0B; border-radius: 10px; }"
                )
                self.orig_badge.setText("\u25cf Salto Frame Mancante")
                self.orig_badge.setStyleSheet(
                    "background-color: #FFFBEB; color: #B45309; border: 1px solid #FDE68A; "
                    "font-size: 10px; font-weight: 700; border-radius: 4px;"
                )
            else:
                self.orig_panel.setStyleSheet(
                    "QFrame#VideoPanel { background-color: #FFFFFF; border: 2px solid #E2E8F0; border-radius: 10px; }"
                )
                self.orig_badge.setText("Sorgente Originale")
                self.orig_badge.setStyleSheet(
                    "background-color: #F8FAFC; color: #64748B; border: 1px solid #E2E8F0; "
                    "font-size: 10px; font-weight: 600; border-radius: 4px;"
                )

        if st_f != self._last_fixed_state:
            self._last_fixed_state = st_f
            if st_f in ("reconstructed_missing", "reconstructed_dup"):
                self.fixed_panel.setStyleSheet(
                    "QFrame#VideoPanel { background-color: #FFFFFF; border: 2px solid #3B82F6; border-radius: 10px; }"
                )
                self.fixed_badge.setText("\u2726 Ricostruito Optical Flow")
                self.fixed_badge.setStyleSheet(
                    "background-color: #EFF6FF; color: #1D4ED8; border: 1px solid #BFDBFE; "
                    "font-size: 10px; font-weight: 700; border-radius: 4px;"
                )
            elif st_f == "removed_dup":
                self.fixed_panel.setStyleSheet(
                    "QFrame#VideoPanel { background-color: #FFFFFF; border: 2px solid #EF4444; border-radius: 10px; }"
                )
                self.fixed_badge.setText("\u2715 Duplicato Rimosso")
                self.fixed_badge.setStyleSheet(
                    "background-color: #FEF2F2; color: #DC2626; border: 1px solid #FECACA; "
                    "font-size: 10px; font-weight: 800; border-radius: 4px;"
                )
            else:
                self.fixed_panel.setStyleSheet(
                    "QFrame#VideoPanel { background-color: #FFFFFF; border: 2px solid #E2E8F0; border-radius: 10px; }"
                )
                self.fixed_badge.setText("Flusso Continuo")
                self.fixed_badge.setStyleSheet(
                    "background-color: #F8FAFC; color: #64748B; border: 1px solid #E2E8F0; "
                    "font-size: 10px; font-weight: 600; border-radius: 4px;"
                )

    @staticmethod
    def _nearest_real_frame(frames: list, idx: int, backward_only: bool = False):
        """Restituisce il fotogramma reale (non None) più vicino prima di `idx` (o dopo, se non esiste)."""
        for j in range(idx - 1, -1, -1):
            if frames[j] is not None:
                return frames[j]
        if backward_only:
            return None
        for j in range(idx + 1, len(frames)):
            if frames[j] is not None:
                return frames[j]
        return None

    @staticmethod
    def _make_gap_visual(base_frame: np.ndarray | None, title: str, subtitle: str, color_rgb: tuple) -> np.ndarray:
        """
        Visualizza uno slot VUOTO: il fotogramma in questo istante non esiste (mancante nel sorgente
        oppure rimosso dall'output). Mostra l'ultimo frame reale molto scurito con un tratteggio
        diagonale e un'etichetta, così non può essere scambiato per un fotogramma vero.
        """
        if base_frame is None:
            out = np.full((360, 640, 3), 24, dtype=np.uint8)
        else:
            gray = cv2.cvtColor(base_frame, cv2.COLOR_RGB2GRAY)
            dark = (gray.astype(np.float32) * 0.28).astype(np.uint8)
            out = np.stack([dark, dark, dark], axis=-1)
        h, w, _ = out.shape
        stripe_col = tuple(int(c * 0.45) for c in color_rgb)
        step = max(18, w // 28)
        for x0 in range(-h, w, step):
            cv2.line(out, (x0, h), (x0 + h, 0), stripe_col, 2, cv2.LINE_AA)
        cv2.rectangle(out, (2, 2), (w - 3, h - 3), color_rgb, 3)

        font = cv2.FONT_HERSHEY_SIMPLEX
        scale_t = max(0.6, min(1.3, w / 900.0))
        scale_s = scale_t * 0.55
        (tw, th), _ = cv2.getTextSize(title, font, scale_t, 2)
        (sw, sh), _ = cv2.getTextSize(subtitle, font, scale_s, 1)
        box_w = max(tw, sw) + 40
        box_h = th + sh + 44
        bx0 = max(0, (w - box_w) // 2)
        by0 = max(0, (h - box_h) // 2)
        cv2.rectangle(out, (bx0, by0), (bx0 + box_w, by0 + box_h), color_rgb, -1)
        cv2.putText(out, title, ((w - tw) // 2, by0 + 16 + th), font, scale_t, (255, 255, 255), 2, cv2.LINE_AA)
        cv2.putText(out, subtitle, ((w - sw) // 2, by0 + 28 + th + sh), font, scale_s, (255, 255, 255), 1, cv2.LINE_AA)
        return out

    def _video_counts_text(self) -> str:
        plan = build_correction_plan(self.analysis)
        text = (
            f"Video intero: ORIGINALE {plan['source_count']} \u2192 CORRETTO {plan['output_count']} frame  "
            f"(+{len(plan['missing_set'])} ricostruiti, "
            f"\u2212{len(plan['drop_dup_set'])} rimossi, "
            f"{len(plan['replace_dup_set'])} sostituiti)"
        )
        if self.preview_data:
            text += (
                f"   \u2022   Finestra: {self.preview_data['orig_real_count']} \u2192 "
                f"{self.preview_data['fixed_real_count']} frame"
            )
        return text

    def _render_current_step(self):
        if not self.preview_data:
            return
        orig_frames = self.preview_data["orig_frames"]
        fixed_frames = self.preview_data["fixed_frames"]
        orig_status = self.preview_data["orig_status"]
        fixed_status = self.preview_data["fixed_status"]
        orig_deltas = self.preview_data["orig_deltas"]
        fixed_deltas = self.preview_data["fixed_deltas"]
        orig_frame_nums = self.preview_data.get("orig_frame_numbers") or []
        fixed_frame_nums = self.preview_data.get("fixed_frame_numbers") or []
        src_total = self.preview_data.get("video_source_count", 0)
        out_total = self.preview_data.get("video_output_count", 0)

        idx_o = min(self.current_step, len(orig_frames) - 1)
        idx_f = min(self.current_step, len(fixed_frames) - 1)

        st_o = orig_status[idx_o]
        st_f = fixed_status[idx_f]
        fr_o = orig_frames[idx_o]
        fr_f = fixed_frames[idx_f]

        if fr_o is None:
            disp_o = self._make_gap_visual(
                self._nearest_real_frame(orig_frames, idx_o),
                "FRAME MANCANTE",
                "questo fotogramma non esiste nel video originale",
                (234, 88, 12),
            )
        elif self.diff_mode:
            prev_o = self._nearest_real_frame(orig_frames, idx_o, backward_only=True)
            disp_o = self._make_diff_visual(fr_o, prev_o if prev_o is not None else fr_o, st_o, orig_deltas[idx_o] or 0.0)
        else:
            disp_o = fr_o

        if fr_f is None:
            disp_f = self._make_gap_visual(
                self._nearest_real_frame(fixed_frames, idx_f),
                "FRAME RIMOSSO",
                "duplicato eliminato dal video esportato",
                (220, 38, 38),
            )
        elif self.diff_mode:
            prev_f = self._nearest_real_frame(fixed_frames, idx_f, backward_only=True)
            disp_f = self._make_diff_visual(fr_f, prev_f if prev_f is not None else fr_f, st_f, fixed_deltas[idx_f] or 0.0)
        else:
            disp_f = fr_f

        self.orig_img_lbl.set_frame_array(disp_o)
        self.fixed_img_lbl.set_frame_array(disp_f)

        self._update_panel_indicators(st_o, st_f)

        if fr_o is None:
            self.orig_meta_lbl.setText(
                "Frame assente nel sorgente (mancante)"
            )
        else:
            f_num_orig = orig_frame_nums[idx_o] if idx_o < len(orig_frame_nums) else idx_o
            self.orig_meta_lbl.setText(
                f"Frame sorgente #{f_num_orig} (su {src_total})  \u2022  Delta movimento: {orig_deltas[idx_o]:.2f}"
            )
        if fr_f is None:
            self.fixed_meta_lbl.setText(
                "Frame rimosso dall'output (duplicato)"
            )
        else:
            f_num_out = fixed_frame_nums[idx_f] if idx_f < len(fixed_frame_nums) else idx_f
            self.fixed_meta_lbl.setText(
                f"Frame output #{f_num_out} (su {out_total})  \u2022  Delta movimento: {fixed_deltas[idx_f]:.2f}"
            )
        self.timeline.set_step(self.current_step)

    def closeEvent(self, event):
        self.timer.stop()
        if self.thumb_worker and self.thumb_worker.isRunning():
            self.thumb_worker.stop()
            self.thumb_worker.wait(800)
        if self.worker and self.worker.isRunning():
            self.worker.wait(1500)
        super().closeEvent(event)


class VideoJudderFixerWindow(QMainWindow):
    """Finestra principale MONOVIDEOFIX v1.4 (Sfondo Blu Cobalto MONOGRID con superfici Bianche e testo Nero)."""

    def __init__(self, initial_videos: list[str] | None = None):
        super().__init__()
        self.setWindowTitle("MONOVIDEOFIX v1.4")
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
        self.preview_btn = QPushButton("\U0001f441 Anteprima & Verifica Correzione")
        self.preview_btn.setObjectName("PreviewBtn")
        self.preview_btn.setCursor(Qt.PointingHandCursor)
        self.preview_btn.setEnabled(False)
        self.preview_btn.setToolTip(
            "Apre la finestra di confronto Prima / Dopo con rallentatore 0.25x, semaforo frame duplicati/mancanti e grafico del movimento"
        )
        self.preview_btn.clicked.connect(self.open_preview_dialog)
        header_row.addWidget(title_lbl)
        header_row.addStretch()
        header_row.addWidget(self.meta_badge)
        header_row.addSpacing(10)
        header_row.addWidget(self.preview_btn)
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
            QPushButton#PreviewBtn {
                background-color: #FFFFFF;
                border: none;
                border-radius: 6px;
                padding: 5px 12px;
                color: #0047FF;
                font-size: 12px;
                font-weight: 900;
            }
            QPushButton#PreviewBtn:hover {
                background-color: #E2EBFF;
                color: #0035C9;
            }
            QPushButton#PreviewBtn:disabled {
                background-color: rgba(255, 255, 255, 0.25);
                color: rgba(255, 255, 255, 0.65);
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

    def open_preview_dialog(self):
        row = self.get_selected_row()
        if row < 0 or row >= len(self.batch_items):
            return
        item = self.batch_items[row]
        if not item.meta or not item.analysis:
            return
        dlg = PreviewComparisonDialog(self, item)
        dlg.exec_()

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
        self.preview_btn.setEnabled(False)
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
            self.preview_btn.setEnabled(True)
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
            self.preview_btn.setEnabled(False)
            self.meta_badge.setText(f"{Path(item.path).name} — Errore")
            self.missing_box_val.setText("Err")
            self.dup_box_val.setText("Err")
            self.cuts_box_val.setText("Err")
            self.log_box.setPlainText(f"Errore durante l'analisi:\n{item.error_msg}")
        else:
            self.preview_btn.setEnabled(False)
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

    def _on_export_item_finished(self, idx: int, out_path: str, written: int, fixed: int, removed: int):
        if idx < len(self.batch_items):
            item = self.batch_items[idx]
            item.exported = True
            item.status = "✓ Completato"
            item.report_text += (
                f"\n\n[✓] ESPORTAZIONE COMPLETATA!\n"
                f"    File salvato     : {out_path}\n"
                f"    Frame originali  : {item.analysis['num_frames']}\n"
                f"    Frame ricostruiti: {fixed} (Optical Flow: mancanti inseriti + duplicati sostituiti)\n"
                f"    Frame rimossi    : {removed} (duplicati puri)\n"
                f"    Frame esportati  : {written} @ {item.target_fps} FPS ({item.target_codec.upper()})"
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
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("monogrid.monovideofix.v1.4")
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

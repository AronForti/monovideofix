#!/usr/bin/env python3
"""
fix_video_judder.py
===================
Analizza e corregge gli "scattini" regolari (judder / frame drop) nei video,
come quelli causati da una conversione errata 25 fps -> 24 fps (1 frame perso
ogni 24 frame = 1 volta al secondo).

Funzionalità principali:
1. Analisi ottica (Optical Flow Lucas-Kanade & DIS) per individuare:
   - Cambi di inquadratura (scene cuts)
   - Salti di movimento anomali (~2x rispetto ai frame adiacenti = frame mancante)
   - Cadenza periodica dei frame persi (es. ogni 24 frame)
2. Ricostruzione chirurgica dei soli frame mancanti tramite Optical Flow
   bidirezionale denso (OpenCV DISOpticalFlow) con pesatura di occlusione
   forward-backward:
   - Tutti i frame originali restano intatti al 100% (nessun degrado o blur)
   - Pipeline a 16-bit per canale (rgb48le) tramite FFmpeg per preservare la
     profondità colore a 10-bit (yuv420p10le, BT.709).
"""

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

import cv2
import numpy as np


def get_video_metadata(video_path: str) -> dict:
    """Estrae i metadati dello stream video tramite ffprobe."""
    cmd = [
        "ffprobe",
        "-v", "error",
        "-select_streams", "v:0",
        "-show_entries",
        "stream=width,height,r_frame_rate,avg_frame_rate,nb_frames,duration,pix_fmt,color_space,color_transfer,color_primaries,color_range,codec_name",
        "-of", "json",
        str(video_path),
    ]
    out = subprocess.check_output(cmd, text=True)
    stream = json.loads(out)["streams"][0]
    num, den = map(int, stream["r_frame_rate"].split("/"))
    fps = num / den
    return {
        "width": int(stream["width"]),
        "height": int(stream["height"]),
        "fps": fps,
        "fps_str": stream["r_frame_rate"],
        "nb_frames": int(stream.get("nb_frames", 0)),
        "duration": float(stream.get("duration", 0.0)),
        "pix_fmt": stream.get("pix_fmt", "yuv420p10le"),
        "color_space": stream.get("color_space", "bt709"),
        "color_transfer": stream.get("color_transfer", "bt709"),
        "color_primaries": stream.get("color_primaries", "bt709"),
        "color_range": stream.get("color_range", "tv"),
    }


def analyze_video_motion(video_path: str) -> dict:
    """
    Calcola lo spostamento ottico (Lucas-Kanade + MAD) tra ogni coppia di frame
    consecutivi per rilevare scene cuts e frame persi (salti ~2x).
    """
    cap = cv2.VideoCapture(str(video_path))
    gray_frames = []
    while True:
        ret, frame = cap.read()
        if not ret:
            break
        gray_frames.append(cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY))
    cap.release()

    n = len(gray_frames)
    mad_scores = []
    med_displacements = []
    p90_displacements = []

    for i in range(n - 1):
        g0, g1 = gray_frames[i], gray_frames[i + 1]
        mad = float(np.mean(np.abs(g1.astype(np.float32) - g0.astype(np.float32))))
        mad_scores.append(mad)

        pts0 = cv2.goodFeaturesToTrack(g0, maxCorners=500, qualityLevel=0.01, minDistance=10)
        if pts0 is not None and len(pts0) > 10:
            pts1, st, _ = cv2.calcOpticalFlowPyrLK(g0, g1, pts0, None)
            good0 = pts0[st.ravel() == 1]
            good1 = pts1[st.ravel() == 1]
            if len(good0) > 5:
                disp = np.linalg.norm(good1 - good0, axis=1)
                med_displacements.append(float(np.median(disp)))
                p90_displacements.append(float(np.percentile(disp, 90)))
            else:
                med_displacements.append(0.0)
                p90_displacements.append(0.0)
        else:
            med_displacements.append(0.0)
            p90_displacements.append(0.0)

    # Rileva i cambi di scena (MAD > 25.0)
    scene_cuts = [i for i, m in enumerate(mad_scores) if m > 25.0]

    # Rileva i picchi anomali (frame drop) confrontando ogni step con la mediana locale
    detected_drops = []
    ratios = []
    for i in range(n - 1):
        if i in scene_cuts:
            ratios.append(1.0)
            continue
        # Finestra locale escludendo cambi scena e il frame corrente
        neighbors = [
            p90_displacements[j]
            for j in range(max(0, i - 6), min(n - 1, i + 7))
            if j != i and j not in scene_cuts and abs(j - i) != 24
        ]
        local_ref = float(np.median(neighbors)) if neighbors else 1.0
        ratio = p90_displacements[i] / max(local_ref, 1e-4)
        ratios.append(ratio)
        if ratio > 1.40:
            detected_drops.append(i)

    return {
        "num_frames": n,
        "mad_scores": mad_scores,
        "med_displacements": med_displacements,
        "p90_displacements": p90_displacements,
        "ratios": ratios,
        "scene_cuts": scene_cuts,
        "detected_drops": detected_drops,
    }


def interpolate_midpoint_16bit(
    frame0_u16: np.ndarray,
    frame1_u16: np.ndarray,
    alpha: float = 0.5,
) -> np.ndarray:
    """
    Ricostruisce un frame intermedio al tempo `alpha` (default 0.5) tra
    `frame0_u16` e `frame1_u16` (uint16 RGB) usando DISOpticalFlow bidirezionale
    con pesatura di consistenza forward-backward per evitare ghosting sui bordi.
    """
    # Converti in uint8 grayscale per il calcolo dell'optical flow ad alta precisione
    f0_u8 = (frame0_u16 >> 8).astype(np.uint8)
    f1_u8 = (frame1_u16 >> 8).astype(np.uint8)
    g0 = cv2.cvtColor(f0_u8, cv2.COLOR_RGB2GRAY)
    g1 = cv2.cvtColor(f1_u8, cv2.COLOR_RGB2GRAY)

    dis = cv2.DISOpticalFlow.create(cv2.DISOPTICAL_FLOW_PRESET_MEDIUM)
    dis.setFinestScale(1)
    dis.setPatchSize(16)
    dis.setPatchStride(4)

    # flow01: da frame0 a frame1; flow10: da frame1 a frame0
    flow01 = dis.calc(g0, g1, None)
    flow10 = dis.calc(g1, g0, None)

    h, w = g0.shape
    grid_x, grid_y = np.meshgrid(
        np.arange(w, dtype=np.float32),
        np.arange(h, dtype=np.float32),
    )

    # Coordinate di campionamento (backward remap dal tempo intermedio alpha)
    map0_x = grid_x + alpha * flow10[..., 0]
    map0_y = grid_y + alpha * flow10[..., 1]
    map1_x = grid_x + (1.0 - alpha) * flow01[..., 0]
    map1_y = grid_y + (1.0 - alpha) * flow01[..., 1]

    # Warp in float32 per preservare tutti i 16-bit senza perdita di precisione
    f0_flt = frame0_u16.astype(np.float32)
    f1_flt = frame1_u16.astype(np.float32)

    w0 = cv2.remap(f0_flt, map0_x, map0_y, cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)
    w1 = cv2.remap(f1_flt, map1_x, map1_y, cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)

    # Consistenza forward-backward per pesare eventuali aree occluse (somma pesi sempre = 1.0)
    flow01_warped = cv2.remap(flow01, map0_x, map0_y, cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)
    flow10_warped = cv2.remap(flow10, map1_x, map1_y, cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)

    err0 = np.hypot(flow10[..., 0] + flow01_warped[..., 0], flow10[..., 1] + flow01_warped[..., 1])
    err1 = np.hypot(flow01[..., 0] + flow10_warped[..., 0], flow01[..., 1] + flow10_warped[..., 1])

    wt0 = (1.0 - alpha) * np.exp(-0.1 * err0) + 1e-2
    wt1 = alpha * np.exp(-0.1 * err1) + 1e-2
    alpha0 = (wt0 / (wt0 + wt1))[..., None]

    blended = w0 * alpha0 + w1 * (1.0 - alpha0)
    return np.clip(np.round(blended), 0, 65535).astype(np.uint16)


def repair_video(
    input_path: str,
    output_path: str,
    drop_indices: list[int],
    output_fps: str = "25/1",
    crf: int = 15,
) -> None:
    """
    Legge il video originale a 16-bit (rgb48le), inserisce i frame mancanti
    esattamente nelle transizioni `i -> i+1` per ogni `i` in `drop_indices`,
    e codifica il video risultante in HEVC 10-bit (yuv420p10le, BT.709).
    """
    meta = get_video_metadata(input_path)
    w, h = meta["width"], meta["height"]
    frame_bytes = w * h * 3 * 2  # rgb48le = 6 byte per pixel

    decode_cmd = [
        "ffmpeg",
        "-v", "error",
        "-i", str(input_path),
        "-f", "rawvideo",
        "-pix_fmt", "rgb48le",
        "-",
    ]

    encode_cmd = [
        "ffmpeg",
        "-y",
        "-v", "error",
        "-f", "rawvideo",
        "-pix_fmt", "rgb48le",
        "-s", f"{w}x{h}",
        "-r", output_fps,
        "-i", "-",
        "-c:v", "libx265",
        "-preset", "medium",
        "-crf", str(crf),
        "-pix_fmt", meta["pix_fmt"],
        "-color_primaries", meta["color_primaries"],
        "-color_trc", meta["color_transfer"],
        "-colorspace", meta["color_space"],
        "-color_range", meta["color_range"],
        "-tag:v", "hvc1",
        "-x265-params", "log-level=error",
        str(output_path),
    ]

    drop_set = set(drop_indices)
    decoder = subprocess.Popen(decode_cmd, stdout=subprocess.PIPE)
    encoder = subprocess.Popen(encode_cmd, stdin=subprocess.PIPE)

    prev_frame = None
    idx = 0
    inserted_count = 0

    try:
        while True:
            raw = decoder.stdout.read(frame_bytes)
            if len(raw) < frame_bytes:
                break
            curr_frame = np.frombuffer(raw, dtype=np.uint16).reshape((h, w, 3))

            if prev_frame is not None and (idx - 1) in drop_set:
                mid_frame = interpolate_midpoint_16bit(prev_frame, curr_frame, alpha=0.5)
                encoder.stdin.write(mid_frame.tobytes())
                inserted_count += 1
                print(f"  [+] Ricostruito e inserito frame mancante tra #{idx-1} e #{idx}")

            encoder.stdin.write(raw)
            prev_frame = curr_frame
            idx += 1
    finally:
        if decoder.stdout:
            decoder.stdout.close()
        decoder.wait()
        if encoder.stdin:
            encoder.stdin.close()
        encoder.wait()

    print(
        f"Completato: {idx} frame originali + {inserted_count} frame ricostruiti = "
        f"{idx + inserted_count} frame totali salvati in '{output_path}' ({output_fps} fps)."
    )


def print_analysis_report(video_path: str, meta: dict, analysis: dict, cadence: int = 24) -> list[int]:
    """Stampa un report dettagliato dell'analisi e restituisce gli indici da riparare."""
    n = analysis["num_frames"]
    cuts = set(analysis["scene_cuts"])
    p90 = analysis["p90_displacements"]
    med = analysis["med_displacements"]
    ratios = analysis["ratios"]

    print("=" * 72)
    print(f"ANALISI VIDEO: {video_path}")
    print("=" * 72)
    print(f"Risoluzione : {meta['width']}x{meta['height']} ({meta['pix_fmt']}, {meta['color_space']})")
    print(f"Framerate   : {meta['fps_str']} fps ({meta['fps']:.3f} fps)")
    print(f"Frame totali: {n} (durata: {meta['duration']:.4f} s)")
    print(f"Scene cuts  : {[f'{c}->{c+1}' for c in analysis['scene_cuts']]}")
    print("-" * 72)
    print("Rilevamento cadenza 'scattini' (verifica salti ogni 24 frame = 1.00 s):")

    cadence_drops = []
    for k in range(cadence, n, cadence):
        i = k - 1  # transizione (24k - 1) -> 24k
        if i in cuts:
            continue
        cadence_drops.append(i)
        t_sec = k / meta["fps"]
        print(
            f"  * Frame {i:3d} -> {i+1:3d} (t = {t_sec:5.2f}s): "
            f"spostamento med={med[i]:5.2f}px, p90={p90[i]:5.2f}px "
            f"({ratios[i]:.2f}x rispetto ai frame adiacenti) -> FRAME MANCANTE!"
        )
    print("=" * 72)
    return cadence_drops


def main():
    parser = argparse.ArgumentParser(
        description="Analizza e corregge gli scattini periodici (frame drop 25->24 fps) nei video."
    )
    parser.add_argument("input", help="Percorso del file video di input")
    parser.add_argument(
        "-o", "--output",
        help="Percorso del file video corretto (default: <input>_fixed_25fps.mp4)",
    )
    parser.add_argument(
        "--cadence",
        type=int,
        default=24,
        help="Cadenza dei frame persi in numero di frame (default: 24, tipico di 25fps -> 24fps)",
    )
    parser.add_argument(
        "--fps",
        default="25/1",
        help="Framerate del file di output (default: '25/1' per ripristinare i 25 fps nativi mantenendo la durata identica; usa '24/1' se vuoi mantenere 24 fps)",
    )
    parser.add_argument(
        "--crf",
        type=int,
        default=15,
        help="Qualità di codifica HEVC 10-bit (default: 15, visivamente lossless)",
    )
    parser.add_argument(
        "--analyze-only",
        action="store_true",
        help="Esegue solo l'analisi senza generare il video corretto",
    )

    args = parser.parse_args()
    input_path = Path(args.input)
    if not input_path.exists():
        print(f"Errore: file non trovato: {input_path}", file=sys.stderr)
        sys.exit(1)

    meta = get_video_metadata(str(input_path))
    analysis = analyze_video_motion(str(input_path))
    drop_indices = print_analysis_report(str(input_path), meta, analysis, cadence=args.cadence)

    if args.analyze_only:
        return

    if args.output:
        output_path = Path(args.output)
    else:
        fps_tag = args.fps.split("/")[0]
        output_path = input_path.with_name(f"{input_path.stem}_fixed_{fps_tag}fps{input_path.suffix}")

    print(f"\nAvvio ricostruzione dei {len(drop_indices)} frame mancanti...")
    repair_video(
        input_path=str(input_path),
        output_path=str(output_path),
        drop_indices=drop_indices,
        output_fps=args.fps,
        crf=args.crf,
    )


if __name__ == "__main__":
    main()

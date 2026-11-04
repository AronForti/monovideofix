<p align="center">
  <img src="assets/icon.svg" width="72" height="72" alt="MonoVideoFix Logo">
</p>

<h1 align="center">MONOVIDEOFIX v0.3</h1>
<p align="center">
  <strong>Seedance FPS & Judder Repair Tool</strong><br>
  Strumento desktop con interfaccia grafica (PyQt5) e CLI per l'analisi automatica e la correzione chirurgica di scattini (<em>judder</em>), frame persi e frame duplicati nei video (es. conversioni errate 25 → 24 fps).
</p>

---

## ✨ Caratteristiche Principali

- **Rilevamento Automatico con Optical Flow (Lucas-Kanade + MAD)**:
  - Individua **frame mancanti** (salti di movimento anomali ~2x rispetto ai fotogrammi adiacenti) e rileva automaticamente eventuali **cadenze periodiche** (es. 1 frame perso ogni 24 fotogrammi).
  - Individua **frame duplicati / congelati** (freeze frames).
  - Riconosce ed esclude automaticamente i **cambi di inquadratura (scene cuts)** per evitare artefatti tra scene diverse.
- **Ricostruzione Chirurgica a 16-bit (Zero Degrado sui Frame Originali)**:
  - **100% Bit-Exact sui frame sani**: tutti i fotogrammi originali vengono copiati intatti senza alcuna alterazione o sfocatura.
  - **Interpolazione DIS Optical Flow bidirezionale**: ricostruisce *esclusivamente* i fotogrammi mancanti o sostituisce i fotogrammi duplicati tramite flusso ottico denso bidirezionale (`cv2.DISOpticalFlow`) con pesatura di occlusione forward-backward.
  - **Pipeline a 16-bit per canale (`rgb48le`)**: preserva fedelmente la profondità colore a 10-bit (`yuv420p10le`, spazio colore BT.709) e mantiene eventuale audio presente nel file sorgente.
- **Elaborazione Singola e Batch (Drag & Drop)**:
  - Trascina uno o più file video (`.mp4`, `.mov`, `.mkv`, `.avi`, `.webm`, `.m4v`, `.mpg`, `.mpeg`) o intere cartelle nella coda di lavoro.
  - Configura **FPS di uscita** (`24`, `25`, `30`, `60` fps) e **Codec** (`H.264`, `H.265 / HEVC 10-bit`, `Apple ProRes 422 HQ`) per singolo video o per l'intero batch.
- **Avvio Rapido Intelligente (Smart FFmpeg Lookup & Cache)**:
  - Il singolo file `.exe` include `ffmpeg.exe` e `ffprobe.exe` ma **non li estrae ad ogni apertura**: controlla prima se sono già presenti nel sistema (`PATH`) o nella cache locale (`%LOCALAPPDATA%\MonoVideoFix\bin`), aprendo la finestra quasi istantaneamente.

---

## 🚀 Download ed Esecuzione Rapida (Windows Standalone `.exe`)

Non è necessario installare Python, OpenCV o FFmpeg:

1. Scarica **`MONOVIDEOFIX_v0.3.exe`** dalla sezione [**Releases**](https://github.com/AronForti/monovideofix/releases/latest).
2. Avvia **`MONOVIDEOFIX_v0.3.exe`** con un doppio clic (il singolo file `.exe` include già Python, PyQt5, OpenCV, `ffmpeg.exe` e `ffprobe.exe`).
3. Trascina i video nella finestra: l'analisi partirà automaticamente.
4. Scegli gli **FPS** e il **Codec** desiderati (di default vengono impostati quelli corrispondenti al file sorgente) e clicca su **ESPORTA**.
5. I video corretti verranno salvati nella stessa cartella degli originali con il suffisso `_[fps]fps` (es. `video_24fps.mp4` o `video_24fps.mov`).

---

## 🛠️ Utilizzo da Codice Sorgente (Python)

### Requisiti
- **Python 3.10+**
- **FFmpeg & FFprobe** installati nel `PATH` di sistema (oppure copiati nella cartella del progetto)

### Installazione dipendenze
```powershell
pip install -r requirements.txt
```

### Avvio Interfaccia Grafica (GUI)
```powershell
python video_judder_gui.py
```
*(Su Windows puoi anche fare doppio clic su `Avvia_GUI.bat`)*

### Utilizzo da Riga di Comando (CLI)
È disponibile anche lo script CLI `fix_video_judder.py`:
```powershell
# Esegui analisi e correzione automatica
python fix_video_judder.py input_video.mp4 -o output_fixed.mp4

# Solo analisi (senza esportazione)
python fix_video_judder.py input_video.mp4 --analyze-only

# Mantieni il conteggio frame originale e specifica il bitrate
python fix_video_judder.py input_video.mp4 --keep-frame-count --bitrate 20M
```

---

## 📦 Compilazione dell'Eseguibile Singolo (`.exe`)

Per rigenerare l'eseguibile standalone a singolo file con PyInstaller (includendo automaticamente `ffmpeg.exe` e `ffprobe.exe` con estrazione on-demand):

```powershell
pyinstaller --noconfirm --clean MONOVIDEOFIX_v0.3.spec
```
Il file compilato verrà generato in `dist\MONOVIDEOFIX_v0.3.exe`.

---

## 📁 Struttura del Progetto

| File | Descrizione |
| :--- | :--- |
| `video_judder_gui.py` | Applicazione desktop GUI (PyQt5) con supporto Drag & Drop e coda Batch |
| `fix_video_judder.py` | Script CLI per analisi ottica e riparazione judder da terminale |
| `MONOVIDEOFIX_v0.3.spec` | Configurazione PyInstaller per la creazione del singolo file `.exe` con FFmpeg on-demand |
| `requirements.txt` | Elenco delle dipendenze Python del progetto |
| `LEGGIMI.txt` | Guida rapida per l'utente finale |
| `monovideofix.ico` / `assets/icon.svg` | Icone dell'applicazione |

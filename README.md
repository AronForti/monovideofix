<p align="center">
  <img src="assets/icon.svg" width="72" height="72" alt="MonoVideoFix Logo">
</p>

<h1 align="center">MONOVIDEOFIX v1.1</h1>
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
- **Anteprima & Verifica Diagnostica Prima / Dopo (`👁 Anteprima & Verifica Correzione`)**:
  - **Confronto Side-by-Side sincronizzato** tra il video *Originale* e il video *Corretto (Optical Flow)*.
  - **Galleria Thumbnail Cliccabili dei Momenti Anomali (1 Thumbnail = 1 Singolo Errore)**: mostra le anteprime visive di tutti gli istanti in cui si verifica un **frame duplicato** o **frame mancante**; selezionando una thumbnail viene caricato uno spezzone mirato centrato esclusivamente su quel singolo errore (1 sola tacca nella timeline sottostante).
  - **Rallentatore "Microscopio" (`0.25x` / `0.5x` / `1x`) + Navigazione frame da tastiera (`←` / `→`)**: rende palese a occhio nudo ogni frame ripetuto o salto risolto.
  - **Indicatori di stato a zero jitter**: badge e bordi a dimensione/spessore fisso che segnalano i frame duplicati/mancanti senza provocare alcun movimento o scattino nel layout della UI.
  - **Timeline "Elettrocardiogramma" (Grafico del Movimento Frame-by-Frame)**: mostra la tacca rossa del blocco/salto sull'originale e il frame ricostruito sul video corretto; cliccando sulla tacca si salta all'istante esatto dello scatto.
  - **Modalità `🔥 Heat-Map Movimento`**: sovrappone una mappa termica (`Turbo`: Blu → Ciano → Giallo → Rosso) ai soggetti in movimento sulla scena desaturata; quando un frame è congelato/ripetuto, la mappa termica si spegne completamente con allerta `0% MOVIMENTO - FRAME FERMO`.
- **Ricostruzione Chirurgica a 16-bit (Zero Degrado sui Frame Originali)**:
  - **100% Bit-Exact sui frame sani**: tutti i fotogrammi originali vengono copiati intatti senza alcuna alterazione o sfocatura.
  - **Interpolazione DIS Optical Flow bidirezionale**: ricostruisce *esclusivamente* i fotogrammi mancanti o sostituisce i fotogrammi duplicati tramite flusso ottico denso bidirezionale (`cv2.DISOpticalFlow`) con pesatura di occlusione forward-backward.
  - **Pipeline a 16-bit per canale (`rgb48le`)**: preserva fedelmente la profondità colore a 10-bit (`yuv420p10le`, spazio colore BT.709) e mantiene eventuale audio presente nel file sorgente.
- **Elaborazione Singola e Batch (Drag & Drop)**:
  - Trascina uno o più file video (`.mp4`, `.mov`, `.mkv`, `.avi`, `.webm`, `.m4v`, `.mpg`, `.mpeg`) o intere cartelle nella coda di lavoro.
  - Configura **FPS di uscita** (`24`, `25`, `30`, `60` fps) e **Codec** (`H.264`, `H.265 / HEVC 10-bit`, `Apple ProRes 422 HQ`), applicati automaticamente a tutti i video caricati nella coda.
- **Avvio Rapido Intelligente (Smart FFmpeg Lookup & Cache)**:
  - Il singolo file `.exe` include `ffmpeg.exe` e `ffprobe.exe` ma **non li estrae ad ogni apertura**: controlla prima se sono già presenti nel sistema (`PATH`) o nella cache locale (`%LOCALAPPDATA%\MonoVideoFix\bin`), aprendo la finestra quasi istantaneamente.

---

## 🚀 Download ed Esecuzione Rapida (Windows Standalone `.exe`)

Non è necessario installare Python, OpenCV o FFmpeg:

1. Scarica **`MONOVIDEOFIX_v1.1.exe`** dalla sezione [**Releases**](https://github.com/AronForti/monovideofix/releases/latest).
2. Avvia **`MONOVIDEOFIX_v1.1.exe`** con un doppio clic (il singolo file `.exe` include già Python, PyQt5, OpenCV, `ffmpeg.exe` e `ffprobe.exe`).
3. Trascina i video nella finestra: l'analisi partirà automaticamente.
4. Clicca su **`👁 Anteprima & Verifica Correzione`** per ispezionare tramite le thumbnail dei momenti anomali, al rallentatore (`0.25x`) o con la **Heat-Map Movimento** la risoluzione dei frame mancanti/duplicati.
5. Scegli gli **FPS** e il **Codec** desiderati (applicati automaticamente a tutta la coda) e clicca su **ESPORTA**.
6. I video corretti verranno salvati nella stessa cartella degli originali con il suffisso `_[fps]fps` (es. `video_24fps.mp4` o `video_24fps.mov`).

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
pyinstaller --noconfirm --clean MONOVIDEOFIX_v1.1.spec
```
Il file compilato verrà generato in `dist\MONOVIDEOFIX_v1.1.exe`.

---

## 📁 Struttura del Progetto

| File | Descrizione |
| :--- | :--- |
| `video_judder_gui.py` | Applicazione desktop GUI (PyQt5) con supporto Drag & Drop, Preview Prima/Dopo e coda Batch |
| `fix_video_judder.py` | Script CLI per analisi ottica e riparazione judder da terminale |
| `MONOVIDEOFIX_v1.1.spec` | Configurazione PyInstaller per la creazione del singolo file `.exe` con FFmpeg on-demand |
| `requirements.txt` | Elenco delle dipendenze Python del progetto |
| `LEGGIMI.txt` | Guida rapida per l'utente finale |
| `monovideofix.ico` / `assets/icon.svg` | Icone dell'applicazione |

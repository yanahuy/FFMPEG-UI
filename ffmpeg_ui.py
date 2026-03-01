#!/usr/bin/env python3
import json
import os
import platform
import random
import re
import shlex
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from PySide6.QtCore import QObject, Qt, QThread, Signal
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QProgressBar,
    QPlainTextEdit,
    QSpinBox,
    QDoubleSpinBox,
    QTabWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)


APP_NAME = "FFMPEG-UI"


@dataclass
class AppSettings:
    workdir: str = str(Path.cwd())
    ffmpeg_path: str = "ffmpeg"
    ytdlp_path: str = "yt-dlp"
    bgs: List[str] = field(default_factory=lambda: ["b1.mp4", "b2.mp4", "b3.mp4"])
    banner: str = "banner.mov"
    speed: float = 1.1
    video_y: int = 100
    banner_x: int = 0
    banner_y: int = 260
    banner_scale: float = 0.60
    out_no_suffix: str = "_nob"
    out_ban_suffix: str = "_b"
    output_folder: str = "processed_temp"
    fps: int = 30
    crf: int = 23
    preset: str = "veryfast"
    volume: float = 2.0
    audio_bitrate: str = "192k"
    sleep_min: int = 4
    sleep_max: int = 7


class SettingsService:
    def __init__(self):
        self.settings_path = self._resolve_path()

    @staticmethod
    def _resolve_path() -> Path:
        home = Path.home()
        if platform.system() == "Windows":
            base = Path(os.getenv("LOCALAPPDATA", str(home / "AppData/Local")))
        elif platform.system() == "Darwin":
            base = home / "Library/Application Support"
        else:
            base = Path(os.getenv("XDG_CONFIG_HOME", str(home / ".config")))
        path = base / APP_NAME
        path.mkdir(parents=True, exist_ok=True)
        return path / "settings.json"

    def load(self) -> AppSettings:
        if not self.settings_path.exists():
            return AppSettings()
        try:
            data = json.loads(self.settings_path.read_text(encoding="utf-8"))
            s = AppSettings()
            for k, v in data.items():
                if hasattr(s, k):
                    setattr(s, k, v)
            return s
        except Exception:
            return AppSettings()

    def save(self, settings: AppSettings) -> None:
        self.settings_path.write_text(json.dumps(asdict(settings), ensure_ascii=False, indent=2), encoding="utf-8")


class LogBus(QObject):
    line = Signal(str)

    def emit(self, message: str):
        stamp = datetime.now().strftime("%H:%M:%S")
        self.line.emit(f"[{stamp}] {message}")


class CancellationToken:
    def __init__(self):
        self._event = threading.Event()

    def cancel(self):
        self._event.set()

    def is_cancelled(self) -> bool:
        return self._event.is_set()


class ProcessRunner:
    def __init__(self, log: LogBus):
        self.log = log

    def run(self, args: List[str], cwd: Optional[str] = None, token: Optional[CancellationToken] = None, quiet: bool = False) -> Tuple[int, str, str]:
        cmd = " ".join(shlex.quote(x) for x in args)
        self.log.emit(f"$ {cmd}")
        process = subprocess.Popen(
            args,
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        out_lines: List[str] = []
        err_lines: List[str] = []

        def reader(stream, sink, prefix=""):
            for ln in iter(stream.readline, ""):
                sink.append(ln)
                if not quiet:
                    self.log.emit(f"{prefix}{ln.rstrip()}")

        t1 = threading.Thread(target=reader, args=(process.stdout, out_lines, ""), daemon=True)
        t2 = threading.Thread(target=reader, args=(process.stderr, err_lines, "ERR: "), daemon=True)
        t1.start(); t2.start()

        while process.poll() is None:
            if token and token.is_cancelled():
                process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                break
            time.sleep(0.1)

        t1.join(timeout=1)
        t2.join(timeout=1)
        return process.returncode or 0, "".join(out_lines), "".join(err_lines)


class FileScanner:
    @staticmethod
    def scan_videos(workdir: Path, banner: str, bgs: List[str]) -> List[Path]:
        ex = {banner.lower(), *[Path(x).name.lower() for x in bgs]}
        files: List[Path] = []
        for p in workdir.iterdir():
            if not p.is_file():
                continue
            if p.suffix.lower() not in {".mp4", ".mov"}:
                continue
            if p.name.lower() in ex:
                continue
            files.append(p)
        return files


class DownloaderService:
    def __init__(self, settings: AppSettings, runner: ProcessRunner, log: LogBus):
        self.settings = settings
        self.runner = runner
        self.log = log

    @staticmethod
    def _cookies_order() -> List[str]:
        return ["safari", "chrome"] if platform.system() == "Darwin" else ["edge", "chrome"]

    @staticmethod
    def _extract_vid(url: str) -> Optional[str]:
        m = re.search(r"/video/([^/?#]+)", url)
        return m.group(1) if m else None

    def unwrap_unique(self, urls: List[str], token: CancellationToken) -> Tuple[List[str], Dict[str, str]]:
        urls = [u.strip() for u in urls if u.strip()]
        random.shuffle(urls)
        seen = set()
        result = []
        status: Dict[str, str] = {}
        for url in urls:
            if token.is_cancelled():
                break
            if "/video/" in url:
                vid = self._extract_vid(url)
                if not vid:
                    status[url] = "error"
                    self.log.emit(f"❌ Не удалось выделить id: {url}")
                    continue
                if vid in seen:
                    status[url] = "duplicate"
                    self.log.emit(f"⚠️ Дубликат ID {vid} — пропуск")
                    continue
                seen.add(vid)
                result.append(url)
                status[url] = "ok"
                continue
            parsed = None
            for browser in self._cookies_order():
                if token.is_cancelled():
                    break
                code, out, _ = self.runner.run([
                    self.settings.ytdlp_path,
                    "--cookies-from-browser",
                    browser,
                    "--print",
                    "%(uploader)s|%(id)s",
                    url,
                ], cwd=self.settings.workdir)
                if code == 0 and "|" in out:
                    parsed = out.strip().splitlines()[-1]
                    break
            if not parsed:
                status[url] = "error"
                self.log.emit(f"❌ Не удалось разобрать: {url}")
                continue
            uploader, vid = parsed.split("|", 1)
            uploader, vid = uploader.strip(), vid.strip()
            if not uploader or not vid:
                status[url] = "error"
                continue
            if vid in seen:
                status[url] = "duplicate"
                self.log.emit(f"⚠️ Дубликат ID {vid} — пропуск")
                continue
            seen.add(vid)
            long_url = f"https://www.tiktok.com/@{uploader}/video/{vid}"
            result.append(long_url)
            status[long_url] = "ok"
            self.log.emit(f"✔ Развернута: {long_url}")
        self.log.emit(f"🔢 Уникальных ссылок: {len(result)}")
        return result, status

    def download_all(self, urls: List[str], token: CancellationToken, status_cb: Callable[[str, str], None]):
        for idx, url in enumerate(urls, 1):
            if token.is_cancelled():
                return
            if idx > 1:
                d = random.randint(self.settings.sleep_min, self.settings.sleep_max)
                self.log.emit(f"⏳ Пауза {d} сек")
                for _ in range(d * 10):
                    if token.is_cancelled():
                        return
                    time.sleep(0.1)
            vid = self._extract_vid(url) or ""
            ok = False
            for attempt in range(1, 6):
                if token.is_cancelled():
                    return
                self.log.emit(f"Попытка {attempt}: {url}")
                for browser in self._cookies_order():
                    if token.is_cancelled():
                        return
                    self.runner.run([
                        self.settings.ytdlp_path,
                        "--cookies-from-browser", browser,
                        "-f", "bv*[vcodec^=avc]+ba/b",
                        "--merge-output-format", "mp4",
                        "--no-part", "--no-continue", "--no-overwrites",
                        "--retries", "2", "--fragment-retries", "2",
                        "--restrict-filenames",
                        "-o", str(Path(self.settings.workdir) / "%(uploader)s_%(id)s.%(ext)s"),
                        url,
                    ], cwd=self.settings.workdir)
                    if self._has_downloaded(vid):
                        ok = True
                        break
                    self.log.emit("⚠️ ФАЙЛА НЕТ")
                    time.sleep(4)
                if ok:
                    break
            status_cb(url, "success" if ok else "skipped")

    def _has_downloaded(self, vid: str) -> bool:
        wd = Path(self.settings.workdir)
        return any(wd.glob(f"*_{vid}.mp4"))


class MontageService:
    def __init__(self, settings: AppSettings, runner: ProcessRunner, log: LogBus):
        self.settings = settings
        self.runner = runner
        self.log = log

    def run_montage(self, files: List[Path], num_no: int, token: CancellationToken, progress_cb: Callable[[int, int], None]):
        wd = Path(self.settings.workdir)
        total = len(files)
        if total == 0:
            raise RuntimeError("Нет файлов для монтажа")
        bgs = [wd / bg for bg in self.settings.bgs]
        missing = [str(p) for p in bgs if not p.exists()]
        if missing:
            raise RuntimeError("Отсутствуют фоны: " + ", ".join(missing))
        if total - num_no > 0:
            banner = wd / self.settings.banner
            if not banner.exists():
                raise RuntimeError(f"Отсутствует баннер: {banner}")

        out_dir = wd / self.settings.output_folder
        out_dir.mkdir(parents=True, exist_ok=True)
        random.shuffle(files)
        files_no = files[:num_no]
        files_b = files[num_no:]

        start = time.time()
        count = 0
        for f in files_no:
            if token.is_cancelled():
                break
            count += 1
            self._process_no(f, random.choice(bgs), out_dir)
            self._cleanup(wd)
            progress_cb(count, total)

        for f in files_b:
            if token.is_cancelled():
                break
            count += 1
            self._process_banner(f, random.choice(bgs), wd / self.settings.banner, out_dir)
            self._cleanup(wd)
            progress_cb(count, total)

        duration = int(time.time() - start)
        return duration

    def _process_no(self, f: Path, bg: Path, out_dir: Path):
        wd = Path(self.settings.workdir)
        base = f.stem
        out = out_dir / f"{base}{self.settings.out_no_suffix}.mp4"
        temp_v = wd / "temp_v.mp4"
        self.runner.run([
            self.settings.ffmpeg_path, "-y", "-i", str(f),
            "-filter_complex",
            f"[0:v]setpts=(PTS-STARTPTS)/{self.settings.speed},scale=810:1440:force_original_aspect_ratio=decrease,pad=ceil(iw/2)*2:ceil(ih/2)*2:(ow-iw)/2:(oh-ih)/2[v];[0:a]atempo={self.settings.speed},volume={self.settings.volume}[a]",
            "-map", "[v]", "-map", "[a]",
            "-c:v", "libx264", "-crf", str(self.settings.crf), "-preset", self.settings.preset,
            "-c:a", "aac", "-b:a", self.settings.audio_bitrate,
            str(temp_v)
        ], cwd=self.settings.workdir)
        self.runner.run([
            self.settings.ffmpeg_path, "-y", "-stream_loop", "-1", "-i", str(bg), "-i", str(temp_v),
            "-filter_complex", f"[0:v]scale=1080:1920[bg];[bg][1:v]overlay=(W-w)/2:(H-h)/2+{self.settings.video_y}[out]",
            "-map", "[out]", "-map", "1:a",
            "-c:v", "libx264", "-crf", str(self.settings.crf), "-preset", self.settings.preset,
            "-r", str(self.settings.fps), "-shortest", str(out)
        ], cwd=self.settings.workdir)

    def _process_banner(self, f: Path, bg: Path, banner: Path, out_dir: Path):
        wd = Path(self.settings.workdir)
        base = f.stem
        out = out_dir / f"{base}{self.settings.out_ban_suffix}.mp4"
        temp_v = wd / "temp_v.mp4"
        temp_bg = wd / "temp_bg.mp4"
        self.runner.run([
            self.settings.ffmpeg_path, "-y", "-i", str(f),
            "-filter_complex",
            f"[0:v]setpts=(PTS-STARTPTS)/{self.settings.speed},scale=810:1440:force_original_aspect_ratio=decrease,pad=ceil(iw/2)*2:ceil(ih/2)*2:(ow-iw)/2:(oh-ih)/2[v];[0:a]atempo={self.settings.speed},volume={self.settings.volume}[a]",
            "-map", "[v]", "-map", "[a]",
            "-c:v", "libx264", "-crf", str(self.settings.crf), "-preset", self.settings.preset,
            "-c:a", "aac", "-b:a", self.settings.audio_bitrate, str(temp_v)
        ], cwd=self.settings.workdir)
        self.runner.run([
            self.settings.ffmpeg_path, "-y", "-stream_loop", "-1", "-i", str(bg), "-i", str(temp_v),
            "-filter_complex", f"[0:v]scale=1080:1920[bg];[bg][1:v]overlay=(W-w)/2:(H-h)/2+{self.settings.video_y}[out]",
            "-map", "[out]", "-map", "1:a",
            "-c:v", "libx264", "-crf", str(self.settings.crf), "-preset", self.settings.preset,
            "-r", str(self.settings.fps), "-shortest", str(temp_bg)
        ], cwd=self.settings.workdir)
        self.runner.run([
            self.settings.ffmpeg_path, "-y", "-i", str(temp_bg), "-i", str(banner),
            "-filter_complex", f"[1:v]scale=iw*{self.settings.banner_scale}:ih*{self.settings.banner_scale}[b];[0:v][b]overlay=(W-w)/2+{self.settings.banner_x}:{self.settings.banner_y}:shortest=1[out]",
            "-map", "[out]", "-map", "0:a",
            "-c:v", "libx264", "-crf", str(self.settings.crf), "-preset", self.settings.preset,
            "-r", str(self.settings.fps), "-c:a", "copy", str(out)
        ], cwd=self.settings.workdir)

    def _cleanup(self, wd: Path):
        for tmp in [wd / "temp_v.mp4", wd / "temp_bg.mp4"]:
            try:
                if tmp.exists():
                    tmp.unlink()
            except Exception:
                pass


class Worker(QThread):
    error = Signal(str)
    done = Signal(object)

    def __init__(self, fn: Callable, *args, **kwargs):
        super().__init__()
        self.fn = fn
        self.args = args
        self.kwargs = kwargs

    def run(self):
        try:
            out = self.fn(*self.args, **self.kwargs)
            self.done.emit(out)
        except Exception as e:
            self.error.emit(str(e))


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("TikTok Downloader + Montage")
        self.resize(1200, 800)

        self.settings_service = SettingsService()
        self.settings = self.settings_service.load()
        self.log_bus = LogBus()
        self.runner = ProcessRunner(self.log_bus)
        self.downloader = DownloaderService(self.settings, self.runner, self.log_bus)
        self.montage = MontageService(self.settings, self.runner, self.log_bus)
        self.cancel_token = CancellationToken()
        self.montage_files: List[Path] = []
        self.unwrap_urls: List[str] = []

        self._build_ui()
        self.log_bus.line.connect(self.log_view.appendPlainText)
        self.detect_tools()

    def _build_ui(self):
        tabs = QTabWidget()
        tabs.addTab(self._build_downloader_tab(), "Downloader")
        tabs.addTab(self._build_montage_tab(), "Montage")
        tabs.addTab(self._build_settings_tab(), "Settings")
        tabs.addTab(self._build_logs_tab(), "Logs")
        self.setCentralWidget(tabs)

    def _build_downloader_tab(self):
        w = QWidget(); l = QVBoxLayout(w)
        wd_row = QHBoxLayout()
        self.workdir_edit = QLineEdit(self.settings.workdir)
        wd_btn = QPushButton("Выбрать рабочую папку")
        wd_btn.clicked.connect(self.pick_workdir)
        wd_row.addWidget(self.workdir_edit); wd_row.addWidget(wd_btn)
        l.addLayout(wd_row)

        self.urls_edit = QTextEdit()
        self.urls_edit.setPlaceholderText("Вставьте ссылки, каждая с новой строки")
        l.addWidget(self.urls_edit)

        row = QHBoxLayout()
        b1 = QPushButton("Развернуть + убрать дубликаты")
        b2 = QPushButton("Скачать")
        b3 = QPushButton("Очистить")
        b1.clicked.connect(self.unwrap_clicked)
        b2.clicked.connect(self.download_clicked)
        b3.clicked.connect(lambda: self.urls_edit.clear())
        row.addWidget(b1); row.addWidget(b2); row.addWidget(b3)
        l.addLayout(row)

        row2 = QHBoxLayout()
        self.skip_download = QCheckBox("Видео уже скачаны (пропустить скачивание)")
        self.cookies_combo = QComboBox()
        self.cookies_combo.addItems(["auto", "safari", "chrome"] if platform.system()=="Darwin" else ["auto", "edge", "chrome"])
        row2.addWidget(self.skip_download)
        row2.addWidget(QLabel("Cookies:")); row2.addWidget(self.cookies_combo)
        l.addLayout(row2)

        self.unique_list = QListWidget(); l.addWidget(self.unique_list)
        self.dl_status = QListWidget(); l.addWidget(self.dl_status)
        return w

    def _build_montage_tab(self):
        w = QWidget(); l = QVBoxLayout(w)
        row = QHBoxLayout()
        scan = QPushButton("Сканировать рабочую папку и собрать видео")
        scan.clicked.connect(self.scan_files)
        rnd = QPushButton("Рандомизировать порядок")
        rnd.clicked.connect(self.randomize_files)
        row.addWidget(scan); row.addWidget(rnd)
        l.addLayout(row)

        self.files_list = QListWidget(); l.addWidget(self.files_list)
        self.total_lbl = QLabel("TOTAL: 0")
        l.addWidget(self.total_lbl)

        row2 = QHBoxLayout()
        self.num_no_spin = QSpinBox(); self.num_no_spin.setMinimum(0)
        self.start_btn = QPushButton("Старт монтажа")
        self.cancel_btn = QPushButton("Отмена")
        self.open_out_btn = QPushButton("Открыть output папку")
        self.open_out_btn.clicked.connect(self.open_output_folder)
        self.start_btn.clicked.connect(self.start_montage)
        self.cancel_btn.clicked.connect(self.cancel_current)
        row2.addWidget(QLabel("Сколько видео без баннера")); row2.addWidget(self.num_no_spin)
        row2.addWidget(self.start_btn); row2.addWidget(self.cancel_btn); row2.addWidget(self.open_out_btn)
        l.addLayout(row2)

        self.progress = QProgressBar()
        self.progress_lbl = QLabel("0/0")
        l.addWidget(self.progress); l.addWidget(self.progress_lbl)
        return w

    def _build_settings_tab(self):
        w = QWidget(); l = QVBoxLayout(w)
        form = QFormLayout()
        self.ffmpeg_edit = QLineEdit(self.settings.ffmpeg_path)
        self.ytdlp_edit = QLineEdit(self.settings.ytdlp_path)
        self.banner_edit = QLineEdit(self.settings.banner)
        self.bgs_edit = QTextEdit("\n".join(self.settings.bgs))
        self.speed_spin = QDoubleSpinBox(); self.speed_spin.setValue(self.settings.speed); self.speed_spin.setRange(0.1, 4.0); self.speed_spin.setSingleStep(0.1)
        self.video_y_spin = QSpinBox(); self.video_y_spin.setValue(self.settings.video_y); self.video_y_spin.setRange(-2000, 2000)
        self.bx_spin = QSpinBox(); self.bx_spin.setValue(self.settings.banner_x); self.bx_spin.setRange(-2000, 2000)
        self.by_spin = QSpinBox(); self.by_spin.setValue(self.settings.banner_y); self.by_spin.setRange(-2000, 2000)
        self.bs_spin = QDoubleSpinBox(); self.bs_spin.setValue(self.settings.banner_scale); self.bs_spin.setRange(0.1, 2.0)
        self.out_no_edit = QLineEdit(self.settings.out_no_suffix)
        self.out_b_edit = QLineEdit(self.settings.out_ban_suffix)

        form.addRow("ffmpeg", self.ffmpeg_edit)
        form.addRow("yt-dlp", self.ytdlp_edit)
        form.addRow("Banner file", self.banner_edit)
        form.addRow("Background files (по 1 на строку)", self.bgs_edit)
        form.addRow("SPEED", self.speed_spin)
        form.addRow("VIDEO_Y", self.video_y_spin)
        form.addRow("BANNER_X", self.bx_spin)
        form.addRow("BANNER_Y", self.by_spin)
        form.addRow("BANNER_SCALE", self.bs_spin)
        form.addRow("Suffix no banner", self.out_no_edit)
        form.addRow("Suffix banner", self.out_b_edit)
        l.addLayout(form)

        row = QHBoxLayout()
        save = QPushButton("Сохранить"); reset = QPushButton("Сбросить по умолчанию")
        save.clicked.connect(self.save_settings)
        reset.clicked.connect(self.reset_settings)
        row.addWidget(save); row.addWidget(reset)
        l.addLayout(row)
        return w

    def _build_logs_tab(self):
        w = QWidget(); l = QVBoxLayout(w)
        self.log_view = QPlainTextEdit(); self.log_view.setReadOnly(True)
        l.addWidget(self.log_view)
        row = QHBoxLayout()
        cp = QPushButton("Copy"); sv = QPushButton("Save to file")
        cp.clicked.connect(lambda: QApplication.clipboard().setText(self.log_view.toPlainText()))
        sv.clicked.connect(self.save_logs)
        row.addWidget(cp); row.addWidget(sv)
        l.addLayout(row)
        return w

    def pick_workdir(self):
        d = QFileDialog.getExistingDirectory(self, "Выбор рабочей папки", self.workdir_edit.text())
        if d:
            self.workdir_edit.setText(d)
            self.settings.workdir = d

    def detect_tools(self):
        for tool, widget in [("ffmpeg", self.ffmpeg_edit), ("yt-dlp", self.ytdlp_edit)]:
            p = shutil.which(tool)
            if p:
                widget.setText(p)
                self.log_bus.emit(f"Найден {tool}: {p}")

    def apply_settings_from_ui(self):
        self.settings.workdir = self.workdir_edit.text().strip() or self.settings.workdir
        self.settings.ffmpeg_path = self.ffmpeg_edit.text().strip()
        self.settings.ytdlp_path = self.ytdlp_edit.text().strip()
        self.settings.banner = self.banner_edit.text().strip()
        self.settings.bgs = [x.strip() for x in self.bgs_edit.toPlainText().splitlines() if x.strip()]
        self.settings.speed = float(self.speed_spin.value())
        self.settings.video_y = int(self.video_y_spin.value())
        self.settings.banner_x = int(self.bx_spin.value())
        self.settings.banner_y = int(self.by_spin.value())
        self.settings.banner_scale = float(self.bs_spin.value())
        self.settings.out_no_suffix = self.out_no_edit.text().strip() or "_nob"
        self.settings.out_ban_suffix = self.out_b_edit.text().strip() or "_b"

    def save_settings(self):
        self.apply_settings_from_ui()
        self.settings_service.save(self.settings)
        QMessageBox.information(self, "OK", "Настройки сохранены")

    def reset_settings(self):
        self.settings = AppSettings()
        self.workdir_edit.setText(self.settings.workdir)
        self.ffmpeg_edit.setText(self.settings.ffmpeg_path)
        self.ytdlp_edit.setText(self.settings.ytdlp_path)
        self.banner_edit.setText(self.settings.banner)
        self.bgs_edit.setPlainText("\n".join(self.settings.bgs))
        self.speed_spin.setValue(self.settings.speed)
        self.video_y_spin.setValue(self.settings.video_y)
        self.bx_spin.setValue(self.settings.banner_x)
        self.by_spin.setValue(self.settings.banner_y)
        self.bs_spin.setValue(self.settings.banner_scale)
        self.out_no_edit.setText(self.settings.out_no_suffix)
        self.out_b_edit.setText(self.settings.out_ban_suffix)

    def unwrap_clicked(self):
        self.apply_settings_from_ui()
        self.cancel_token = CancellationToken()
        urls = self.urls_edit.toPlainText().splitlines()

        self.worker = Worker(self.downloader.unwrap_unique, urls, self.cancel_token)
        self.worker.done.connect(self._unwrap_done)
        self.worker.error.connect(self._show_error)
        self.worker.start()

    def _unwrap_done(self, payload):
        urls, _ = payload
        self.unwrap_urls = urls
        self.unique_list.clear()
        for u in urls:
            self.unique_list.addItem(u)

    def download_clicked(self):
        self.apply_settings_from_ui()
        if self.skip_download.isChecked():
            self.log_bus.emit("⚡ Пропуск скачивания")
            return
        urls = self.unwrap_urls or [self.urls_edit.toPlainText().strip()]
        urls = [u for u in urls if u]
        if not urls:
            self._show_error("Нет ссылок")
            return
        self.cancel_token = CancellationToken()
        self.dl_status.clear()

        def fn():
            self.downloader.download_all(urls, self.cancel_token, self._set_dl_status)

        self.worker = Worker(fn)
        self.worker.error.connect(self._show_error)
        self.worker.done.connect(lambda _: self.log_bus.emit("✅ Загрузка завершена"))
        self.worker.start()

    def _set_dl_status(self, url: str, st: str):
        self.dl_status.addItem(f"{st.upper()}: {url}")

    def scan_files(self):
        self.apply_settings_from_ui()
        wd = Path(self.settings.workdir)
        if not wd.exists():
            self._show_error("Рабочая папка не существует")
            return
        self.montage_files = FileScanner.scan_videos(wd, self.settings.banner, self.settings.bgs)
        self.files_list.clear()
        for f in self.montage_files:
            self.files_list.addItem(f.name)
        self.total_lbl.setText(f"TOTAL: {len(self.montage_files)}")
        self.num_no_spin.setMaximum(len(self.montage_files))

    def randomize_files(self):
        random.shuffle(self.montage_files)
        self.files_list.clear()
        for f in self.montage_files:
            self.files_list.addItem(f.name)

    def start_montage(self):
        self.apply_settings_from_ui()
        if not self.montage_files:
            self._show_error("Сначала просканируйте папку")
            return
        num_no = self.num_no_spin.value()
        self.cancel_token = CancellationToken()
        self.progress.setMaximum(len(self.montage_files)); self.progress.setValue(0)

        def fn():
            return self.montage.run_montage(self.montage_files[:], num_no, self.cancel_token, self._on_progress)

        self.worker = Worker(fn)
        self.worker.done.connect(self._montage_done)
        self.worker.error.connect(self._show_error)
        self.worker.start()

    def _on_progress(self, count: int, total: int):
        self.progress.setValue(count)
        self.progress_lbl.setText(f"{count}/{total}")

    def _montage_done(self, duration: int):
        m, s = divmod(duration, 60)
        self.log_bus.emit(f"✅ Время обработки: {m}м {s}с")

    def cancel_current(self):
        self.cancel_token.cancel()
        self.log_bus.emit("⛔ Отмена запрошена")

    def save_logs(self):
        path, _ = QFileDialog.getSaveFileName(self, "Save logs", str(Path(self.settings.workdir) / "app.log"), "Log (*.log *.txt)")
        if path:
            Path(path).write_text(self.log_view.toPlainText(), encoding="utf-8")

    def open_output_folder(self):
        out = Path(self.settings.workdir) / self.settings.output_folder
        out.mkdir(parents=True, exist_ok=True)
        if platform.system() == "Darwin":
            subprocess.Popen(["open", str(out)])
        elif platform.system() == "Windows":
            subprocess.Popen(["explorer", str(out)])
        else:
            subprocess.Popen(["xdg-open", str(out)])

    def _show_error(self, msg: str):
        self.log_bus.emit(f"❌ {msg}")
        QMessageBox.critical(self, "Ошибка", msg)


def main():
    app = QApplication(sys.argv)
    w = MainWindow()
    w.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())

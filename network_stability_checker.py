"""Network Stability Checker

A small Tkinter desktop app that measures repeated HTTPS response times.
It is a general endpoint check, not a direct measurement of PlayStation Portal
streaming traffic or gameplay latency.
"""

from __future__ import annotations

import csv
import queue
import statistics
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

APP_TITLE = "Network Stability Checker"
DEFAULT_URL = "https://www.playstation.com/ja-jp/"
DEFAULT_COUNT = 10
TIMEOUT_SECONDS = 8
USER_AGENT = "NetworkStabilityChecker/1.0"


@dataclass
class Measurement:
    attempt: int
    timestamp: str
    url: str
    status: str
    elapsed_ms: float | None
    error: str = ""


def measure_once(url: str, attempt: int, timeout: float = TIMEOUT_SECONDS) -> Measurement:
    """Measure the time until an HTTPS response is received."""
    timestamp = datetime.now().astimezone().isoformat(timespec="seconds")
    request = urllib.request.Request(
        url,
        headers={"User-Agent": USER_AGENT, "Cache-Control": "no-cache"},
        method="GET",
    )
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            # Read a small amount so the response is actually received.
            response.read(1024)
            elapsed = (time.perf_counter() - started) * 1000
            return Measurement(attempt, timestamp, url, f"HTTP {response.status}", elapsed)
    except urllib.error.HTTPError as exc:
        # An HTTP error still proves the server responded over the network.
        elapsed = (time.perf_counter() - started) * 1000
        return Measurement(attempt, timestamp, url, f"HTTP {exc.code}", elapsed,
                           f"HTTPエラー応答: {exc.code}")
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        elapsed = (time.perf_counter() - started) * 1000
        reason = getattr(exc, "reason", exc)
        return Measurement(attempt, timestamp, url, "失敗", elapsed, str(reason))


def summarize(measurements: list[Measurement]) -> dict[str, float | int | None]:
    """Calculate simple summary statistics for completed measurements."""
    successful = [m.elapsed_ms for m in measurements if m.status.startswith("HTTP") and m.elapsed_ms is not None]
    total = len(measurements)
    success_count = len(successful)
    response_times = [float(x) for x in successful]
    consecutive_diffs = [abs(response_times[i] - response_times[i - 1])
                         for i in range(1, len(response_times))]
    return {
        "total": total,
        "success": success_count,
        "failure": total - success_count,
        "success_rate": (success_count / total * 100) if total else 0.0,
        "average_ms": statistics.mean(response_times) if response_times else None,
        "min_ms": min(response_times) if response_times else None,
        "max_ms": max(response_times) if response_times else None,
        # Mean absolute difference between adjacent successful measurements.
        "jitter_ms": statistics.mean(consecutive_diffs) if consecutive_diffs else None,
    }


class NetworkStabilityApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title(APP_TITLE)
        self.root.geometry("860x650")
        self.root.minsize(720, 540)
        self.events: queue.Queue[tuple[str, object]] = queue.Queue()
        self.worker: threading.Thread | None = None
        self.stop_event = threading.Event()
        self.measurements: list[Measurement] = []
        self._build_ui()
        self.root.after(100, self._process_events)

    def _build_ui(self) -> None:
        main = ttk.Frame(self.root, padding=16)
        main.pack(fill="both", expand=True)

        ttk.Label(main, text="Network Stability Checker", font=("Segoe UI", 19, "bold")).pack(anchor="w")
        ttk.Label(
            main,
            text="WebサイトへのHTTPS応答を繰り返し測定し、応答時間と安定性の目安を確認します。",
        ).pack(anchor="w", pady=(2, 12))

        settings = ttk.LabelFrame(main, text="測定設定", padding=10)
        settings.pack(fill="x")
        ttk.Label(settings, text="測定先URL").grid(row=0, column=0, sticky="w", padx=(0, 8), pady=4)
        self.url_var = tk.StringVar(value=DEFAULT_URL)
        ttk.Entry(settings, textvariable=self.url_var).grid(row=0, column=1, sticky="ew", pady=4)
        ttk.Label(settings, text="測定回数").grid(row=1, column=0, sticky="w", padx=(0, 8), pady=4)
        self.count_var = tk.StringVar(value=str(DEFAULT_COUNT))
        ttk.Spinbox(settings, from_=1, to=100, textvariable=self.count_var, width=8).grid(row=1, column=1, sticky="w", pady=4)
        settings.columnconfigure(1, weight=1)

        buttons = ttk.Frame(main)
        buttons.pack(fill="x", pady=10)
        self.start_button = ttk.Button(buttons, text="測定開始", command=self.start_test)
        self.start_button.pack(side="left")
        self.stop_button = ttk.Button(buttons, text="停止", command=self.stop_test, state="disabled")
        self.stop_button.pack(side="left", padx=8)
        self.save_button = ttk.Button(buttons, text="CSVに保存", command=self.save_csv, state="disabled")
        self.save_button.pack(side="left")
        self.progress = ttk.Progressbar(buttons, mode="determinate", maximum=100)
        self.progress.pack(side="left", fill="x", expand=True, padx=(14, 0))

        summary_frame = ttk.LabelFrame(main, text="測定結果の概要", padding=10)
        summary_frame.pack(fill="x", pady=(0, 10))
        self.summary_vars: dict[str, tk.StringVar] = {}
        summary_items = [
            ("success_rate", "成功率"), ("average", "平均応答時間"),
            ("min", "最短応答時間"), ("max", "最長応答時間"),
            ("jitter", "応答時間の変動幅"), ("counts", "成功 / 失敗"),
        ]
        for index, (key, label) in enumerate(summary_items):
            row, column = divmod(index, 3)
            cell = ttk.Frame(summary_frame, padding=5)
            cell.grid(row=row, column=column, sticky="ew")
            ttk.Label(cell, text=label, foreground="#555555").pack(anchor="w")
            var = tk.StringVar(value="--")
            self.summary_vars[key] = var
            ttk.Label(cell, textvariable=var, font=("Segoe UI", 12, "bold")).pack(anchor="w")
        for column in range(3):
            summary_frame.columnconfigure(column, weight=1)

        table_frame = ttk.LabelFrame(main, text="測定履歴", padding=8)
        table_frame.pack(fill="both", expand=True)
        columns = ("attempt", "time", "status", "elapsed", "error")
        self.tree = ttk.Treeview(table_frame, columns=columns, show="headings", height=12)
        headings = {"attempt": "回数", "time": "測定時刻", "status": "結果", "elapsed": "応答時間 (ms)", "error": "備考"}
        widths = {"attempt": 55, "time": 165, "status": 85, "elapsed": 120, "error": 300}
        for column in columns:
            self.tree.heading(column, text=headings[column])
            self.tree.column(column, width=widths[column], anchor="center" if column != "error" else "w")
        scrollbar = ttk.Scrollbar(table_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scrollbar.set)
        self.tree.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        self.status_var = tk.StringVar(value="測定先と回数を設定して「測定開始」を押してください。")
        ttk.Label(main, textvariable=self.status_var, foreground="#444444", wraplength=820).pack(anchor="w", pady=(10, 0))
        ttk.Label(
            main,
            text="注意：これは指定URLへの一般的なHTTPS応答測定です。PlayStation Portalのストリーミング経路・映像品質・ゲーム内遅延を直接測定するものではありません。",
            foreground="#8a4b08", wraplength=820,
        ).pack(anchor="w", pady=(6, 0))

    def start_test(self) -> None:
        if self.worker and self.worker.is_alive():
            return
        url = self.url_var.get().strip()
        if not (url.startswith("https://") or url.startswith("http://")):
            messagebox.showerror("URLエラー", "URLは https:// または http:// から入力してください。")
            return
        try:
            count = int(self.count_var.get())
            if not 1 <= count <= 100:
                raise ValueError
        except ValueError:
            messagebox.showerror("回数エラー", "測定回数は1～100の整数で入力してください。")
            return

        self.measurements.clear()
        for item in self.tree.get_children():
            self.tree.delete(item)
        self._update_summary()
        self.progress.configure(value=0, maximum=count)
        self.stop_event.clear()
        self.start_button.configure(state="disabled")
        self.stop_button.configure(state="normal")
        self.save_button.configure(state="disabled")
        self.status_var.set("測定中です。ネットワーク状況によって時間がかかる場合があります。")
        self.worker = threading.Thread(target=self._run_measurements, args=(url, count), daemon=True)
        self.worker.start()

    def _run_measurements(self, url: str, count: int) -> None:
        for attempt in range(1, count + 1):
            if self.stop_event.is_set():
                break
            measurement = measure_once(url, attempt)
            self.events.put(("measurement", measurement))
        self.events.put(("finished", None))

    def stop_test(self) -> None:
        self.stop_event.set()
        self.status_var.set("停止要求を受け付けました。現在の測定が終わると停止します。")
        self.stop_button.configure(state="disabled")

    def _process_events(self) -> None:
        try:
            while True:
                event, payload = self.events.get_nowait()
                if event == "measurement":
                    m = payload
                    assert isinstance(m, Measurement)
                    self.measurements.append(m)
                    elapsed = f"{m.elapsed_ms:.2f}" if m.elapsed_ms is not None else "--"
                    self.tree.insert("", "end", values=(m.attempt, m.timestamp, m.status, elapsed, m.error))
                    self.progress.configure(value=len(self.measurements))
                    self._update_summary()
                elif event == "finished":
                    self.start_button.configure(state="normal")
                    self.stop_button.configure(state="disabled")
                    self.save_button.configure(state="normal" if self.measurements else "disabled")
                    self.status_var.set(f"測定終了：{len(self.measurements)}回の結果を取得しました。必要に応じてCSVに保存できます。")
        except queue.Empty:
            pass
        self.root.after(100, self._process_events)

    def _update_summary(self) -> None:
        stats = summarize(self.measurements)
        def fmt(value: object) -> str:
            return "--" if value is None else f"{float(value):.2f} ms"
        self.summary_vars["success_rate"].set(f"{stats['success_rate']:.1f}%")
        self.summary_vars["average"].set(fmt(stats["average_ms"]))
        self.summary_vars["min"].set(fmt(stats["min_ms"]))
        self.summary_vars["max"].set(fmt(stats["max_ms"]))
        self.summary_vars["jitter"].set(fmt(stats["jitter_ms"]))
        self.summary_vars["counts"].set(f"{stats['success']} / {stats['failure']}")

    def save_csv(self) -> None:
        if not self.measurements:
            return
        path = filedialog.asksaveasfilename(
            title="CSVファイルを保存",
            defaultextension=".csv",
            initialfile=f"network_measurements_{datetime.now():%Y%m%d_%H%M%S}.csv",
            filetypes=[("CSVファイル", "*.csv"), ("すべてのファイル", "*.*")],
        )
        if not path:
            return
        try:
            with Path(path).open("w", newline="", encoding="utf-8-sig") as file:
                writer = csv.writer(file)
                writer.writerow(["attempt", "timestamp", "url", "status", "elapsed_ms", "error"])
                for m in self.measurements:
                    writer.writerow([m.attempt, m.timestamp, m.url, m.status,
                                     "" if m.elapsed_ms is None else f"{m.elapsed_ms:.3f}", m.error])
            messagebox.showinfo("保存完了", f"CSVを保存しました。\n{path}")
        except OSError as exc:
            messagebox.showerror("保存エラー", f"CSVを保存できませんでした。\n{exc}")


def main() -> None:
    root = tk.Tk()
    try:
        style = ttk.Style(root)
        if "vista" in style.theme_names():
            style.theme_use("vista")
    except tk.TclError:
        pass
    NetworkStabilityApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()

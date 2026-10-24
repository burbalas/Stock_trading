"""A local desktop control window for the supervised paper-trading application."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import math
import os
from pathlib import Path
from queue import Empty, Queue
import subprocess
import sys
import tkinter as tk
from tkinter import messagebox, ttk
from tkinter.scrolledtext import ScrolledText

from .time_utils import LOCAL_TZ


BACKGROUND = "#f4f6fa"
TEXT = "#17243b"
MUTED = "#59677d"
ACCENT = "#2257c7"


def _money(value) -> str:
    try:
        amount = float(value)
        return f"${amount:,.2f}" if math.isfinite(amount) else "Unavailable"
    except (ValueError, TypeError):
        return "Unavailable"


def _number(value) -> str:
    try:
        number = float(value)
        return f"{number:,.6f}".rstrip("0").rstrip(".") if math.isfinite(number) else "—"
    except (ValueError, TypeError):
        return "—"


def _timestamp(value) -> str:
    if not value:
        return "Never refreshed"
    try:
        timestamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return timestamp.astimezone(LOCAL_TZ).strftime("%d %b %Y, %H:%M:%S %Z")
    except (ValueError, TypeError):
        return str(value)


def _rows(value) -> list[dict]:
    if isinstance(value, list):
        return [row for row in value if isinstance(row, dict)]
    if isinstance(value, dict):
        return [{"symbol": symbol, **row} for symbol, row in value.items() if isinstance(row, dict)]
    return []


class TradingDesktop:
    """Keep network and process operations off the Tk event thread."""

    def __init__(self, root: tk.Tk, application, *, smoke_test: bool = False) -> None:
        self.root = root
        self.application = application
        self.smoke_test = smoke_test
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="paper-controls")
        self._results = Queue()
        self._busy = False
        self._status_pending = False
        self._closed = False
        self._last_status: dict = {}
        self._last_logs = None
        self._buttons: dict[str, ttk.Button] = {}
        self._build()
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.root.after(75, self._drain_results)
        self.root.after(100, self._initial_load)
        self.root.after(3000, self._poll_status)
        if smoke_test:
            self.root.after(1400, self.close)

    def _build(self) -> None:
        root = self.root
        root.title("Stock Trading — Paper Control")
        root.geometry(f"1160x{min(820, root.winfo_screenheight() - 85)}")
        root.minsize(1000, 680)
        root.configure(background=BACKGROUND)
        style = ttk.Style(root)
        if "clam" in style.theme_names():
            style.theme_use("clam")
        style.configure("TFrame", background=BACKGROUND)
        style.configure("TLabel", background=BACKGROUND, foreground=TEXT, font=("Segoe UI", 10))
        style.configure("Muted.TLabel", foreground=MUTED)
        style.configure("Title.TLabel", font=("Segoe UI", 23, "bold"))
        style.configure("Section.TLabel", font=("Segoe UI", 11, "bold"))
        style.configure("Card.TFrame", background="white")
        style.configure("Card.TLabel", background="white", foreground=MUTED)
        style.configure("Metric.TLabel", background="white", foreground=TEXT, font=("Segoe UI", 21, "bold"))
        style.configure("Badge.TLabel", background="#e4f1ed", foreground="#216549", padding=(14, 8), font=("Segoe UI", 10, "bold"))
        style.configure("TButton", padding=(12, 9), font=("Segoe UI", 10))
        style.configure("TCheckbutton", background=BACKGROUND, foreground=TEXT, font=("Segoe UI", 10))
        style.map("TCheckbutton", background=[("active", BACKGROUND)])
        style.configure("Primary.TButton", background=ACCENT, foreground="white")
        style.map("Primary.TButton", background=[("active", "#1747a7"), ("disabled", "#a8b8d7")])
        style.configure("Treeview", rowheight=28, font=("Segoe UI", 10))
        style.configure("Treeview.Heading", font=("Segoe UI", 9, "bold"), padding=7)

        shell = ttk.Frame(root, padding=(26, 20, 26, 14))
        shell.pack(fill="both", expand=True)
        shell.columnconfigure(0, weight=1)
        shell.rowconfigure(5, weight=1)

        heading = ttk.Frame(shell)
        heading.grid(row=0, column=0, sticky="ew")
        heading.columnconfigure(0, weight=1)
        ttk.Label(heading, text="Stock Trading", style="Title.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(heading, text="PAPER MONEY ONLY", style="Badge.TLabel").grid(row=0, column=1, rowspan=2, sticky="e")
        ttk.Label(heading, text="Run and monitor the frozen relative-strength candidate.", style="Muted.TLabel").grid(row=1, column=0, sticky="w", pady=(4, 0))

        status_panel = ttk.Frame(shell, padding=(0, 17, 0, 12))
        status_panel.grid(row=1, column=0, sticky="ew")
        self.manager_text = tk.StringVar(value="Manager: Checking local status…")
        self.operator_text = tk.StringVar(value="Trading worker: No status loaded")
        self.broker_text = tk.StringVar(value="Broker account: Not refreshed")
        self.manager_error_text = tk.StringVar(value="")
        self.paused_text = tk.StringVar(value="")
        ttk.Label(status_panel, textvariable=self.manager_text, style="Section.TLabel").pack(anchor="w")
        self.manager_error_label = ttk.Label(status_panel, textvariable=self.manager_error_text, foreground="#b42332", wraplength=920)
        ttk.Label(status_panel, textvariable=self.operator_text, style="Muted.TLabel").pack(anchor="w", pady=(4, 0))
        ttk.Label(status_panel, textvariable=self.broker_text, style="Muted.TLabel", wraplength=920).pack(anchor="w", pady=(4, 0))
        ttk.Label(status_panel, textvariable=self.paused_text, foreground="#865700").pack(anchor="w", pady=(4, 0))

        cards = ttk.Frame(shell)
        cards.grid(row=2, column=0, sticky="ew", pady=(0, 14))
        self.metrics = {}
        for column, (key, title) in enumerate((('equity', 'Paper equity'), ('cash', 'Available cash'), ('positions', 'Positions'), ('orders', 'Open orders'))):
            cards.columnconfigure(column, weight=1)
            card = ttk.Frame(cards, style="Card.TFrame", padding=(17, 12))
            card.grid(row=0, column=column, sticky="nsew", padx=(0, 12 if column < 3 else 0))
            ttk.Label(card, text=title, style="Card.TLabel").pack(anchor="w")
            value = tk.StringVar(value="—")
            ttk.Label(card, textvariable=value, style="Metric.TLabel").pack(anchor="w", pady=(5, 0))
            self.metrics[key] = value

        controls = ttk.Frame(shell)
        controls.grid(row=3, column=0, sticky="ew", pady=(0, 7))
        actions = (
            ("start", "Start paper trading", self.application.start, "Primary.TButton"),
            ("stop", "Stop bot", self.application.stop, "TButton"),
            ("pause", "Pause buys", self.application.pause_buys, "TButton"),
            ("resume", "Resume buys", self.application.resume_buys, "TButton"),
            ("refresh", "Refresh broker", self.application.refresh_broker, "TButton"),
            ("report", "Create report", self.application.write_report, "TButton"),
        )
        for column, (key, title, operation, button_style) in enumerate(actions):
            button = ttk.Button(controls, text=title, style=button_style,
                                command=lambda action=key, callback=operation: self._action(action, callback))
            button.grid(row=0, column=column, padx=(0, 7), sticky="w")
            self._buttons[key] = button
        self._buttons['logs'] = ttk.Button(controls, text="Open logs", command=self._open_logs)
        self._buttons['logs'].grid(row=0, column=6, sticky="w")

        self.message_text = tk.StringVar(value="Loading local status. Opening this window does not start trading.")
        self.message_label = ttk.Label(shell, textvariable=self.message_text, style="Muted.TLabel", wraplength=1060)
        self.message_label.grid(row=4, column=0, sticky="ew", pady=(0, 12))

        body = ttk.Frame(shell)
        body.grid(row=5, column=0, sticky="nsew")
        body.columnconfigure(0, weight=1, uniform='body')
        body.columnconfigure(1, weight=1, uniform='body')
        body.rowconfigure(1, weight=1)
        ttk.Label(body, text="Account positions", style="Section.TLabel").grid(row=0, column=0, sticky="w", pady=(0, 7))
        ttk.Label(body, text="Recent activity", style="Section.TLabel").grid(row=0, column=1, sticky="w", padx=(18, 0), pady=(0, 7))
        positions_panel = ttk.Frame(body)
        positions_panel.grid(row=1, column=0, sticky="nsew")
        positions_panel.rowconfigure(0, weight=1)
        positions_panel.columnconfigure(0, weight=1)
        self.positions = ttk.Treeview(positions_panel, columns=('symbol', 'quantity', 'cost', 'price', 'value', 'pnl'), show="headings", height=4)
        for key, title, width in (('symbol', 'Symbol', 70), ('quantity', 'Shares', 60), ('cost', 'Avg cost', 85), ('price', 'Last price', 85), ('value', 'Value', 95), ('pnl', 'Open P/L', 90)):
            self.positions.heading(key, text=title)
            self.positions.column(key, width=width, anchor="w" if key == 'symbol' else "e")
        self.positions.grid(row=0, column=0, sticky="nsew")
        positions_scroll = ttk.Scrollbar(positions_panel, orient='vertical', command=self.positions.yview)
        positions_scroll.grid(row=0, column=1, sticky='ns')
        self.positions.configure(yscrollcommand=positions_scroll.set)
        self.log_view = ScrolledText(body, height=6, width=45, wrap="word", font=("Consolas", 9),
                                     background="#152136", foreground="#e2e8f0", insertbackground="white",
                                     relief="flat", borderwidth=0, padx=12, pady=10, state="disabled")
        self.log_view.grid(row=1, column=1, sticky="nsew", padx=(18, 0))

        footer = ttk.Frame(shell)
        footer.grid(row=6, column=0, sticky="ew", pady=(12, 0))
        startup_row = ttk.Frame(footer)
        startup_row.pack(fill='x', pady=(0, 6))
        self.autostart_var = tk.BooleanVar(value=False)
        self.autostart_check = ttk.Checkbutton(startup_row, text='Resume on Windows sign-in', variable=self.autostart_var, command=self._toggle_autostart)
        self.autostart_check.pack(side='left')
        ttk.Label(startup_row, text='Resumes only if paper trading was left enabled. Stop bot keeps it stopped.', style='Muted.TLabel', font=('Segoe UI', 9)).pack(side='left', padx=(12, 0))
        ttk.Label(footer, text="The active manager prevents idle sleep on Windows. Restart still needs sign-in resume or Start. Live trading is not supported.", style="Muted.TLabel").pack(anchor="w")
        ttk.Label(footer, text="Closing this window leaves the manager running. Stop bot ends monitoring; broker protective orders remain active.", style="Muted.TLabel").pack(anchor="w", pady=(3, 0))
        self.runtime_text = tk.StringVar(value="")
        ttk.Label(footer, textvariable=self.runtime_text, style="Muted.TLabel", font=("Segoe UI", 8)).pack(anchor="w", pady=(6, 0))
        self._set_controls()

    def _initial_load(self) -> None:
        def initialize():
            if not self.smoke_test:
                self.application.ensure_runtime_if_needed()
        self._action("initialize", initialize)

    def _action(self, kind: str, operation) -> None:
        if self._closed or self._busy:
            return
        self._busy = True
        self._set_controls()
        labels = {'initialize': 'Loading local paper workspace…', 'start': 'Starting paper manager…', 'stop': 'Stopping paper manager…',
                  'pause': 'Pausing new buys…', 'resume': 'Resuming new buys…', 'refresh': 'Refreshing the Alpaca paper account…',
                  'report': 'Creating report…', 'open_logs': 'Opening logs…', 'open_report': 'Opening the saved report…',
                  'autostart': 'Updating the Windows sign-in setting…'}
        self._set_message(labels.get(kind, 'Working…'))
        self._executor.submit(self._work, kind, operation)

    def _work(self, kind: str, operation) -> None:
        try:
            value = operation()
            status = self.application.status()
            logs = self.application.logs()
            self._results.put((kind, value, status, logs, None))
        except Exception as exc:
            self._results.put((kind, None, None, None, str(exc) or type(exc).__name__))

    def _poll_status(self) -> None:
        if self._closed:
            return
        if not self._busy and not self._status_pending:
            self._status_pending = True
            self._executor.submit(self._work, "status", lambda: None)
        self.root.after(3000, self._poll_status)

    def _drain_results(self) -> None:
        if self._closed:
            return
        while True:
            try:
                kind, value, status, logs, error = self._results.get_nowait()
            except Empty:
                break
            if kind == 'status':
                self._status_pending = False
            else:
                self._busy = False
            if error:
                if kind == 'autostart':
                    self.autostart_var.set(bool(self._last_status.get('autostart', False)))
                self._set_message(f"Action failed: {error}", error=True)
            else:
                self._render(status, logs)
                messages = {'initialize': 'Local workspace ready. Opening the app does not start trading.',
                            'start': 'Paper manager start requested. Its current state is shown above.',
                            'stop': 'Stop requested. Check the manager and trading-worker states above.',
                            'pause': 'New buys are paused. Position exits and risk checks remain active.',
                            'resume': 'New buys are enabled when the strategy and risk limits permit.',
                            'refresh': 'Paper account refreshed.', 'report': f'Report saved: {value}',
                            'open_logs': 'Logs opened.', 'open_report': f'Report opened: {value}',
                            'autostart': 'Windows sign-in setting saved. It resumes only when paper trading was left enabled.'}
                if kind != 'status':
                    self._set_message(messages.get(kind, 'Done.'))
                if kind == 'initialize' and not self.smoke_test:
                    self._action('refresh', self.application.refresh_broker)
                elif kind == 'report' and value:
                    self._action('open_report', lambda report=Path(value): self._open_path(report))
            self._set_controls()
        self.root.after(100, self._drain_results)

    def _render(self, status: dict, logs: str) -> None:
        self._last_status = status if isinstance(status, dict) else {}
        supervisor = self._last_status.get('supervisor') or {}
        operator = self._last_status.get('operator') or {}
        broker = self._last_status.get('broker') or {}
        if not isinstance(supervisor, dict):
            supervisor = {}
        if not isinstance(operator, dict):
            operator = {}
        if not isinstance(broker, dict):
            broker = {}
        manager_state = str(supervisor.get('state') or supervisor.get('status') or ('running' if supervisor.get('running') else 'stopped'))
        if supervisor.get('manager_stale'):
            manager_state = 'monitoring_unverified'
        pid = supervisor.get('worker_pid') or supervisor.get('pid')
        restarts = supervisor.get('restarts', 0)
        self.manager_text.set(f"Manager: {manager_state.replace('_', ' ').capitalize()}" + (f" · Worker PID {pid}" if pid else '') + (f" · Restarts: {restarts}" if restarts else ''))
        warnings = [supervisor.get('last_error'), supervisor.get('power_warning')]
        self.manager_error_text.set(' '.join(' '.join(str(value) for value in warnings if value).split())[:450])
        if self.manager_error_text.get():
            self.manager_error_label.pack(anchor='w')
        else:
            self.manager_error_label.pack_forget()
        detail = str(operator.get('detail') or operator.get('status') or operator.get('state') or 'No heartbeat recorded')
        operator_state = str(operator.get('state', ''))
        if operator_state in {'stale', 'invalid', 'failed'} and operator_state not in detail:
            detail = f"{operator_state}: {detail}"
        worker_stamp = operator.get('updated_at')
        self.operator_text.set(f"Trading worker: {detail.replace('_', ' ')}" + (f" · Last heartbeat {_timestamp(worker_stamp)}" if worker_stamp else ''))
        broker_stamp = broker.get('refreshed_at') or broker.get('updated_at') or broker.get('timestamp') or broker.get('fetched_at')
        broker_error = broker.get('error') or broker.get('last_error')
        reconciliation = broker.get('reconciliation_ok')
        market = broker.get('market_open')
        broker_details = [f"Broker account: Last refresh {_timestamp(broker_stamp)}"]
        if broker.get('account_suffix'):
            broker_details.append(f"Paper account …{broker['account_suffix']}")
        if reconciliation is not None:
            broker_details.append('Reconciled' if reconciliation else 'Reconciliation requires attention')
        if 'unprotected_symbols' in broker:
            uncovered = broker.get('unprotected_symbols') or []
            broker_details.append('STOP CHECK: ' + ', '.join(uncovered) if uncovered else 'Stops confirmed at last refresh')
        if market is not None:
            broker_details.append('Market open at last refresh' if market else 'Market closed at last refresh')
        if broker_error:
            broker_details.append(str(broker_error))
        self.broker_text.set(' · '.join(broker_details))
        if self._last_status.get('stop_requested'):
            self.paused_text.set('Stop requested. The manager is finishing its shutdown.' if supervisor.get('manager_running') or supervisor.get('worker_running')
                                 else 'Bot stopped. Broker protective orders remain active; monitoring is off.')
        elif supervisor.get('recovery_required'):
            self.paused_text.set('Monitoring is off. Start paper trading to recover the previous enabled session.')
        elif self._last_status.get('paused'):
            self.paused_text.set('NEW BUYS PAUSED · Existing positions remain managed while the bot is running.')
        else:
            self.paused_text.set('New buys enabled when the bot is running and strategy and risk checks permit.')
        account = broker.get('account') if isinstance(broker.get('account'), dict) else broker
        positions = _rows(broker.get('positions'))
        orders = broker.get('open_orders', broker.get('orders'))
        order_count = len(orders) if isinstance(orders, (list, dict)) else broker.get('open_order_count')
        self.metrics['equity'].set(_money(account.get('equity')))
        self.metrics['cash'].set(_money(account.get('cash')))
        self.metrics['positions'].set(str(len(positions)) if 'positions' in broker else '—')
        self.metrics['orders'].set(str(order_count) if order_count is not None else '—')
        self.positions.delete(*self.positions.get_children())
        for position in positions:
            quantity = position.get('qty', position.get('quantity'))
            price = position.get('current_price', position.get('price'))
            average = position.get('avg_entry_price', position.get('average_cost'))
            value = position.get('market_value', position.get('value'))
            pnl = position.get('unrealized_pl', position.get('unrealized_pnl'))
            try:
                if value is None:
                    value = float(quantity) * float(price)
                if pnl is None:
                    pnl = float(quantity) * (float(price) - float(average))
            except (ValueError, TypeError):
                pass
            self.positions.insert('', 'end', values=(
                str(position.get('symbol', '')), _number(quantity), _money(average), _money(price), _money(value), _money(pnl),
            ))
        tail = '\n'.join(str(logs or '').splitlines()[-250:])[-24000:]
        if tail != self._last_logs:
            at_end = self.log_view.yview()[1] >= 0.98
            self.log_view.configure(state='normal')
            self.log_view.delete('1.0', 'end')
            self.log_view.insert('1.0', tail or 'No local activity recorded yet.')
            self.log_view.configure(state='disabled')
            if at_end or self._last_logs is None:
                self.log_view.see('end')
            self._last_logs = tail
        self.runtime_text.set(f"Workspace: {self._last_status.get('runtime_dir', 'Not initialized')}")
        self.autostart_var.set(bool(self._last_status.get('autostart', False)))

    def _set_controls(self) -> None:
        for button in self._buttons.values():
            button.configure(state='disabled' if self._busy else 'normal')
        self.autostart_check.configure(state='disabled' if self._busy else 'normal')
        if not self._busy:
            supervisor = self._last_status.get('supervisor') or {}
            if isinstance(supervisor, dict) and (supervisor.get('running') or supervisor.get('manager_running') or supervisor.get('state', supervisor.get('status')) in {'starting', 'running', 'restarting', 'stopping'}):
                self._buttons['start'].configure(state='disabled')
            self._buttons['pause' if self._last_status.get('paused') else 'resume'].configure(state='disabled')

    def _set_message(self, text: str, *, error: bool = False) -> None:
        if error:
            text = ' '.join(text.split())
            if len(text) > 450:
                text = text[:447] + '…'
        self.message_text.set(text)
        self.message_label.configure(foreground='#b42332' if error else MUTED)

    def _open_logs(self) -> None:
        log_path = self._last_status.get('log_path')
        runtime_dir = self._last_status.get('runtime_dir', '.')

        def open_logs():
            path = Path(log_path) if log_path else Path(runtime_dir)
            return self._open_path(path if path.exists() else path.parent)
        self._action('open_logs', open_logs)

    def _toggle_autostart(self) -> None:
        enabled = bool(self.autostart_var.get())
        self._action('autostart', lambda: self.application.set_autostart(enabled))

    @staticmethod
    def _open_path(path: Path) -> Path:
        if not path.exists():
            raise FileNotFoundError(f"Path does not exist: {path}")
        if sys.platform == 'win32':
            os.startfile(str(path))
        else:
            subprocess.Popen(['open' if sys.platform == 'darwin' else 'xdg-open', str(path)])
        return path

    def close(self) -> None:
        self._closed = True
        self._executor.shutdown(wait=False, cancel_futures=True)
        self.root.destroy()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description='Desktop controls for supervised Alpaca paper trading.')
    parser.add_argument('--smoke-test', action='store_true', help='Open local status briefly, without broker requests or starting trading.')
    args = parser.parse_args(argv)
    root = tk.Tk()
    try:
        from .paper_app import PaperApplication

        TradingDesktop(root, PaperApplication(), smoke_test=args.smoke_test)
        root.mainloop()
        return 0
    except Exception as exc:
        messagebox.showerror('Stock Trading could not open', str(exc), parent=root)
        root.destroy()
        return 1


if __name__ == '__main__':
    raise SystemExit(main())

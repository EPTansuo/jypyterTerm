"""A PTY-backed terminal widget for Jupyter Notebook and JupyterLab.

Usage in a notebook cell:

    from jupyter_terminal import JupyterTerminal
    term = JupyterTerminal(height=480)
    term.display()
"""

from __future__ import annotations

import codecs
import errno
import json
import os
import pty
import shutil
import signal
import struct
import subprocess
import termios
import threading
from collections import deque
from functools import lru_cache
from pathlib import Path
from typing import Callable, Deque, Iterable

try:
    import anywidget
    import ipywidgets as widgets
    import traitlets as t
    from IPython import get_ipython
    from IPython.display import display
except Exception:  # pragma: no cover - exercised only outside notebook environments.
    anywidget = None
    widgets = None
    t = None
    get_ipython = None
    display = None


def _normalize_size(value: int | str, fallback_unit: str = "px") -> str:
    if isinstance(value, int):
        return f"{value}{fallback_unit}"
    return value


@lru_cache(maxsize=None)
def _read_asset_text(relative_path: str) -> str:
    base_dir = Path(__file__).resolve().parent
    return (base_dir / relative_path).read_text(encoding="utf-8")


@lru_cache(maxsize=None)
def _build_terminal_widget_css() -> str:
    return (
        _read_asset_text("vendor/xterm/xterm.css")
        + """

.jupyter-terminal-root {
    width: 100%;
}

.jupyter-terminal-host {
    width: 100%;
    border: 1px solid #2b2b2b;
    border-radius: 6px;
    overflow: hidden;
    background: #111111;
    box-sizing: border-box;
}

.jupyter-terminal-host .terminal {
    padding: 8px 10px;
    box-sizing: border-box;
    height: 100%;
}
"""
    )


@lru_cache(maxsize=None)
def _build_terminal_widget_esm() -> str:
    xterm_js = _read_asset_text("vendor/xterm/xterm.js")
    fit_addon_js = _read_asset_text("vendor/xterm/addon-fit.js")
    return f"""
if (!globalThis.Terminal) {{
{xterm_js}
}}

if (!globalThis.FitAddon) {{
{fit_addon_js}
}}

function applyHeight(el, value) {{
    el.style.height = value || "420px";
}}

function parseOptions(model) {{
    try {{
        return JSON.parse(model.get("options_json") || "{{}}");
    }} catch (error) {{
        console.error("failed to parse terminal options", error);
        return {{}};
    }}
}}

export default {{
    render({{ model, el }}) {{
        el.classList.add("jupyter-terminal-root");
        el.innerHTML = "";

        const host = document.createElement("div");
        host.className = "jupyter-terminal-host";
        applyHeight(host, model.get("height"));
        el.appendChild(host);

        const term = new Terminal(parseOptions(model));
        const fitAddon = new FitAddon.FitAddon();
        term.loadAddon(fitAddon);
        term.open(host);

        const fitAndReport = () => {{
            fitAddon.fit();
            model.send({{ type: "resize", rows: term.rows, cols: term.cols }});
        }};

        const ready = () => {{
            fitAndReport();
            term.focus();
            model.send({{ type: "ready", rows: term.rows, cols: term.cols }});
        }};

        const onDataDisposable = term.onData((data) => {{
            model.send({{ type: "input", data }});
        }});

        const onResizeDisposable = term.onResize((size) => {{
            model.send({{ type: "resize", rows: size.rows, cols: size.cols }});
        }});

        const onHeightChange = () => {{
            applyHeight(host, model.get("height"));
            requestAnimationFrame(fitAndReport);
        }};

        const onMessage = (msg) => {{
            const ops = msg && Array.isArray(msg.ops) ? msg.ops : [];
            for (const op of ops) {{
                if (op.op === "write") {{
                    term.write(op.data || "");
                }} else if (op.op === "clear") {{
                    term.clear();
                }} else if (op.op === "reset") {{
                    term.reset();
                    requestAnimationFrame(fitAndReport);
                }} else if (op.op === "fit") {{
                    requestAnimationFrame(fitAndReport);
                }} else if (op.op === "focus") {{
                    term.focus();
                }}
            }}
        }};

        model.on("change:height", onHeightChange);
        model.on("msg:custom", onMessage);

        let resizeObserver = null;
        if (window.ResizeObserver) {{
            resizeObserver = new ResizeObserver(() => {{
                requestAnimationFrame(fitAndReport);
            }});
            resizeObserver.observe(host);
        }}

        requestAnimationFrame(ready);

        return () => {{
            resizeObserver?.disconnect();
            onDataDisposable.dispose();
            onResizeDisposable.dispose();
            model.off("change:height", onHeightChange);
            model.off("msg:custom", onMessage);
            term.dispose();
        }};
    }},
}};
"""


if anywidget is not None and t is not None:
    class _TerminalFrontendWidget(anywidget.AnyWidget):
        _esm = _build_terminal_widget_esm()
        _css = _build_terminal_widget_css()

        height = t.Unicode("420px").tag(sync=True)
        options_json = t.Unicode("{}").tag(sync=True)


class TerminalSession:
    """Run an interactive shell inside a pseudo-terminal."""

    def __init__(
        self,
        *,
        cwd: str | None = None,
        argv: Iterable[str] | None = None,
        env: dict[str, str] | None = None,
        rows: int = 24,
        cols: int = 80,
        on_output: Callable[[str], None] | None = None,
        on_exit: Callable[[int], None] | None = None,
    ) -> None:
        if os.name != "posix":
            raise NotImplementedError("TerminalSession currently requires a POSIX environment.")

        shell = os.environ.get("SHELL") or shutil.which("bash") or "/bin/sh"
        self.argv = list(argv) if argv is not None else [shell, "-i"]
        self.cwd = os.path.abspath(cwd or os.getcwd())
        self.rows = int(rows)
        self.cols = int(cols)
        self.on_output = on_output
        self.on_exit = on_exit

        merged_env = dict(os.environ)
        if env:
            merged_env.update(env)
        merged_env.setdefault("TERM", "xterm-256color")
        merged_env.setdefault("COLORTERM", "truecolor")
        merged_env["LINES"] = str(self.rows)
        merged_env["COLUMNS"] = str(self.cols)
        self.env = merged_env

        self.proc: subprocess.Popen[bytes] | None = None
        self._master_fd: int | None = None
        self._reader_thread: threading.Thread | None = None
        self._write_lock = threading.Lock()
        self._closed = threading.Event()

    def start(self) -> None:
        if self.proc is not None and self.proc.poll() is None:
            return

        master_fd, slave_fd = pty.openpty()
        self._set_winsize(slave_fd, self.rows, self.cols)

        try:
            self.proc = subprocess.Popen(
                self.argv,
                stdin=slave_fd,
                stdout=slave_fd,
                stderr=slave_fd,
                cwd=self.cwd,
                env=self.env,
                close_fds=True,
                start_new_session=True,
            )
        finally:
            os.close(slave_fd)

        self._master_fd = master_fd
        self._closed.clear()
        self._reader_thread = threading.Thread(target=self._reader_loop, daemon=True)
        self._reader_thread.start()

    def is_running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def write(self, data: bytes) -> None:
        master_fd = self._master_fd
        if master_fd is None or not self.is_running():
            return

        with self._write_lock:
            view = memoryview(data)
            while view:
                written = os.write(master_fd, view)
                view = view[written:]

    def resize(self, rows: int, cols: int) -> None:
        self.rows = max(2, int(rows))
        self.cols = max(8, int(cols))
        self.env["LINES"] = str(self.rows)
        self.env["COLUMNS"] = str(self.cols)

        master_fd = self._master_fd
        proc = self.proc
        if master_fd is None:
            return

        self._set_winsize(master_fd, self.rows, self.cols)
        if proc is not None and proc.poll() is None:
            try:
                os.killpg(proc.pid, signal.SIGWINCH)
            except ProcessLookupError:
                pass

    def interrupt(self) -> None:
        proc = self.proc
        if proc is None or proc.poll() is not None:
            return
        try:
            os.killpg(proc.pid, signal.SIGINT)
        except ProcessLookupError:
            pass

    def terminate(self) -> None:
        proc = self.proc
        if proc is None or proc.poll() is not None:
            return
        try:
            os.killpg(proc.pid, signal.SIGHUP)
        except ProcessLookupError:
            return

    def close(self, timeout: float = 1.5) -> None:
        proc = self.proc
        if proc is not None and proc.poll() is None:
            self.terminate()
            try:
                proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                proc.wait(timeout=timeout)

        self._closed.set()
        self._close_master_fd()

    def _reader_loop(self) -> None:
        decoder = codecs.getincrementaldecoder("utf-8")("replace")

        try:
            while not self._closed.is_set():
                master_fd = self._master_fd
                if master_fd is None:
                    break

                try:
                    chunk = os.read(master_fd, 65536)
                except OSError as exc:
                    if exc.errno in (errno.EIO, errno.EBADF):
                        break
                    if exc.errno == errno.EINTR:
                        continue
                    raise

                if not chunk:
                    break

                text = decoder.decode(chunk)
                if text and self.on_output is not None:
                    self.on_output(text)

            tail = decoder.decode(b"", final=True)
            if tail and self.on_output is not None:
                self.on_output(tail)
        finally:
            returncode = -1
            if self.proc is not None:
                returncode = self.proc.wait()
            self._close_master_fd()
            self._closed.set()
            if self.on_exit is not None:
                self.on_exit(returncode)

    def _close_master_fd(self) -> None:
        if self._master_fd is not None:
            try:
                os.close(self._master_fd)
            except OSError:
                pass
            finally:
                self._master_fd = None

    @staticmethod
    def _set_winsize(fd: int, rows: int, cols: int) -> None:
        size = struct.pack("HHHH", rows, cols, 0, 0)
        termios.tcsetwinsize(fd, (rows, cols)) if hasattr(termios, "tcsetwinsize") else None
        try:
            import fcntl

            fcntl.ioctl(fd, termios.TIOCSWINSZ, size)
        except Exception:
            pass


class JupyterTerminal:
    """Render an xterm.js frontend backed by a PTY shell."""

    def __init__(
        self,
        *,
        cwd: str | None = None,
        argv: Iterable[str] | None = None,
        env: dict[str, str] | None = None,
        rows: int = 24,
        cols: int = 80,
        height: int | str = 420,
        theme: dict[str, str] | None = None,
        scrollback: int = 5000,
        font_size: int = 14,
    ) -> None:
        if anywidget is None or widgets is None or display is None or get_ipython is None:
            raise RuntimeError("anywidget, ipywidgets and IPython are required to use JupyterTerminal.")
        if get_ipython() is None:
            raise RuntimeError("JupyterTerminal must be created inside an IPython kernel.")

        self.cwd = os.path.abspath(cwd or os.getcwd())
        self.argv = list(argv) if argv is not None else None
        self.env = dict(env or {})
        self.rows = int(rows)
        self.cols = int(cols)
        self.height = _normalize_size(height)
        self.scrollback = int(scrollback)
        self.font_size = int(font_size)
        self.theme = theme or {
            "background": "#111111",
            "foreground": "#f2f2f2",
            "cursor": "#f5f5f5",
            "selectionBackground": "#264f78",
            "black": "#000000",
            "red": "#cd3131",
            "green": "#0dbc79",
            "yellow": "#e5e510",
            "blue": "#2472c8",
            "magenta": "#bc3fbc",
            "cyan": "#11a8cd",
            "white": "#e5e5e5",
            "brightBlack": "#666666",
            "brightRed": "#f14c4c",
            "brightGreen": "#23d18b",
            "brightYellow": "#f5f543",
            "brightBlue": "#3b8eea",
            "brightMagenta": "#d670d6",
            "brightCyan": "#29b8db",
            "brightWhite": "#ffffff",
        }

        self._displayed = False
        self._frontend_ready = False
        self._outbox: Deque[dict[str, str]] = deque()
        self._outbox_lock = threading.Lock()
        self._session_token = 0
        self._flush_pending = False

        self.status = widgets.HTML(
            value=f"<span style='font-family:monospace'>starting in {self.cwd}</span>"
        )
        self.interrupt_button = widgets.Button(
            description="Interrupt",
            button_style="warning",
            layout=widgets.Layout(width="110px"),
        )
        self.restart_button = widgets.Button(
            description="Restart Shell",
            layout=widgets.Layout(width="120px"),
        )
        self.clear_button = widgets.Button(
            description="Clear",
            layout=widgets.Layout(width="80px"),
        )
        self.fit_button = widgets.Button(
            description="Fit",
            layout=widgets.Layout(width="70px"),
        )

        options = {
            "cols": self.cols,
            "rows": self.rows,
            "fontSize": self.font_size,
            "cursorBlink": True,
            "scrollback": self.scrollback,
            "allowTransparency": False,
            "convertEol": False,
            "theme": self.theme,
        }
        self._terminal_widget = _TerminalFrontendWidget(
            height=self.height,
            options_json=json.dumps(options),
            layout=widgets.Layout(width="100%"),
        )
        self._terminal_widget.on_msg(self._on_frontend_message)

        self.widget = widgets.VBox(
            [
                widgets.HBox(
                    [
                        self.interrupt_button,
                        self.restart_button,
                        self.clear_button,
                        self.fit_button,
                        self.status,
                    ],
                    layout=widgets.Layout(align_items="center", gap="8px"),
                ),
                self._terminal_widget,
            ]
        )

        self.interrupt_button.on_click(lambda _: self.interrupt())
        self.restart_button.on_click(lambda _: self.restart())
        self.clear_button.on_click(lambda _: self.clear())
        self.fit_button.on_click(lambda _: self.fit())

        self._session = self._make_session()

    def display(self) -> None:
        if not self._displayed:
            display(self.widget)
            self._displayed = True

        self.start()
        self.fit()

    def start(self) -> None:
        if self._session.is_running():
            return
        self._set_status(f"running in {self.cwd}")
        self._session.start()

    def interrupt(self) -> None:
        self._session.interrupt()

    def clear(self) -> None:
        self._enqueue_op({"op": "clear"})
        self.focus()

    def fit(self) -> None:
        self._enqueue_op({"op": "fit"})

    def focus(self) -> None:
        self._enqueue_op({"op": "focus"})

    def restart(self) -> None:
        self._set_status("restarting shell")
        old_session = self._session
        self._session = self._make_session()
        old_session.close()
        with self._outbox_lock:
            self._outbox.clear()
        self._enqueue_op({"op": "reset"})
        self.start()

    def close(self) -> None:
        old_session = self._session
        self._session = self._make_session()
        old_session.close()
        self._set_status("closed")

    def _handle_output(self, token: int, text: str) -> None:
        if token != self._session_token:
            return
        self._enqueue_op({"op": "write", "data": text})

    def _handle_exit(self, token: int, returncode: int) -> None:
        if token != self._session_token:
            return
        self._set_status(f"shell exited with code {returncode}")
        self._enqueue_op({"op": "write", "data": f"\r\n[process exited {returncode}]\r\n"})

    def _make_session(self) -> TerminalSession:
        self._session_token += 1
        token = self._session_token
        return TerminalSession(
            cwd=self.cwd,
            argv=self.argv,
            env=self.env,
            rows=self.rows,
            cols=self.cols,
            on_output=lambda text: self._handle_output(token, text),
            on_exit=lambda returncode: self._handle_exit(token, returncode),
        )

    def _enqueue_op(self, op: dict[str, str]) -> None:
        with self._outbox_lock:
            if op["op"] == "write" and self._outbox and self._outbox[-1]["op"] == "write":
                self._outbox[-1]["data"] += op["data"]
            else:
                self._outbox.append(op)
        self._schedule_flush()

    def _schedule_flush(self) -> None:
        self._run_on_main_thread(self._flush_outbox)

    def _flush_outbox(self) -> None:
        if not self._frontend_ready:
            return

        self._flush_pending = False
        ops: list[dict[str, str]] = []
        total_chars = 0

        with self._outbox_lock:
            while self._outbox and len(ops) < 64:
                op = self._outbox[0]
                op_size = len(op.get("data", ""))
                if ops and total_chars + op_size > 16000:
                    break
                ops.append(self._outbox.popleft())
                total_chars += op_size

        if not ops:
            return

        payload_ops = []
        for op in ops:
            if op["op"] == "write":
                payload_ops.append({"op": "write", "data": op["data"]})
            else:
                payload_ops.append(op)

        self._terminal_widget.send({"ops": payload_ops})

        with self._outbox_lock:
            has_more = bool(self._outbox)
        if has_more:
            self._schedule_flush()

    def _on_frontend_message(
        self,
        _widget: widgets.Widget,
        message: dict,
        buffers: list[memoryview],
    ) -> None:
        if not isinstance(message, dict):
            return

        msg_type = message.get("type")
        if msg_type == "ready":
            self._frontend_ready = True
            self.rows = int(message.get("rows", self.rows))
            self.cols = int(message.get("cols", self.cols))
            self._session.resize(self.rows, self.cols)
            self.focus()
            self._flush_outbox()
            return

        if msg_type == "input":
            data = message.get("data", "")
            if data:
                self._session.write(data.encode("utf-8"))
            return

        if msg_type == "resize":
            self.rows = int(message.get("rows", self.rows))
            self.cols = int(message.get("cols", self.cols))
            self._session.resize(self.rows, self.cols)
            return

    def _set_status(self, text: str) -> None:
        self._run_on_main_thread(
            lambda: setattr(
                self.status,
                "value",
                f"<span style='font-family:monospace'>{text}</span>",
            )
        )

    @staticmethod
    def _run_on_main_thread(func: Callable[[], None]) -> None:
        ip = get_ipython()
        if ip is None:
            func()
            return

        if threading.current_thread() is threading.main_thread():
            func()
            return

        ip.kernel.io_loop.add_callback(func)

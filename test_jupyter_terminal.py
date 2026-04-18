import queue
import shutil
import time
import json
from pathlib import Path
from typing import List

import pytest

import jupyter_terminal
from jupyter_terminal import (
    JupyterTerminal,
    TerminalSession,
    _build_terminal_init_script,
)


def _wait_for(predicate, timeout=5.0, interval=0.05):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


@pytest.fixture
def bash_argv():
    bash = shutil.which("bash")
    if not bash:
        pytest.skip("bash not available")
    return [bash, "--noprofile", "--norc", "-i"]


def test_terminal_session_runs_interactive_shell_and_echoes_output(tmp_path, bash_argv):
    output = queue.Queue()
    exit_codes = queue.Queue()

    session = TerminalSession(
        cwd=str(tmp_path),
        argv=bash_argv,
        env={"PS1": "", "PROMPT_COMMAND": ""},
        rows=24,
        cols=80,
        on_output=output.put,
        on_exit=exit_codes.put,
    )

    session.start()
    session.write(b'printf "__READY__\\n"\n')

    chunks = []
    assert _wait_for(lambda: any("__READY__" in chunk for chunk in list(chunks) + list(_drain(output, chunks))), timeout=5)

    session.write(b"pwd\n")
    assert _wait_for(lambda: any(str(tmp_path) in chunk for chunk in chunks + list(_drain(output, chunks))), timeout=5)

    session.write(b"exit\n")
    assert _wait_for(lambda: not session.is_running(), timeout=5)
    assert exit_codes.get(timeout=5) == 0


def test_terminal_session_resize_updates_stty_size(bash_argv):
    output = queue.Queue()
    chunks = []

    session = TerminalSession(
        argv=bash_argv,
        env={"PS1": "", "PROMPT_COMMAND": ""},
        rows=24,
        cols=80,
        on_output=output.put,
    )

    session.start()
    session.resize(40, 100)
    session.write(b"stty size\n")

    assert _wait_for(lambda: any("40 100" in chunk for chunk in chunks + list(_drain(output, chunks))), timeout=5)
    session.close()


def test_terminal_session_interrupt_returns_control(bash_argv):
    output = queue.Queue()
    chunks = []

    session = TerminalSession(
        argv=bash_argv,
        env={"PS1": "", "PROMPT_COMMAND": ""},
        rows=24,
        cols=80,
        on_output=output.put,
    )

    session.start()
    session.write(b"sleep 10\n")
    time.sleep(0.4)
    session.interrupt()
    session.write(b'printf "__AFTER__\\n"\n')

    assert _wait_for(lambda: any("__AFTER__" in chunk for chunk in chunks + list(_drain(output, chunks))), timeout=5)
    session.close()


def test_terminal_widget_frontend_maps_ctrl_c_to_sigint_input():
    init_js = _build_terminal_init_script(
        terminal_id="term-test",
        bridge_class="bridge-test",
        ops_class="ops-test",
        height="420px",
        options_json="{}",
    )

    assert "window.__jupyterTerminalRegistry" in init_js
    assert "attachCustomKeyEventHandler" in init_js
    assert 'document.addEventListener("keydown", onDocumentKeyDownCapture, true);' in init_js
    assert "term.hasSelection" in init_js
    assert 'pushEvent({ type: "interrupt" });' in init_js
    assert 'bridgeRoot.querySelector("input")' in init_js
    assert 'opsRoot.querySelector("input")' in init_js
    assert 'const opsInterval = window.setInterval(pollOps, 30);' in init_js
    assert 'pushEvent({ type: "ack", ack_seq: payloadSeq });' in init_js


def test_terminal_widget_backend_handles_interrupt_messages():
    source = Path("jupyter_terminal.py").read_text(encoding="utf-8")

    assert 'if msg_type == "interrupt":' in source
    assert "self._session.interrupt()" in source


def test_jupyter_terminal_pushes_initial_shell_output_into_ops_bridge(monkeypatch):
    class _DummyLoop:
        @staticmethod
        def add_callback(func):
            func()

    class _DummyKernel:
        io_loop = _DummyLoop()

    class _DummyIP:
        kernel = _DummyKernel()

    displayed = []

    monkeypatch.setattr(jupyter_terminal, "get_ipython", lambda: _DummyIP())
    monkeypatch.setattr(jupyter_terminal, "display", lambda *args, **kwargs: displayed.append((args, kwargs)))

    term = JupyterTerminal(height=320)
    term.display()
    time.sleep(1.0)

    payload = json.loads(term._ops_widget.value)
    write_ops = [op for op in payload["ops"] if op["op"] == "write"]

    assert payload["seq"] >= 1
    assert write_ops
    assert any(op["data"] for op in write_ops)

    term.close()


def _drain(source: queue.Queue, sink: List[str]) -> List[str]:
    drained = []
    while True:
        try:
            chunk = source.get_nowait()
        except queue.Empty:
            break
        drained.append(chunk)
        sink.append(chunk)
    return drained

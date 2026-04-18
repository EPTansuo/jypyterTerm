# AGENTS

## Test Policy

- `JupyterTerminal` 的测试不能只看“控件出现了”或者“有光标在闪”。
- 通过标准必须包含“终端区域里出现了实际输出”，例如 shell prompt 或命令回显。
- 仅有黑色终端窗口和闪烁光标，不算测试通过。

## Backend Tests

- 默认回归命令：

```bash
pytest -q
```

- 这条命令当前覆盖：
  - PTY shell 启动与输出
  - resize 后 `stty size` 更新
  - `SIGINT` 中断恢复
  - 前端桥接脚本生成
  - `JupyterTerminal` 启动后，初始 shell 输出会被推进前端 bridge

## Python 3.6 Compatibility

- 兼容性回归命令：

```bash
conda run -n jqdata-py36 pytest -q
```

- 当前已验证结果：
  - `Python 3.6.15`
  - `6 passed`

## Notebook Frontend Validation

- 对前端改动，除了 `pytest`，还要做一次真实 notebook/JupyterLab 验证。
- 建议步骤：

```python
import importlib
import jupyter_terminal

importlib.reload(jupyter_terminal)
from jupyter_terminal import JupyterTerminal

term = JupyterTerminal(height=520)
term.display()
```

- 验证点：
  - 终端区域能显示 prompt 或其他实际 shell 输出
  - 输入 `pwd`、`echo hello` 后有回显
  - `Ctrl-C` 或 `Interrupt` 按钮能中断当前命令

## Local Browser Integration Check

- 本地可以起一个临时 JupyterLab 做联调：

```bash
jupyter lab --no-browser --ServerApp.token='' --ServerApp.password='' --ServerApp.disable_check_xsrf=True --port=8891
```

- 如需自动化，可用 `selenium + chromium + chromedriver` 打开测试 notebook。
- 自动化检查时，判定标准仍然是“终端中出现实际文字输出”，不是只检查 DOM 是否创建成功。

## Notes

- 这个项目当前工作树里经常会有 `jupyter_terminal_demo.ipynb` 的本地改动；提交时不要默认带上。
- 如果前端看起来“只有光标没有输出”，优先怀疑前后端 bridge，而不是 PTY 本身。纯 Python 侧可以直接验证 `TerminalSession` 是否已经收到了 prompt 输出。

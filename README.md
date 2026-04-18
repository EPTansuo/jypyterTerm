# Jupyter Terminal

一个在 Jupyter Notebook / JupyterLab 中运行的 PTY 终端组件。

它不是简单的“输入框 + `subprocess` 输出”，而是：

- 后端使用伪终端 `PTY`
- 前端使用 `xterm.js`
- 通过 widget custom message 与内核通信

因此能更接近真实终端行为，包括：

- shell prompt
- ANSI 颜色和光标控制
- `Ctrl+C`
- Tab 补全
- `vim`、`less`、`top` 这类 TTY 程序
- 窗口 resize

## 文件

- `jupyter_terminal.py`: 终端组件实现
- `jupyter_terminal_demo.ipynb`: 可直接打开运行的示例 notebook
- `test_jupyter_terminal.py`: 后端 PTY 会话测试
- `vendor/xterm/`: 本地前端资源

## 环境

当前实现面向 POSIX 环境：

- Linux
- macOS

需要：

- Python
- Jupyter Notebook 或 JupyterLab
- `ipywidgets`
- `anywidget`

已验证的环境：

- 本机 Python 3.14
- Python 3.6

## 快速开始

在 notebook 中运行：

```python
from jupyter_terminal import JupyterTerminal

term = JupyterTerminal(height=520)
term.display();
```

如果修改过模块并希望在当前内核里重新加载：

```python
import importlib
import jupyter_terminal

importlib.reload(jupyter_terminal)
from jupyter_terminal import JupyterTerminal
```

## 运行示例

直接打开：

- `jupyter_terminal_demo.ipynb`

按顺序运行前两个代码单元即可。

## 测试

```bash
pytest -q
```

在 Python 3.6 环境中测试：

```bash
conda run -n <your-py36-env> pytest -q
```

执行示例 notebook：

```bash
conda run -n <your-py36-env> jupyter nbconvert --to notebook --execute jupyter_terminal_demo.ipynb --output /tmp/jupyter_terminal_demo.py36.ipynb
```

当前测试覆盖：

- 交互 shell 启动与输出
- `stty size` 随窗口 resize 更新
- `SIGINT` 中断后恢复控制

## 已知限制

- 当前后端仅支持 POSIX，不支持 Windows `ConPTY`
- 前端资源已本地化，但仍依赖 notebook 前端正确加载 `ipywidgets` / `anywidget`
- 支持 Python 3.6，但需要保留 Python 3.6 兼容语法

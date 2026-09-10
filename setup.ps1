$ErrorActionPreference = "Stop"

Write-Host "[1/3] 使用 Python 3.12 创建项目虚拟环境 .venv"
py -3.12 -m venv .venv

Write-Host "[2/3] 升级 pip"
& .\.venv\Scripts\python.exe -m pip install --upgrade pip

Write-Host "[3/3] 安装项目依赖"
& .\.venv\Scripts\python.exe -m pip install -r requirements.txt

Write-Host "完成。请在 PyCharm 中选择 .venv\Scripts\python.exe，然后运行 main.py。"


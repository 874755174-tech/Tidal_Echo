@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0.."

echo.
echo  ====================================================
echo    CoT 体检  -  双击就能跑，不用打任何命令
echo  ====================================================
echo.
echo  这会做三件事（全程只读，不会改你家 Kael 的任何数据）：
echo    1. 确认房子还在、密钥能用
echo    2. 给你选一个模型
echo    3. 问中转站一句话，把它原话抄回来
echo.
echo  它会自动弹一个记事本，你把 RELAY_SECRET 粘进去、
echo  Ctrl+S 保存、关掉记事本，剩下全自动。
echo.
pause

set "PY=%~dp0..\.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=%~dp0..\.venv\Scripts\python3.exe"
if not exist "%PY%" (
  echo  [x] 找不到虚拟环境 .venv，请先按 README 建好环境。
  echo      预期路径: %~dp0..\.venv\Scripts\python.exe
  pause
  exit /b 2
)

"%PY%" "%~dp0cot_doctor_file.py"
echo.
echo  ====================================================
echo   跑完了。把上面输出的内容贴给 bunny 就行。
echo  ====================================================
pause

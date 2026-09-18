@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0.."

echo ====================================
echo  icourse 语音课件同步
echo  目录：%CD%
echo ====================================
echo.

rem ---- 定位 Python：优先挑「装了 browser_cookie3」的 ----
rem browser_cookie3 能直接从浏览器读 Cookie，走纯 HTTP 同步（快、不占浏览器）。
rem 一个都没装也能跑，会自动退回 bsk 路线或 cookie.txt 路线。
set "PY="
for %%C in (py python python3) do (
  if not defined PY (
    %%C -c "import browser_cookie3" >nul 2>&1 && set "PY=%%C"
  )
)
if not defined PY (
  for %%C in (py python python3) do (
    if not defined PY (
      %%C -c "import sys" >nul 2>&1 && set "PY=%%C"
    )
  )
)
if not defined PY (
  echo [错误] 没有找到 Python。
  echo.
  echo 请先安装 Python 3.9 或更高版本：https://www.python.org/downloads/
  echo 安装时务必勾选 "Add python.exe to PATH"，装完重开这个窗口。
  echo.
  pause
  exit /b 1
)

echo Python：%PY%
echo.
%PY% src\icc_sync.py %*
set "CODE=%ERRORLEVEL%"

echo.
if "%CODE%"=="0" (
  echo 完成。
) else (
  echo 退出码 %CODE%（出错了）。
  echo.
  echo 如果提示登录态无效，先跑一次现场登录：
  echo    %PY% src\icc_sync.py --login
)
echo.
echo 按任意键关闭窗口 ...
pause >nul

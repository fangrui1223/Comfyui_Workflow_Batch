@echo off
setlocal
cd /d "%~dp0"

set "COMFY_PY=C:\FR_comfyui\python\python.exe"

if exist "%COMFY_PY%" goto run_comfy

echo Default ComfyUI Python was not found:
echo %COMFY_PY%
echo.
echo The tool will try the Windows Python launcher now.
echo If this fails, edit COMFY_PY in this bat file.
echo.
where py >nul 2>nul
if errorlevel 1 goto no_python
py app.py
goto end

:run_comfy
"%COMFY_PY%" app.py
goto end

:no_python
echo Python launcher was not found.
echo Edit COMFY_PY in this bat file to match this computer.

:end
pause

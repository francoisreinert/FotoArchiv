@echo off
rem FotoArchiv starten (Windows). Beim ersten Start auf einem Rechner wird die
rem Laufzeitumgebung einmalig nach %%LOCALAPPDATA%%\FotoArchiv entpackt (ca. 1 Minute).
setlocal
set "HERE=%~dp0"
if not exist "%HERE%runtime\python-win-x64.tar.gz" (
  echo.
  echo FotoArchiv wurde nicht vollstaendig entpackt.
  echo Bitte die ZIP-Datei nicht nur oeffnen, sondern zuerst entpacken:
  echo Rechtsklick auf FotoArchiv.zip, "Alle extrahieren ..." waehlen und dann
  echo im entpackten Ordner "FotoArchiv starten (Windows).cmd" doppelklicken.
  echo.
  pause
  exit /b 1
)
set /p RTVER=<"%HERE%runtime\VERSION"
set "RT=%LOCALAPPDATA%\FotoArchiv\runtime-%RTVER%"
set "PY=%RT%\python\python.exe"
if not exist "%RT%\ok" (
  echo FotoArchiv wird auf diesem Computer eingerichtet, bitte warten ...
  if exist "%RT%" rmdir /s /q "%RT%"
  mkdir "%RT%"
  "%SystemRoot%\System32\tar.exe" -xzf "%HERE%runtime\python-win-x64.tar.gz" -C "%RT%" || goto fail
  "%PY%" -m pip install --no-index --no-warn-script-location --disable-pip-version-check -q --find-links "%HERE%runtime\wheels-win-x64" pillow pillow-heif numpy opencv-python-headless rawpy av icloudpd samsungtvws segno onnxruntime || goto fail
  echo ok>"%RT%\ok"
)
"%PY%" "%HERE%app\server.py" %*
if errorlevel 1 pause
exit /b
:fail
echo Einrichtung fehlgeschlagen.
pause

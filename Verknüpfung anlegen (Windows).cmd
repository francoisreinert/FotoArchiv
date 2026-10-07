@echo off
rem Legt FotoArchiv mit Icon auf den Desktop und ins Startmenue.
setlocal
set "FA_HERE=%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -Command "$h=$env:FA_HERE; $ws=New-Object -ComObject WScript.Shell; foreach($d in @([Environment]::GetFolderPath('Desktop'), [Environment]::GetFolderPath('Programs'))){ $l=$ws.CreateShortcut((Join-Path $d 'FotoArchiv.lnk')); $l.TargetPath=(Join-Path $h 'FotoArchiv starten (Windows).cmd'); $l.WorkingDirectory=$h; $l.IconLocation=(Join-Path $h 'FotoArchiv.ico'); $l.WindowStyle=7; $l.Description='FotoArchiv - Fotos verwalten'; $l.Save() }" || goto fail
echo.
echo FotoArchiv liegt jetzt auf dem Desktop und im Startmenue.
echo Wichtig: Diesen Ordner danach nicht mehr verschieben (sonst die Verknuepfung neu anlegen).
pause
exit /b
:fail
echo Verknuepfung konnte nicht angelegt werden.
pause

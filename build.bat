@echo off
REM ArdenTrack — build dist\ardentrack.exe via PyInstaller
REM Run from the repo root: build.bat

echo === Installing PyInstaller (if needed) ===
pip install pyinstaller >nul 2>&1

echo === Building ardentrack.exe ===
pyinstaller ^
    --onefile ^
    --name ardentrack ^
    --hidden-import=win32timezone ^
    --hidden-import=keyring.backends.Windows ^
    --hidden-import=dotenv ^
    --hidden-import=supabase ^
    --hidden-import=postgrest ^
    --hidden-import=gotrue ^
    --hidden-import=storage3 ^
    --hidden-import=realtime ^
    --hidden-import=supafunc ^
    --hidden-import=httpx ^
    --hidden-import=hpack ^
    --hidden-import=h2 ^
    --hidden-import=httpcore ^
    --hidden-import=watchdog.observers ^
    --hidden-import=watchdog.events ^
    ardentrack/main.py

if %ERRORLEVEL% NEQ 0 (
    echo === BUILD FAILED ===
    exit /b 1
)

echo === Build complete: dist\ardentrack.exe ===
dir dist\ardentrack.exe

if defined WIN_SIGN_CERT_SHA1 (
    echo === Signing ardentrack.exe with certificate %WIN_SIGN_CERT_SHA1% ===
    signtool sign /sha1 %WIN_SIGN_CERT_SHA1% /fd sha256 /tr http://timestamp.digicert.com /td sha256 /d "ArdenTrack" dist\ardentrack.exe
    if %ERRORLEVEL% NEQ 0 (
        echo === SIGNING FAILED ===
        exit /b 1
    )
    echo === Signing complete ===
) else (
    echo === Skipping signing (WIN_SIGN_CERT_SHA1 not set) ===
)

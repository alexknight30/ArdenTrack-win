@echo off
REM ArdenTrack — full local build, sign, and release.
REM
REM Prerequisites:
REM   - YubiKey plugged in with code signing cert installed
REM   - Set WIN_SIGN_CERT_SHA1 to your certificate thumbprint
REM   - Node.js and npm installed
REM   - GH_TOKEN set (GitHub personal access token with repo scope)
REM
REM Usage:
REM   set WIN_SIGN_CERT_SHA1=YOUR_CERT_THUMBPRINT
REM   set GH_TOKEN=ghp_YOUR_TOKEN
REM   release.bat v1.0.0

if "%1"=="" (
    echo Usage: release.bat ^<version-tag^>
    echo Example: release.bat v1.0.0
    exit /b 1
)

set VERSION=%1

echo.
echo ========================================
echo  ArdenTrack Release: %VERSION%
echo ========================================
echo.

REM Step 1: Build Python exe
echo [1/5] Building ardentrack.exe...
call build.bat
if %ERRORLEVEL% NEQ 0 exit /b 1

REM Step 2: Install Electron deps
echo.
echo [2/5] Installing Electron dependencies...
cd ardentrack-electron
call npm install
if %ERRORLEVEL% NEQ 0 (
    cd ..
    exit /b 1
)

REM Step 3: Build Electron installer (sign.js handles signing if WIN_SIGN_CERT_SHA1 is set)
echo.
echo [3/5] Building Electron installer...
call npx electron-builder --win
if %ERRORLEVEL% NEQ 0 (
    cd ..
    exit /b 1
)
cd ..

REM Step 4: Tag
echo.
echo [4/5] Creating git tag %VERSION%...
git tag %VERSION%
git push origin %VERSION%

REM Step 5: Upload to GitHub Releases
echo.
echo [5/5] Creating GitHub Release %VERSION%...
gh release create %VERSION% ^
    ardentrack-electron\dist-electron\*.exe ^
    ardentrack-electron\dist-electron\latest.yml ^
    --title "ArdenTrack %VERSION%" ^
    --notes "ArdenTrack %VERSION% release"

echo.
echo ========================================
echo  Release %VERSION% complete!
echo ========================================

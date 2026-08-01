@echo off
setlocal

set "local_file=_internal/version.txt"
set "github_url=https://raw.githubusercontent.com/mepro123/MinecraftConsoleLauncher/refs/heads/main/_internal/version"

if not exist "%local_file%" (
    echo Error: Local file %local_file% missing.
    pause
    exit /b
)

set /p local_ver=<%local_file%
for /f "delims=" %%i in ('curl -s %github_url%') do set "remote_ver=%%i"

if "%local_ver%"=="%remote_ver%" (
    echo Versions match: %local_ver%. Proceeding...
    @echo off
    cmd /k ConsoleLauncher.exe %*

) else (
    echo Version mismatch! Local: %local_ver% ^| Server: %remote_ver%
    start "" "https://github.com/mepro123/MinecraftConsoleLauncher"
    pause
    exit /b
)

:proceed
:: Your main code goes below here
echo Running the main program...


pause

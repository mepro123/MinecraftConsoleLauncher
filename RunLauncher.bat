@echo off

set /p VERSION=<_internal\version.txt

echo Version found: [%VERSION%]

if "%VERSION%"=="1.3" (
	@echo off
	cmd /k ConsoleLauncher.exe %*
) else (
    echo Outdated version. Opening download page...
    start "" "https://github.com/mepro123/MinecraftConsoleLauncher"
)

pause
$ProgressPreference = 'SilentlyContinue'
Set-Location "$env:USERPROFILE"
Invoke-WebRequest https://github.com/mepro123/MinecraftConsoleLauncher/archive/refs/heads/main.zip -OutFile cl.zip
Expand-Archive cl.zip -DestinationPath . -Force
Remove-Item cl.zip
New-Item -ItemType Directory -Force -Path "$env:USERPROFILE\bin" | Out-Null
@"
@echo off
if "%1"=="uninstall" (
    rmdir /s /q "%USERPROFILE%\MinecraftConsoleLauncher-main"
    del "%~f0"
    echo Uninstalled.
    exit /b
)
cd /d "$env:USERPROFILE\MinecraftConsoleLauncher-main"
call RunLauncher.bat
"@ | Set-Content "$env:USERPROFILE\bin\ConsoleLauncher.bat"
[Environment]::SetEnvironmentVariable("PATH", "$([Environment]::GetEnvironmentVariable('PATH','User'));$env:USERPROFILE\bin", "User")
Write-Host "Done! Close and reopen a terminal, then type ConsoleLauncher"
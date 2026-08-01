# <img width="32" height="32" alt="Image" src="https://github.com/user-attachments/assets/b15e94ad-3b24-46e8-a4de-9858cf4795aa" /> MINECRAFT CONSOLE LAUNCHER 1.4 (RESOURCES PACK, MIGRATION UPDATE)

## Information

1. **Windows Defender** might flag the program, but this is usually a false positive because it is written in Python.
2. **Always run the `.bat` file first** if you're using the EXE version.
3. **GitHub is the official download source** for this program.
4. **You can locate game files in**  `_internal\mc_data\` (EXE version)
5. **You can use** `ConsoleLauncher uninstall` if you install with Powershell.
6. **If you use browse** and it is taking too long to browse, this might be a problem with [modrinth api](https://status.modrinth.com).
7. **Migrating always** find the latest version, some mods might be incompatible with each other.

## Installation

1. Open Powershell
2. Execute this command in Powershell
```js
irm https://raw.githubusercontent.com/mepro123/MinecraftConsoleLauncher/refs/heads/main/install.ps1 | %{$_ -replace "^\uFEFF",""} | iex
```

## Update History

- **1.0** First release
- **1.1** Added Snapshot lists
- **1.1.1** EXE version
- **1.2** Added browsing mods
- **1.3** Added shaders, mods, modpacks browsing and added better deleting
- **1.4** Added resources pack and migrating 

[![Last Updated](https://img.shields.io/badge/Click%20to%20know%20how%20to%20use-blue)](https://docs.google.com/videos/d/1WimxN96kx3n_mTjwaau1BcN7g7ZOr1O_nVc-VBKGzTA/play)

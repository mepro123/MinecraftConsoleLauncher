# MINECRAFT CONSOLE LAUNCHER 1.0

## How to use

[[video]]()

[![Watch the video](https://www.youtube.com/watch?v=N4zjIszoNUc)](https://www.youtube.com/watch?v=N4zjIszoNUc)


1) See what versions exist:
     python launcher.py list
     python launcher.py list --all          (include snapshots)

2) Install a version as a named installation:
     python launcher.py install 1.21.11 --name Vanilla
     python launcher.py install 1.21.11 --loader fabric --name MyFabric
     python launcher.py install 1.21.11 --loader forge --name MyForge
   (--name is optional; if you skip it, the version id itself is used as
   the name)

3) See what's installed:
     python launcher.py installed

4) Launch it:
     python launcher.py launch MyFabric --username Steve
   The first time, --username is required and gets saved. RAM defaults
   to 4096MB (4GB) automatically on first launch. After that, just:
     python launcher.py launch MyFabric
   and it reuses the saved username/RAM -- no flags needed.

   To change RAM or username later, either pass the flag again on launch
   (it updates the saved value), or use:
     python launcher.py config MyFabric --ram 6144
     python launcher.py config MyFabric --username NewName
     python launcher.py config MyFabric              (just view current settings)

5) Install mods (Fabric/Forge installations only) from a Modrinth or
   CurseForge mod page URL:
     python launcher.py mods add MyFabric https://modrinth.com/mod/sodium
     python launcher.py mods add MyFabric https://www.curseforge.com/minecraft/mc-mods/jei
     python launcher.py mods list MyFabric
     python launcher.py mods remove MyFabric sodium-fabric-0.5.8.jar

6) Delete an installation you don't want anymore (asks for confirmation):
     python launcher.py delete MyFabric
     python launcher.py delete MyFabric --yes     (skip the confirmation prompt)
   This removes that installation's saves/mods/settings, but keeps the
   shared downloaded game files (versions/libraries/assets) since other
   installations may still be using them.

7) Install a full modpack (Minecraft version + loader + all mods +
   configs, all in one go) as a brand new named installation:
     python launcher.py modpack install https://modrinth.com/modpack/fabulously-optimized --name MyPack
   By default it grabs the latest version. To pick a specific one:
     python launcher.py modpack versions https://modrinth.com/modpack/fabulously-optimized
     python launcher.py modpack install https://modrinth.com/modpack/fabulously-optimized --version 6.4.2 --name MyPack
   This works for Modrinth modpacks (.mrpack). CurseForge modpacks aren't
   supported -- resolving their individual mod files needs a paid
   CurseForge developer API key. If a pack (or a close equivalent) is
   also on Modrinth, use that link instead.

Each named installation has its own isolated saves/mods/config folder
under mc_data/instances/<name>/game/, so different modpacks never
collide with each other.

Notes:
  - Java is auto-detected, and auto-downloaded (a portable Eclipse
    Temurin JRE) if nothing suitable is found -- no manual setup needed.
  - CurseForge mod installs use an unofficial mirror API (CurseForge's
    real API needs a developer key we don't have). Modrinth is more
    reliable if a CurseForge link ever fails.
  - Pass --java "C:\path\to\java.exe" to any install/launch command to
    force a specific Java install instead of auto-detecting.

[Install now (.zip)](https://codeload.github.com/mepro123/MinecraftConsoleLauncher/zip/refs/heads/main)


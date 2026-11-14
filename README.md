# 🎮 Game Launcher

Ein eigener Spiele-Launcher für Windows im Stil von Steam/Epic, für Spiele, die als Ordner auf der Festplatte liegen.

![Logo](launcher.png)

## Features

- **Bibliothek mit Covern**: Spiele als Kacheln mit offiziellem Steam-Cover (wird automatisch geladen)
- **Automatische Erkennung**: Überwachte Ordner werden alle 30 Sekunden geprüft, neue Spiele kommen von selbst dazu (erst wenn Entpacken/Download fertig ist)
- **Co-op-Mods**: Seamless Co-op, Mod Engine usw. werden erkannt und automatisch über deren Launcher gestartet
- **Mod-Updates**: Prüft die offiziellen GitHub-Releases von Seamless Co-op (Elden Ring, Nightreign, Dark Souls 1–3) und aktualisiert per Knopfdruck, mit Sicherung und ohne die eigenen Einstellungen zu überschreiben
- **Platz sparen**: 🗑 packt ein Spiel in ein Archiv und löscht den Ordner, mit „Installieren“ kommt es zurück. Alternativ Windows-Komprimierung (bleibt spielbar)
- **Spielzeit & „zuletzt gespielt“**, Suche, Sortierung
- **Desktop-Import**: Verknüpfungen übernehmen und vom Desktop aufräumen

## Installation

1. [Python 3](https://www.python.org/downloads/) installieren
2. `pip install -r requirements.txt`
3. `GameLauncher.pyw` per Doppelklick starten

Optional: `python make_icon.py` erzeugt das Logo (`launcher.ico` / `launcher.png`) neu.

Für Archivieren/Installieren wird [WinRAR](https://www.win-rar.com/) benötigt.

## Daten

Die Bibliothek wird unter `%APPDATA%\GameLauncher\` gespeichert (`library.json`, `settings.json`, Cover).

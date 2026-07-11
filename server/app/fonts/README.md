# Fonts

Drop `.ttf` files here to enable the alert font numbers. Until a file is present, that
font number falls back to the built-in mono font (2), so alerts always render.

Expected filenames (rename the download to match):

| # | Font | File | Download |
|---|------|------|----------|
| 1 | *(built-in mono — default, no file needed)* | — | ships with the app |
| 2 | Jersey 10 | `Jersey10-Regular.ttf` | https://fonts.google.com/specimen/Jersey+10 |
| 3 | Jacquard 12 | `Jacquard12-Regular.ttf` | https://fonts.google.com/specimen/Jacquard+12 |
| 4 | Doto | `Doto-Regular.ttf` | https://fonts.google.com/specimen/Doto |

Doto is a variable font; either export a static "Regular" instance named `Doto-Regular.ttf`,
or set `FONT_PATH`-style paths in `_ALERT_FONT_FILES` if you keep the variable file.

All three are open-source (SIL Open Font License), so bundling the TTFs is fine.

The base (non-mono) `DejaVuSansMono.ttf` for font 2 can also live here — see the parent
README's Fonts section.

## Android

The same files also go in `android/app/src/main/assets/fonts/` for device-direct alerts.

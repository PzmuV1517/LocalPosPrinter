# Alert fonts (device-direct)

Drop the same TTFs here that the server uses, so alerts printed directly on the device
(via the device's own /print) match. Until a file is present, that font number falls
back to the built-in mono font (2).

| # | Font | File |
|---|------|------|
| 1 | built-in mono (default) | *(none)* |
| 2 | Jersey 10 | `Jersey10-Regular.ttf` |
| 3 | Jacquard 12 | `Jacquard12-Regular.ttf` |
| 4 | Doto | `Doto-Regular.ttf` |

Download from Google Fonts (all OFL-licensed):
- https://fonts.google.com/specimen/Jersey+10
- https://fonts.google.com/specimen/Jacquard+12
- https://fonts.google.com/specimen/Doto

These are loaded in `PrintHubApp` via `ReceiptRenderer.loadFonts(this)`.

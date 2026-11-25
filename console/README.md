# KeyUp Dispatch Console (Windows)

KeyUp's own dispatch console for Windows 10 and 11 (x64).

**What it does**
- **Monitors talkgroups:** shows many talkgroups at once, grouped into your zones, and plays them through the PC speaker or headset.
- **Talks:** transmits as its radio ID, with a hold-to-talk button, a hotkey, or each tile's own PTT.
- **Shows activity:** who is talking on each channel, plus a call and event log.
- **Handles emergencies:** an emergency banner with tones and a clear button.
- **Inhibits radios:** if the console is allowed to.
- **Patches talkgroups:** if the console is allowed to.

**Connecting.** The console has WireGuard built in: it reaches KeyUp through its own tunnel, and falls back to HTTPS when the tunnel isn't available.

## Install

1. Download `KeyUp-Dispatch-Console-Setup-<version>.exe` from the repository's Releases page (tags `console-v*`), or from the latest **Console · Windows build** run in GitHub Actions. That run also has a portable zip.
2. Run the setup. It installs the console and the **KeyUp Tunnel** service, which owns the console's WireGuard adapter so the console itself runs without administrator rights. The console is self-contained, so no .NET install is needed. Windows SmartScreen may warn because the program isn't code-signed: choose **More info → Run anyway**.
3. In the KeyUp web UI, open **Dispatch Consoles**, add a console (pick the radio ID it transmits as and, optionally, its zones), and press **Activate**.
4. Start the console, choose **Connect to KeyUp…**, and enter the server address and the code.

The console then brings up its tunnel. Only traffic to the KeyUp server goes through it; the rest of the PC's traffic is unaffected. It connects through the tunnel and, if the tunnel isn't up, over HTTPS. The header shows which transport is in use.

## Use

| | |
|---|---|
| **Zones** | One tab per zone. Each tile is a talkgroup. |
| **Monitor** | Hear the talkgroup. Its volume slider sets how loud. |
| **Select** (or click the tile) | The talkgroup the big PTT button and the PTT key use. |
| **PTT** on a tile | Hold to talk on that talkgroup straight away. |
| **PTT key** | Space by default (**Settings…**). Hold to talk on the selected talkgroup. |
| **Inhibit…** | Inhibit or uninhibit a radio ID system-wide (radios, phones and consoles using it). |
| **Patch…** | Link two talkgroups, or unlink them. |
| **Emergency bar** | Appears on an emergency, with tones. **Acknowledge / clear** clears it everywhere. |

A busy talkgroup gives a deny tone. A console with **call priority** talks over radios, but not over emergencies.

## Files

| Where | What |
|---|---|
| `%APPDATA%\KeyUp Dispatch Console\console.json` | Your settings. The login token and tunnel key are encrypted with Windows DPAPI for your user. |
| `%ProgramData%\KeyUp Dispatch Console\tunnel.json` | The tunnel the console asks the service for. The private key is encrypted (DPAPI, this PC). |
| `%ProgramData%\KeyUp Dispatch Console\tunnel-status.json` | The service's answer (up / error). |

To move a console to another PC, press **Activate** again in the web UI. The old PC's login and tunnel stop working. **Revoke** cuts a console off at once.

## Build

GitHub Actions builds it on every change under `console/` (**Console · Windows build**). To build locally on Windows with the .NET 8 SDK and Inno Setup 6:

```powershell
# official WireGuardNT DLL, shipped unmodified next to the program
Invoke-WebRequest https://download.wireguard.com/wireguard-nt/wireguard-nt-0.10.1.zip -OutFile wgnt.zip
Expand-Archive wgnt.zip wgnt; Copy-Item wgnt/wireguard-nt/bin/amd64/wireguard.dll console/src/KeyUp.Console/
dotnet publish console/src/KeyUp.Console/KeyUp.Console.csproj -c Release -r win-x64 --self-contained true -o publish
& "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe" "/DPublishDir=$pwd\publish" console/installer/KeyUpConsole.iss
```

- `src/KeyUp.Console.Core` holds the protocol client, G.711, the mixer, activation, settings and the WireGuard configuration. It has no Windows dependencies, so `tests/HeadlessCheck` runs it against a live server; KeyUp's feature lab does that on every CI run.
- `src/KeyUp.Console` holds the WPF window, WinMM audio and the KeyUp Tunnel service.

## License

GPL-2.0, like the rest of KeyUp (`LICENSE.txt`). `wireguard.dll` is WireGuardNT by WireGuard LLC, redistributed unmodified under its own license (`WIREGUARD-NT-LICENSE.txt` in the install folder).

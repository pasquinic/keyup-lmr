# KeyUp Dispatch Console (Windows)

A dispatch console for KeyUp: monitor and talk on many talkgroups from a Windows PC, page, radio-check and inhibit radios, and see who's talking. It is KeyUp's fork of the [DVMProject Desktop Dispatch Console](https://github.com/DVMProject/dvmconsole) (`FORK.md`), and it joins the FNE as its own peer.

The console needs the **DVM backend**, the default in KeyUp. It transmits as one subscriber radio ID and follows that radio's rules:
- it gets the talkgroups that radio ID may use (restricted talkgroups only if the ID lists them), optionally narrowed per console;
- the FNE's radio ID list applies, so inhibiting the radio ID stops the console transmitting;
- with **call priority** set, the FNE lets the console talk over radios;
- with **may inhibit** set, the console can inhibit and uninhibit radios.

## Install

1. Download `KeyUp-Dispatch-Console-win-x86.zip`. It's on the repository's GitHub Releases page (tags `console-v*`), or under the **Console · Windows build** workflow run in GitHub Actions.
2. Unzip it anywhere, for example `C:\KeyUp Dispatch Console`, and run `DVMConsole.exe`. Windows 10 or 11 is required; .NET is bundled. Windows SmartScreen may warn because the program isn't code-signed: choose **More info > Run anyway**.
3. Install [WireGuard for Windows](https://www.wireguard.com/install/).

## Connect to KeyUp

1. In the KeyUp web UI open **Dispatch Consoles** and add a console. Pick the radio ID it transmits as; add that ID under **Subscriber Radios** first.
2. Press **Activate** to get a one-time code. It's valid for 24 hours.
3. In the console choose **File > Connect to KeyUp Server...** and enter the server address (for example `lmr.example.com`) and the code.
4. The console saves its files and opens the folder. In WireGuard choose **Import tunnel(s) from file**, pick `keyup-wireguard.conf` and press **Activate**. The tunnel only carries traffic to the KeyUp server; the rest of the PC's traffic is unaffected.
5. The console logs into the FNE. It shows as online on the **Dispatch Consoles** page, with its FNE peer ID.

Talkgroups and radio aliases come from KeyUp. The console fetches them on every start, or immediately with **File > Sync with KeyUp**. To move a console to another PC, press **Activate** again; the old PC's tunnel and login stop working. **Revoke** disconnects a console at once.

The console's files are in `%APPDATA%\DVMProject\dvmconsole\KeyUp` (**File > Open KeyUp Folder**):

| File | Contents |
|---|---|
| `codeplug.yml` | FNE connection and channels (do not edit; regenerated on sync) |
| `alias.yml` | Radio aliases |
| `keyup-wireguard.conf` | Tunnel for WireGuard (contains this PC's private key) |
| `keyup.json` | Server and console token |

## Audio and modes

- P25 talkgroups use the bundled software IMBE vocoder (`libvocoder.dll`).
- DMR talkgroups need a DVSI vocoder: upstream's console looks for `AMBE.DLL`, which KeyUp can't ship.
- Talkgroups set to DMR in KeyUp (Mode for SIP / PoC / analog bridges) appear as DMR channels, and all others as P25. Cross-mode patches in KeyUp make a DMR talkgroup audible on a P25 one.
- Audio devices, PTT keys, alert tones and the channel layout are set in the console's **Settings** menu, as in upstream.

## Build it yourself

GitHub Actions builds it on every push to `console/` (**Console · Windows build**). To build locally on Windows with Visual Studio 2022 and the .NET 8 SDK:

```powershell
cmake -S console/dvmvocoder -B vocoder-build -A Win32 -DCMAKE_MSVC_RUNTIME_LIBRARY=MultiThreaded
cmake --build vocoder-build --config Release
dotnet publish console/dvmconsole/DVMConsole.csproj -c Release -r win-x86 --self-contained true -o publish
copy vocoder-build\Release\libvocoder.dll publish\
```

## License

AGPL-3.0 (`LICENSE`), as upstream. `libvocoder` is GPL-2.0. Upstream's README is in `UPSTREAM_README.md`.

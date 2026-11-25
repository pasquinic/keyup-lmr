# KeyUp fork of DVMProject dvmconsole

`console/` is KeyUp's fork of the [DVMProject Desktop Dispatch Console](https://github.com/DVMProject/dvmconsole), AGPL-3.0.

| Folder | Upstream | Base commit | License |
|---|---|---|---|
| `dvmconsole/` | [DVMProject/dvmconsole](https://github.com/DVMProject/dvmconsole) | `84d80d6` | AGPL-3.0 |
| `fnecore/` | [DVMProject/fnecore](https://github.com/DVMProject/fnecore) (dvmconsole's submodule) | `d4b15e1` | AGPL-3.0 |
| `dvmvocoder/` | [DVMProject/dvmvocoder](https://github.com/DVMProject/dvmvocoder) (`libvocoder.dll`, IMBE software vocoder) | `e7ab96e` | GPL-2.0 |

Every upstream file keeps its copyright header. The console stays under the AGPL-3.0 (`LICENSE`); the rest of KeyUp is GPL-2.0. They are separate programs that talk over the network, so neither license changes the other.

## KeyUp changes

| Change | Files | Why |
|---|---|---|
| KeyUp provisioning client | `dvmconsole/KeyUpClient.cs` (new) | Trades a one-time activation code for the console's token, codeplug, radio aliases and WireGuard tunnel file, and re-syncs the codeplug later. |
| Connect dialog | `dvmconsole/KeyUpConnectWindow.cs` (new) | Asks for the KeyUp server and activation code. |
| Menu commands and start-up sync | `dvmconsole/MainWindow.KeyUp.cs` (new), one hook in `MainWindow.xaml.cs` (`MainWindow_Loaded`), three items in `MainWindow.xaml` (File menu) | File > Connect to KeyUp Server, Sync with KeyUp, Open KeyUp Folder. On start, a linked console fetches its latest codeplug (6 s at most, cached copy otherwise). |
| Branding | `MainWindow.xaml` (window title), `DVMConsole.csproj` (product, title, copyright) | Shows as "KeyUp Dispatch Console". The assembly and executable keep upstream's name (`DVMConsole.exe`) so resource paths stay intact. |
| Headless network check | `tests/PeerCheck/` (new) | Compiles the console's real network code (PeerSystem, FneSystemBase, KeyUpClient, fnecore) without the UI. KeyUp's DVM lab test uses it to activate, log into the FNE, transmit and receive. |

Everything else (channels, PTT, paging, radio check, inhibit, call history, alert tones, encryption) is upstream's console unchanged. A plain codeplug still opens with File > Open Codeplug.

## Syncing with upstream

```bash
git clone --recurse-submodules https://github.com/DVMProject/dvmconsole.git /tmp/dc
cd /tmp/dc && git diff 84d80d6 HEAD -- dvmconsole configs > /tmp/dc.patch
cd fnecore && git diff d4b15e1 HEAD > /tmp/fnecore.patch
cd <keyup-lmr>/console && git apply --3way /tmp/dc.patch && (cd fnecore && git apply --3way /tmp/fnecore.patch)
```

Then update the base commits above and rerun `tests/lab_dvm_e2e.py` (it builds `tests/PeerCheck`). The Windows build runs in GitHub Actions (`console-windows.yml`).

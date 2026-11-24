# KeyUp fork of DVMProject dvmhost

`dvm/dvmhost/` is KeyUp's fork of [DVMProject/dvmhost](https://github.com/DVMProject/dvmhost), GPL-2.0.

- **Based on:** upstream commit `435ff45b367887ffa94c89cb1a2779628e0f7bfb` (version 05.04A).
- **Not included:** the modem firmware submodules (`src/fw/*`: dvmfirmware, dvmfirmware-hs, dvmv24). Flash modems with DVMProject's firmware releases.
- **Copyright:** every upstream file keeps its original copyright header. KeyUp changes are marked `// KeyUp fork`.

## KeyUp changes

| Change | Files | Why |
|---|---|---|
| Structured call events | `src/fne/KeyUpEvents.{h,cpp}`, hooks in `src/fne/network/callhandler/Tag{P25,DMR,NXDN,Analog}Data.cpp` | When `KEYUP_EVENTS_FILE` is set, the FNE appends one JSON line per call start/end. KeyUp's dashboard, call history and PoC labelling read this instead of parsing log text. Unset, it does nothing. |
| Fork version tag | `src/common/Defines.h`, `src/common/GitHash.h` | Version strings show `keyup.1`. `__NETVER__` (the network protocol version) is unchanged, so fork and upstream peers interoperate. |

Deliberately unchanged:
- **WACN `BEE00` refusal ("error 4"):** this is an upstream safeguard. KeyUp's UI avoids that value and warns about it instead.
- **`iAgreeNotToBeStupid` acknowledgement:** this is upstream's public-safety guard. KeyUp sets it only after the operator acknowledges it on the DVM Network page.

## Syncing with upstream

```bash
git clone https://github.com/DVMProject/dvmhost.git /tmp/upstream
cd /tmp/upstream && git diff 435ff45b367887ffa94c89cb1a2779628e0f7bfb HEAD -- . ':!src/fw' > /tmp/upstream.patch
cd <keyup-lmr>/dvm/dvmhost && git apply --3way /tmp/upstream.patch
```

Resolve any conflicts in files marked `// KeyUp fork`. Then update the base commit above and `DVM_REF` in `central/app/services/dvm.py`, and rerun `tests/lab_dvm_e2e.py`.

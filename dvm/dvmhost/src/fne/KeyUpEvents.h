// SPDX-License-Identifier: GPL-2.0-only
/*
 * KeyUp fork of DVMProject dvmhost.
 * Copyright (C) 2026 KeyUp contributors
 *
 * Structured call events for the KeyUp management plane: when the environment
 * variable KEYUP_EVENTS_FILE is set, the FNE appends one JSON object per line
 * for every call start and end (all modes). KeyUp reads this file instead of
 * parsing human-readable log text. With the variable unset this is a no-op.
 */
#if !defined(__KEYUP_EVENTS_H__)
#define __KEYUP_EVENTS_H__

#include <cstdint>

namespace keyup
{
    /** @brief Append one call event (thread safe). mode: "P25", "DMR", "NXDN", "Analog"; event: "start"/"end". */
    void callEvent(const char* mode, const char* event, bool privateCall, uint32_t peerId, uint32_t srcId,
        uint32_t dstId, uint32_t durationMs, uint32_t streamId, bool fromUpstream);
} // namespace keyup

#endif // __KEYUP_EVENTS_H__

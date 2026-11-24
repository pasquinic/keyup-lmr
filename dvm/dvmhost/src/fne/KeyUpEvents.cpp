// SPDX-License-Identifier: GPL-2.0-only
/*
 * KeyUp fork of DVMProject dvmhost.
 * Copyright (C) 2026 KeyUp contributors
 */
#include "fne/KeyUpEvents.h"

#include <chrono>
#include <cstdio>
#include <cstdlib>
#include <mutex>

namespace
{
    std::mutex g_lock;
    FILE* g_file = nullptr;
    bool g_checked = false;

    FILE* eventsFile()
    {
        if (!g_checked) {
            g_checked = true;
            const char* path = ::getenv("KEYUP_EVENTS_FILE");
            if (path != nullptr && path[0] != '\0')
                g_file = ::fopen(path, "a");
        }
        return g_file;
    }
} // namespace

void keyup::callEvent(const char* mode, const char* event, bool privateCall, uint32_t peerId, uint32_t srcId,
    uint32_t dstId, uint32_t durationMs, uint32_t streamId, bool fromUpstream)
{
    std::lock_guard<std::mutex> lock(g_lock);
    FILE* fp = eventsFile();
    if (fp == nullptr)
        return;

    double ts = std::chrono::duration<double>(std::chrono::system_clock::now().time_since_epoch()).count();
    ::fprintf(fp, "{\"ts\":%.3f,\"mode\":\"%s\",\"event\":\"%s\",\"private\":%s,\"peer\":%u,\"src\":%u,\"dst\":%u,"
        "\"duration_ms\":%u,\"stream\":%u,\"upstream\":%s}\n", ts, mode, event, privateCall ? "true" : "false",
        peerId, srcId, dstId, durationMs, streamId, fromUpstream ? "true" : "false");
    ::fflush(fp);
}

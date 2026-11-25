// SPDX-License-Identifier: GPL-2.0-only
using System.Windows;
using System.Windows.Media;
using KeyUp.Console.Core;

namespace KeyUp.Console;

/// <summary>A horizontal segmented VU (peak) meter, -60..0 dBFS: green, then amber from -12 dB, red from -3 dB.
/// Feed it with <see cref="Push"/> about 20 times a second; it falls back smoothly and holds the peak briefly.</summary>
public sealed class VuMeter : FrameworkElement
{
    private const int Segments = 24;
    private double shown = -60, hold = -60;
    private DateTime holdUntil;
    private static readonly Brush Off = Theme.Brush("#1A232E"), Green = Theme.Brush("#2FD18B"),
        Amber = Theme.Brush("#FFB020"), Red = Theme.Brush("#FF5468"), HoldBrush = Theme.Brush("#E6EDF3");

    public VuMeter() { Height = 8; SnapsToDevicePixels = true; }

    /// <summary>Peak level 0..1 since the last push.</summary>
    public void Push(double level)
    {
        double db = Levels.ToDb(level);
        shown = db > shown ? db : Math.Max(db, shown - 3);        // fast attack, ~60 dB/s release at 20 Hz
        if (db >= hold || DateTime.UtcNow > holdUntil) { hold = db; holdUntil = DateTime.UtcNow.AddMilliseconds(800); }
        InvalidateVisual();
    }

    public void Reset() { shown = hold = -60; InvalidateVisual(); }

    protected override void OnRender(DrawingContext dc)
    {
        double w = ActualWidth, h = ActualHeight;
        if (w <= 0 || h <= 0) return;
        double gap = 1.5, seg = (w - gap * (Segments - 1)) / Segments;
        int lit = (int)Math.Round((shown + 60) / 60 * Segments);
        int holdSeg = (int)Math.Round((hold + 60) / 60 * Segments) - 1;
        for (int i = 0; i < Segments; i++)
        {
            double db = -60 + 60.0 * (i + 1) / Segments;
            Brush on = db > -3 ? Red : db > -12 ? Amber : Green;
            Brush b = i < lit ? on : i == holdSeg && hold > -57 ? HoldBrush : Off;
            dc.DrawRectangle(b, null, new Rect(i * (seg + gap), 0, seg, h));
        }
    }
}

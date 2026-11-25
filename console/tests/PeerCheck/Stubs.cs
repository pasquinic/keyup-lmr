// SPDX-License-Identifier: AGPL-3.0-only
// Minimal stand-ins for the console's WPF types, so its network code can run headless.
using fnecore;

namespace NAudio.Mixer { }

namespace dvmconsole
{
    public class RadioAlias
    {
        public string Alias { get; set; }
        public int Rid { get; set; }
    }

    public class SettingsManager
    {
        public static string UserAppDataPath = Path.Combine(Path.GetTempPath(), "keyup-console-check");
    }

    /// <summary>Receives what the console's FneSystemBase hands to the UI.</summary>
    public class MainWindow
    {
        public readonly object Lock = new object();
        public readonly Dictionary<uint, int> P25Frames = new Dictionary<uint, int>();
        public readonly Dictionary<uint, HashSet<uint>> P25Sources = new Dictionary<uint, HashSet<uint>>();

        public void P25DataReceived(P25DataReceivedEvent e, DateTime pktTime)
        {
            if (e.DUID != fnecore.P25.P25DUID.LDU1 && e.DUID != fnecore.P25.P25DUID.LDU2)
                return;
            lock (Lock)
            {
                P25Frames[e.DstId] = P25Frames.GetValueOrDefault(e.DstId) + 1;
                if (!P25Sources.ContainsKey(e.DstId))
                    P25Sources[e.DstId] = new HashSet<uint>();
                P25Sources[e.DstId].Add(e.SrcId);
            }
        }

        public void DMRDataReceived(DMRDataReceivedEvent e, DateTime pktTime) { }
        public void KeyResponseReceived(KeyResponseEvent e) { }
    }
}

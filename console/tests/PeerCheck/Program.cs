// SPDX-License-Identifier: AGPL-3.0-only
/**
* KeyUp Dispatch Console -- headless network check.
*
* Usage: PeerCheck <keyup-server-url> <activation-code>
* Activates like the console's File > Connect to KeyUp server, logs into the FNE with the received
* codeplug using the console's own PeerSystem, then follows commands on stdin (one JSON line out each):
*   tx <tgid> <seconds>   transmit P25 voice (silence) on a talkgroup, exactly as the console's PTT does
*   rx                    report P25 voice frames received per talkgroup since the last "rx"
*   sync                  re-fetch the codeplug (File > Sync with KeyUp)
*   quit
*/

using System.Text.Json;
using System.Text.RegularExpressions;
using fnecore;
using fnecore.P25;

namespace dvmconsole
{
    public static class Program
    {
        // IMBE silence, as dvmhost uses for null audio
        private static readonly byte[] NULL_IMBE = { 0x04, 0x0C, 0xFD, 0x7B, 0xFB, 0x7D, 0xF2, 0x7B, 0x3D, 0x9E, 0x45 };
        private static readonly int[] IMBE_OFFSETS = { 10, 26, 55, 80, 105, 130, 155, 180, 204 };

        private static void Emit(object o)
        {
            Console.Out.WriteLine(JsonSerializer.Serialize(o));
            Console.Out.Flush();
        }

        private static string Field(string yaml, string key)
        {
            Match m = Regex.Match(yaml, @"^\s*" + key + @":\s*(.+?)\s*$", RegexOptions.Multiline);
            return m.Success ? m.Groups[1].Value.Trim('"', '\'') : null;
        }

        public static async Task<int> Main(string[] args)
        {
            if (args.Length < 2)
            {
                Console.Error.WriteLine("usage: PeerCheck <server> <activation-code>");
                return 2;
            }

            KeyUpClient.ProfileDir = Path.Combine(Path.GetTempPath(), "keyup-console-check-" + Environment.ProcessId);
            KeyUpProfile profile = await KeyUpClient.ActivateAsync(args[0], args[1], "peercheck");
            string yaml = File.ReadAllText(KeyUpClient.CodeplugPath);
            List<string> tgs = Regex.Matches(yaml, @"^\s*tgid:\s*""?(\d+)""?", RegexOptions.Multiline).Select(m => m.Groups[1].Value).ToList();
            string aliasPath = Field(yaml, "aliasPath");
            Emit(new
            {
                @event = "activated",
                console = profile.ConsoleId,
                tunnel_ip = profile.TunnelIp,
                fne = profile.Fne,
                talkgroups = tgs,
                alias_ok = aliasPath == KeyUpClient.AliasPath && File.Exists(aliasPath),
                wireguard_ok = File.ReadAllText(KeyUpClient.WireGuardPath).Contains("[Peer]"),
            });

            Codeplug.System system = new Codeplug.System
            {
                Name = "KeyUp",
                Identity = Field(yaml, "identity"),
                Address = Field(yaml, "address"),
                Port = int.Parse(Field(yaml, "port")),
                PeerId = uint.Parse(Field(yaml, "peerId")),
                Rid = Field(yaml, "rid"),
                Password = Field(yaml, "password"),
                Encrypted = false,
            };

            MainWindow ui = new MainWindow();
            PeerSystem fne = new PeerSystem(ui, system);
            TaskCompletionSource<bool> connected = new TaskCompletionSource<bool>();
            fne.peer.PeerConnected += (s, e) => connected.TrySetResult(true);
            fne.Start();
            if (await Task.WhenAny(connected.Task, Task.Delay(20000)) != connected.Task)
            {
                Emit(new { @event = "error", message = "FNE login timed out" });
                return 1;
            }
            Emit(new { @event = "connected", peer = system.PeerId, rid = system.Rid });

            string line;
            while ((line = Console.In.ReadLine()) != null)
            {
                string[] cmd = line.Trim().Split(' ', StringSplitOptions.RemoveEmptyEntries);
                if (cmd.Length == 0)
                    continue;
                if (cmd[0] == "quit")
                    break;
                try
                {
                    await Command(cmd, ui, fne, system, profile);
                }
                catch (Exception ex)
                {
                    Emit(new { @event = "error", message = ex.GetBaseException().Message, refused = ex is KeyUpRefusedException });
                }
            }
            fne.Stop();
            return 0;
        }

        private static async Task Command(string[] cmd, MainWindow ui, PeerSystem fne, Codeplug.System system, KeyUpProfile profile)
        {
                if (cmd[0] == "rx")
                {
                    lock (ui.Lock)
                    {
                        Emit(new
                        {
                            @event = "rx",
                            frames = ui.P25Frames.ToDictionary(kv => kv.Key.ToString(), kv => kv.Value),
                            sources = ui.P25Sources.ToDictionary(kv => kv.Key.ToString(), kv => kv.Value.ToList()),
                        });
                        ui.P25Frames.Clear();
                        ui.P25Sources.Clear();
                    }
                }
                else if (cmd[0] == "sync")
                {
                    bool changed = await KeyUpClient.SyncAsync(profile);
                    string y = File.ReadAllText(KeyUpClient.CodeplugPath);
                    Emit(new { @event = "sync", changed, talkgroups = Regex.Matches(y, @"^\s*tgid:\s*""?(\d+)""?", RegexOptions.Multiline).Select(m => m.Groups[1].Value).ToList() });
                }
                else if (cmd[0] == "tx" && cmd.Length >= 3)
                {
                    int sent = await Transmit(fne, uint.Parse(system.Rid), uint.Parse(cmd[1]), double.Parse(cmd[2]));
                    Emit(new { @event = "tx_done", ldus = sent });
                }
        }

        /// <summary>
        /// Mirrors MainWindow's P25 PTT path: grant-demand TDU, LDU1/LDU2 every 180 ms, then terminator TDUs.
        /// </summary>
        private static async Task<int> Transmit(PeerSystem fne, uint srcId, uint dstId, double seconds)
        {
            uint streamId = fne.NewStreamId();
            RemoteCallData callData = new RemoteCallData { SrcId = srcId, DstId = dstId, LCO = P25Defines.LC_GROUP };
            byte[] netLDU = new byte[9 * 25];
            foreach (int off in IMBE_OFFSETS)
                Buffer.BlockCopy(NULL_IMBE, 0, netLDU, off, NULL_IMBE.Length);

            fne.SendP25TDU(srcId, dstId, true);
            ushort pktSeq = 0;
            int ldus = (int)(seconds / 0.18);
            for (int n = 0; n < ldus; n++)
            {
                byte[] payload = new byte[200];
                if (n % 2 == 0)
                {
                    fne.CreateP25MessageHdr((byte)P25DUID.LDU1, callData, ref payload, new CryptoParams());
                    fne.CreateP25LDU1Message(netLDU, ref payload, srcId, dstId);
                }
                else
                {
                    fne.CreateP25MessageHdr((byte)P25DUID.LDU2, callData, ref payload, new CryptoParams());
                    fne.CreateP25LDU2Message(netLDU, ref payload, new CryptoParams());
                }
                fne.peer.SendMaster(new Tuple<byte, byte>(Constants.NET_FUNC_PROTOCOL, Constants.NET_PROTOCOL_SUBFUNC_P25), payload, pktSeq, streamId);
                pktSeq = (ushort)(pktSeq + 1 > Constants.RtpCallEndSeq - 1 ? 0 : pktSeq + 1);
                await Task.Delay(180);
            }
            fne.SendP25TDU(srcId, dstId, false);
            return ldus;
        }
    }
}

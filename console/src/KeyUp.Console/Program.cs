// SPDX-License-Identifier: GPL-2.0-only
using System.ServiceProcess;
using System.Windows;

namespace KeyUp.Console;

public static class Program
{
    [STAThread]
    public static int Main(string[] args)
    {
        if (args.Contains("--tunnel-service"))
        {
            ServiceBase.Run(new TunnelService());
            return 0;
        }
        if (args.Contains("--install-service")) return TunnelService.Install();
        if (args.Contains("--uninstall-service")) return TunnelService.Uninstall();

        var app = new Application { ShutdownMode = ShutdownMode.OnMainWindowClose };
        Theme.Apply(app);
        var main = new MainWindow();
        return app.Run(main);
    }
}

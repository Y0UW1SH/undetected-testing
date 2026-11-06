using System;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.Threading.Tasks;
using System.Windows.Forms;

internal sealed class Desktop : Form
{
    private readonly string home = AppDomain.CurrentDomain.BaseDirectory;
    private readonly FlowLayoutPanel actions = new FlowLayoutPanel();
    private readonly Label status = new Label();
    private readonly CheckBox privateFeed = new CheckBox();

    internal Desktop()
    {
        Text = "Stealth Wallet Monitor";
        Font = new Font("Segoe UI", 10);
        AutoScaleMode = AutoScaleMode.Dpi;
        ClientSize = new Size(520, 451);
        FormBorderStyle = FormBorderStyle.FixedDialog;
        MaximizeBox = false;
        StartPosition = FormStartPosition.CenterScreen;
        var title = new Label { Text = "Wallet Monitor", Font = new Font(Font.FontFamily, 18, FontStyle.Bold),
            Location = new Point(22, 16), Size = new Size(460, 40) };
        Controls.Add(title);
        actions.Location = new Point(19, 67);
        actions.Size = new Size(485, 177);
        Controls.Add(actions);
        ActionButton("Open report", delegate { Run("open", false); });
        ActionButton("Edit watchlist", delegate { Run("edit-watchlist", false); });
        ActionButton("Download latest", delegate { Run("download", false); });
        ActionButton("Start local monitor", delegate { Run("start", false); });
        ActionButton("Stop local monitor", delegate { Run("stop", false); });
        ActionButton("Monitor status", delegate { Run("status", false); });
        ActionButton("GitHub login", delegate { Run("login", true); });
        ActionButton("Transfer explorer", delegate { Run("transfers", false); });
        ActionButton("Import explorer", delegate {
            using (var d = new OpenFileDialog { Filter = "Transfer explorer (*.html)|*.html", CheckFileExists = true })
                if (d.ShowDialog(this) == DialogResult.OK)
                    Run("import-explorer \"" + d.FileName + "\"", false);
        });

        var transfer = new Label { Text = "Move to another PC", Font = new Font(Font.FontFamily, 12, FontStyle.Bold),
            Location = new Point(22, 251), Size = new Size(465, 27) };
        Controls.Add(transfer);
        privateFeed.Text = "Include private local activity and checkpoint";
        privateFeed.SetBounds(23, 280, 455, 27);
        Controls.Add(privateFeed);
        var export = new Button { Text = "Export backup", Location = new Point(22, 316), Size = new Size(185, 38) };
        var import = new Button { Text = "Import backup", Location = new Point(216, 316), Size = new Size(185, 38) };
        export.Click += delegate {
            using (var d = new SaveFileDialog { Filter = "Wallet backup (*.zip)|*.zip", FileName = "wallet-backup.zip" })
                if (d.ShowDialog(this) == DialogResult.OK) Backup("export", d.FileName);
        };
        import.Click += delegate {
            using (var d = new OpenFileDialog { Filter = "Wallet backup (*.zip)|*.zip", CheckFileExists = true })
                if (d.ShowDialog(this) == DialogResult.OK) Backup("import", d.FileName);
        };
        Controls.Add(export);
        Controls.Add(import);
        status.SetBounds(22, 368, 474, 72);
        status.AutoEllipsis = true;
        status.AccessibleRole = AccessibleRole.StatusBar;
        status.Text = "Saved reports open offline.";
        Controls.Add(status);
    }

    private void ActionButton(string label, Action click)
    {
        var b = new Button { Text = label, Size = new Size(150, 52) };
        b.Click += delegate { click(); };
        actions.Controls.Add(b);
    }

    private void Backup(string command, string file)
    {
        Run(command + " \"" + file + "\"" + (privateFeed.Checked ? " --include-local-feed" : ""), false);
    }

    private async void Run(string command, bool showConsole)
    {
        foreach (Control c in Controls) if (c != status) c.Enabled = false;
        status.Text = command.StartsWith("download") ? "Checking GitHub for the newest finished scan…" : "Working…";
        var info = new ProcessStartInfo(Path.Combine(home, "runtime", "python.exe"),
            "-B \"" + Path.Combine(home, "wallet_monitor.py") + "\" " + command);
        info.UseShellExecute = false;
        info.CreateNoWindow = !showConsole;
        info.WorkingDirectory = home;
        info.EnvironmentVariables["PYTHONIOENCODING"] = "utf-8";
        if (!showConsole) {
            info.RedirectStandardOutput = true;
            info.RedirectStandardError = true;
            info.StandardOutputEncoding = System.Text.Encoding.UTF8;
            info.StandardErrorEncoding = System.Text.Encoding.UTF8;
        }
        try {
            using (var process = Process.Start(info)) {
                var output = showConsole ? Task.FromResult("") : process.StandardOutput.ReadToEndAsync();
                var errors = showConsole ? Task.FromResult("") : process.StandardError.ReadToEndAsync();
                await Task.Run((Action)process.WaitForExit);
                string message = ((await errors).Trim() + "\n" + (await output).Trim()).Trim();
                if (process.ExitCode != 0) {
                    status.Text = "The action needs attention.";
                    MessageBox.Show(this, message.Length > 0 ? message : "The action did not complete.",
                                    Text, MessageBoxButtons.OK, MessageBoxIcon.Warning);
                } else {
                    status.Text = message.Length > 0 ? message : "Done.";
                }
            }
        } catch (Exception ex) {
            status.Text = "Unable to start the wallet tool.";
            MessageBox.Show(this, ex.Message, Text, MessageBoxButtons.OK, MessageBoxIcon.Error);
        } finally {
            foreach (Control c in Controls) c.Enabled = true;
        }
    }

    [STAThread]
    private static void Main()
    {
        Application.EnableVisualStyles();
        Application.SetCompatibleTextRenderingDefault(false);
        Application.Run(new Desktop());
    }
}

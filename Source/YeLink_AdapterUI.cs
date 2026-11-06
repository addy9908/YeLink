// YeLink_AdapterUI.cs
// Compile (or run YeLink_build_ui.bat):
//   csc /target:winexe /out:YeLink_AdapterUI.exe /win32icon:YeLink.ico /reference:System.Windows.Forms.dll /reference:System.Drawing.dll YeLink_AdapterUI.cs
//
// Resident adapter. Pick COM port and baud, click Connect. The adapter waits for the
// Arduino to report READY and answer PING before it accepts commands. It listens on
// TCP 127.0.0.1:13000 for commands from EthoVision (via YeLink_Controller.exe) and
// forwards them to the Arduino.
//
// For each command the adapter waits for the firmware's own reply ("OK ..." or "ERR ...")
// and returns that reply to the TCP client. Every command is logged with a high-resolution
// timestamp, the firmware reply, and the time the line was handed to the serial port.
// The log can be exported to CSV. Arduino replies are also shown in a hideable panel.
// "Turn all off" and closing the window both drive every output pin (2-13) LOW.
//
// Change log
//   2026-10-30  AllOff: send all twelve off commands as ONE batched line (firmware splits on ';'),
//               wait for the twelve firmware replies (up to 500 ms) instead of fixed sleeps, and
//               report how many pins were confirmed off. Shutdown: flush the port before closing,
//               warn if not all pins were confirmed off, and write yelink_shutdown.log next to the
//               EXE with each step, so a failure to release outputs on exit can be diagnosed.
//               Reason: the first Test 2/3 exit check was ambiguous (logger stopped before the close);
//               the confirmation count and log make the exit behaviour verifiable from the adapter side.
//               Disconnect: now also turns all outputs off before closing the port (previously
//               Disconnect left latched outputs on, and a later window close could not reach them).

using System;
using System.Collections.Generic;
using System.Drawing;
using System.IO;
using System.IO.Ports;
using System.Net;
using System.Net.Sockets;
using System.Text;
using System.Threading;
using System.Windows.Forms;

public class AdapterForm : Form {
    // --- Serial ---
    SerialPort sp;
    ComboBox cboPort, cboBaud;
    Button btnRefresh, btnConnect;
    Label lblSerialStatus;

    // --- Serial receive: complete lines and firmware replies ---
    readonly object rxLock = new object();      // guards rxBuf and fwReplies
    readonly object sendLock = new object();    // one command in flight at a time
    readonly StringBuilder rxBuf = new StringBuilder();
    readonly Queue<string> fwReplies = new Queue<string>();

    // --- TCP ---
    const string TCP_HOST = "127.0.0.1";
    const int TCP_PORT = 13000;
    const int FW_REPLY_TIMEOUT_MS = 500;
    TcpListener listener;
    Thread tcpThread;
    volatile bool tcpRunning = false;
    Label lblTcpInfo, lblTcpStatus;

    // --- Manual send + command log ---
    TextBox txtManual;
    Button btnSend, btnExport, btnClear, btnAllOff, btnToggleReplies;
    ListView lvLog;

    // --- Arduino reply panel (hidden by default) ---
    TextBox txtReplies;
    bool repliesVisible = false;

    // --- High-resolution timestamps: one wall-clock anchor, advanced by the Stopwatch ---
    static readonly DateTime anchorWall = DateTime.Now;
    static readonly long anchorTicks = System.Diagnostics.Stopwatch.GetTimestamp();

    static string Stamp() {
        long elapsed = System.Diagnostics.Stopwatch.GetTimestamp() - anchorTicks;
        long ticks = (long)(elapsed * (TimeSpan.TicksPerSecond / (double)System.Diagnostics.Stopwatch.Frequency));
        return anchorWall.AddTicks(ticks).ToString("yyyy-MM-dd HH:mm:ss.ffffff");
    }

    public AdapterForm() {
        Text = "YeLink AdapterUI";
        try { this.Icon = System.Drawing.Icon.ExtractAssociatedIcon(Application.ExecutablePath); } catch { }
        Width = 900; Height = 540;
        StartPosition = FormStartPosition.CenterScreen;

        // ----- Serial group -----
        GroupBox grpSerial = new GroupBox() { Text = "Comport", Left = 12, Top = 8, Width = 860, Height = 80 };
        Label l1 = new Label() { Text = "Comport:", Left = 12, Top = 30, Width = 60 };
        cboPort = new ComboBox() { Left = 76, Top = 26, Width = 100, DropDownStyle = ComboBoxStyle.DropDownList };
        Label l2 = new Label() { Text = "Baud:", Left = 190, Top = 30, Width = 40 };
        cboBaud = new ComboBox() { Left = 232, Top = 26, Width = 90, DropDownStyle = ComboBoxStyle.DropDownList };
        cboBaud.Items.AddRange(new object[] { "9600", "19200", "38400", "57600", "115200" });
        cboBaud.SelectedItem = "115200";
        btnRefresh = new Button() { Text = "Refresh", Left = 335, Top = 25, Width = 70 };
        btnConnect = new Button() { Text = "Connect", Left = 415, Top = 25, Width = 90 };
        lblSerialStatus = new Label() { Text = "Not Connected", Left = 515, Top = 30, Width = 190, ForeColor = Color.White, BackColor = Color.Firebrick, TextAlign = ContentAlignment.MiddleCenter };
        btnRefresh.Click += (s, e) => RefreshPorts();
        btnConnect.Click += (s, e) => ToggleConnect();
        grpSerial.Controls.AddRange(new Control[] { l1, cboPort, l2, cboBaud, btnRefresh, btnConnect, lblSerialStatus });

        // ----- TCP group -----
        GroupBox grpTcp = new GroupBox() { Text = "Video Tracking (TCP)", Left = 12, Top = 94, Width = 860, Height = 60 };
        lblTcpInfo = new Label() { Text = "Host: " + TCP_HOST + "    Port: " + TCP_PORT, Left = 12, Top = 26, Width = 260 };
        lblTcpStatus = new Label() { Text = "Listener: stopped", Left = 300, Top = 26, Width = 300 };
        grpTcp.Controls.AddRange(new Control[] { lblTcpInfo, lblTcpStatus });

        // ----- Manual send -----
        Label l3 = new Label() { Text = "Manual command:", Left = 16, Top = 168, Width = 110 };
        txtManual = new TextBox() { Left = 130, Top = 165, Width = 360, Text = "p=8,v=1,d=5" };
        btnSend = new Button() { Text = "Send", Left = 500, Top = 163, Width = 80 };
        btnSend.Click += (s, e) => SendToSerial(txtManual.Text.Trim(), "MANUAL");

        // ----- Legend -----
        Label lblLegend = new Label() {
            Left = 130, Top = 192, Width = 700, Height = 16,
            ForeColor = Color.DimGray,
            Text = "p = digital pin (2-13)  |  v = 1 HIGH, 0 LOW  |  d = seconds, used only with v=1 (0 = latch on). v=0 is off at once."
        };

        // ----- Command log -----
        lvLog = new ListView() { Left = 12, Top = 214, Width = 876, Height = 250, View = View.Details, FullRowSelect = true, GridLines = true };
        lvLog.Columns.Add("Time received", 175);
        lvLog.Columns.Add("Source", 55);
        lvLog.Columns.Add("Received", 140);
        lvLog.Columns.Add("Sent to Arduino", 140);
        lvLog.Columns.Add("Written to port", 175);
        lvLog.Columns.Add("Firmware reply", 180);

        btnToggleReplies = new Button() { Text = "Show Arduino replies", Left = 12, Top = 470, Width = 150 };
        btnAllOff = new Button() { Text = "Turn all off", Left = 170, Top = 470, Width = 90, BackColor = Color.Gold };
        btnExport = new Button() { Text = "Export CSV", Left = 700, Top = 470, Width = 90 };
        btnClear = new Button() { Text = "Clear Log", Left = 795, Top = 470, Width = 80 };
        btnToggleReplies.Click += (s, e) => ToggleReplies();
        btnAllOff.Click += (s, e) => {
            int n = AllOff("MANUAL-ALLOFF");
            if (n < 12) MessageBox.Show("Only " + n + " of 12 pins were confirmed off by the firmware.", "Turn all off",
                                        MessageBoxButtons.OK, MessageBoxIcon.Warning);
        };
        btnExport.Click += (s, e) => ExportCsv();
        btnClear.Click += (s, e) => lvLog.Items.Clear();   // clears the log only; pins are not touched

        // ----- Arduino reply panel (hidden by default) -----
        txtReplies = new TextBox() {
            Left = 12, Top = 500, Width = 876, Height = 60,
            Multiline = true, ReadOnly = true, ScrollBars = ScrollBars.Vertical,
            BackColor = Color.FromArgb(245, 245, 245),
            Visible = false
        };

        Controls.AddRange(new Control[] { grpSerial, grpTcp, l3, txtManual, btnSend, lblLegend,
                                          lvLog, btnToggleReplies, btnAllOff, btnExport, btnClear, txtReplies });

        RefreshPorts();
        FormClosing += (s, e) => Shutdown();
    }

    void ToggleReplies() {
        repliesVisible = !repliesVisible;
        txtReplies.Visible = repliesVisible;
        btnToggleReplies.Text = repliesVisible ? "Hide Arduino replies" : "Show Arduino replies";
        Height = repliesVisible ? 600 : 540;
    }

    void RefreshPorts() {
        cboPort.Items.Clear();
        string[] ports = SerialPort.GetPortNames();
        Array.Sort(ports);
        cboPort.Items.AddRange(ports);
        if (cboPort.Items.Count > 0) cboPort.SelectedIndex = 0;
    }

    // ---- Connect: open port, wait for READY and PING/PONG, then start the TCP listener ----
    void ToggleConnect() {
        if (sp != null && sp.IsOpen) { Disconnect(); return; }
        if (cboPort.SelectedItem == null) { MessageBox.Show("Select a COM port."); return; }

        string portName = cboPort.SelectedItem.ToString();   // .NET SerialPort accepts COM10+ directly
        int baud = int.Parse(cboBaud.SelectedItem.ToString());
        try {
            sp = new SerialPort(portName, baud, Parity.None, 8, StopBits.One);
            sp.DtrEnable = false;     // reduce auto-reset on open
            sp.RtsEnable = false;
            sp.NewLine = "\n";
            sp.ReadTimeout = 500;
            sp.Open();

            lblSerialStatus.Text = "Waiting for Arduino...";
            lblSerialStatus.BackColor = Color.DarkOrange;
            Application.DoEvents();

            bool ready = WaitForArduino(12000);
            lock (rxLock) { rxBuf.Clear(); fwReplies.Clear(); }
            sp.DataReceived += SerialDataReceived;

            if (ready) {
                lblSerialStatus.Text = "Connected: " + portName;
                lblSerialStatus.BackColor = Color.SeaGreen;
            } else {
                lblSerialStatus.Text = "Connected (no READY?)";
                lblSerialStatus.BackColor = Color.DarkGoldenrod;
            }
            btnConnect.Text = "Disconnect";
            StartTcp();
        } catch (Exception ex) {
            lblSerialStatus.Text = "Failed";
            lblSerialStatus.BackColor = Color.Firebrick;
            MessageBox.Show("Serial error: " + ex.Message);
            if (sp != null && sp.IsOpen) sp.Close();
            sp = null;
        }
    }

    // Wait until the board prints READY or answers PING with PONG. Runs before the
    // DataReceived handler is attached, so it can read the port synchronously.
    bool WaitForArduino(int timeoutMs) {
        DateTime deadline = DateTime.Now.AddMilliseconds(timeoutMs);
        StringBuilder sb = new StringBuilder();
        bool sawReady = false;
        DateTime nextPing = DateTime.Now.AddMilliseconds(500);

        while (DateTime.Now < deadline) {
            try {
                string chunk = sp.ReadExisting();
                if (chunk.Length > 0) {
                    sb.Append(chunk);
                    string all = sb.ToString();
                    if (all.IndexOf("READY", StringComparison.OrdinalIgnoreCase) >= 0) sawReady = true;
                    if (all.IndexOf("PONG", StringComparison.OrdinalIgnoreCase) >= 0) return true;
                }
            } catch { }

            if (DateTime.Now >= nextPing) {
                try { sp.WriteLine("PING"); } catch { }
                nextPing = DateTime.Now.AddMilliseconds(500);
            }

            Application.DoEvents();
            Thread.Sleep(50);
        }
        return sawReady;
    }

    void Disconnect() {
        StopTcp();
        try { AllOff("DISCONNECT"); } catch { }      // release every output before giving up the port
        try { if (sp != null && sp.IsOpen) sp.Close(); } catch { }
        sp = null;
        lblSerialStatus.Text = "Not Connected";
        lblSerialStatus.BackColor = Color.Firebrick;
        btnConnect.Text = "Connect";
    }

    // ---- TCP listener ----
    void StartTcp() {
        if (tcpRunning) return;
        tcpRunning = true;
        tcpThread = new Thread(TcpLoop) { IsBackground = true };
        tcpThread.Start();
        SetTcpStatus("Listener: waiting for connection");
    }

    void StopTcp() {
        tcpRunning = false;
        try { if (listener != null) listener.Stop(); } catch { }
        SetTcpStatus("Listener: stopped");
    }

    void TcpLoop() {
        try {
            listener = new TcpListener(IPAddress.Loopback, TCP_PORT);
            listener.Start();
            while (tcpRunning) {
                TcpClient client;
                try { client = listener.AcceptTcpClient(); }   // blocks until a connection arrives
                catch (SocketException) { break; }             // listener.Stop() was called from StopTcp()
                catch (ObjectDisposedException) { break; }
                using (client)
                using (NetworkStream ns = client.GetStream()) {
                    byte[] buf = new byte[512];
                    int n = ns.Read(buf, 0, buf.Length);
                    if (n <= 0) continue;
                    string raw = Encoding.ASCII.GetString(buf, 0, n).Trim();
                    SetTcpStatus("Listener: last cmd " + DateTime.Now.ToString("HH:mm:ss"));
                    // Reply to the client with the firmware's own answer(s), e.g. "OK p=8 v=1 d=5.00"
                    // or "ERR bad pin: 99". Batches return the replies joined with ';'.
                    string fwReply = SendToSerial(raw, "TCP");
                    byte[] ack = Encoding.ASCII.GetBytes(fwReply + "\n");
                    ns.Write(ack, 0, ack.Length);
                }
            }
        } catch (Exception ex) {
            if (tcpRunning) SetTcpStatus("Listener error: " + ex.Message);
        }
    }

    // ---- Send a (possibly batched) command to the Arduino, wait for the firmware replies, log ----
    // "p=8,v=1,d=5;p=7,v=1,d=10" is sent as one serial line; the firmware splits on ';' and
    // replies once per part. Each part is logged as its own row with the receive time, the time
    // the line was handed to the serial port, and that part's firmware reply.
    // Returns the firmware replies joined with ';' (used as the TCP reply).
    string SendToSerial(string raw, string source) {
        string tRecv = Stamp();
        if (string.IsNullOrEmpty(raw)) return "";
        if (sp == null || !sp.IsOpen) {
            AddLog(source, raw, "(not connected)", tRecv, "", "");
            return "NOT-CONNECTED";
        }

        var sb = new StringBuilder();
        var logRows = new List<string[]>();
        foreach (string part in raw.Split(';')) {
            string cmd = part.Trim();
            if (cmd.Length == 0) continue;
            string serialCmd = cmd.Replace(",", " ");
            if (sb.Length > 0) sb.Append(';');
            sb.Append(serialCmd);
            logRows.Add(new string[] { cmd, serialCmd });
        }
        if (logRows.Count == 0) return "";

        lock (sendLock) {
            try {
                lock (rxLock) { fwReplies.Clear(); }   // drop stale replies from earlier commands
                sp.WriteLine(sb.ToString());
                string tWritten = Stamp();

                List<string> fw = WaitForFirmwareReplies(logRows.Count, FW_REPLY_TIMEOUT_MS);
                var replies = new List<string>();
                for (int i = 0; i < logRows.Count; i++) {
                    string f = i < fw.Count ? fw[i] : "NO-REPLY";
                    replies.Add(f);
                    AddLog(source, logRows[i][0], logRows[i][1], tRecv, tWritten, f);
                }
                return string.Join(";", replies);
            } catch (Exception ex) {
                foreach (string[] r in logRows) AddLog(source, r[0], "ERR: " + ex.Message, tRecv, "", "");
                return "ERR: " + ex.Message;
            }
        }
    }

    // Collect up to 'count' firmware replies ("OK..." or "ERR...") or stop at the timeout.
    List<string> WaitForFirmwareReplies(int count, int timeoutMs) {
        var got = new List<string>();
        DateTime deadline = DateTime.Now.AddMilliseconds(timeoutMs);
        while (got.Count < count && DateTime.Now < deadline) {
            lock (rxLock) {
                while (fwReplies.Count > 0 && got.Count < count) got.Add(fwReplies.Dequeue());
            }
            if (got.Count < count) Thread.Sleep(2);
        }
        return got;
    }

    // Receives serial data, splits it into complete lines, keeps firmware replies
    // (lines starting with OK or ERR) for the TCP path, and shows every line in the reply panel.
    void SerialDataReceived(object sender, SerialDataReceivedEventArgs e) {
        try {
            string chunk = sp.ReadExisting();
            lock (rxLock) {
                rxBuf.Append(chunk);
                string all = rxBuf.ToString();
                int start = 0, nl;
                while ((nl = all.IndexOf('\n', start)) >= 0) {
                    string line = all.Substring(start, nl - start).Trim();
                    start = nl + 1;
                    if (line.Length == 0) continue;
                    if (line.StartsWith("OK") || line.StartsWith("ERR")) fwReplies.Enqueue(line);
                    AddReply(line);
                }
                rxBuf.Remove(0, start);
                if (rxBuf.Length > 4096) rxBuf.Clear();   // guard against a line that never ends
            }
        } catch { }
    }

    // ---- Command log (thread-safe) ----
    void AddLog(string source, string received, string sent) {
        AddLog(source, received, sent, Stamp(), "", "");
    }

    void AddLog(string source, string received, string sent, string timeReceived, string written) {
        AddLog(source, received, sent, timeReceived, written, "");
    }

    void AddLog(string source, string received, string sent, string timeReceived, string written, string firmware) {
        if (IsDisposed || Disposing) return;
        if (InvokeRequired) {
            try { BeginInvoke(new Action(() => AddLog(source, received, sent, timeReceived, written, firmware))); } catch { }
            return;
        }
        var item = new ListViewItem(timeReceived);
        item.SubItems.Add(source);
        item.SubItems.Add(received);
        item.SubItems.Add(sent);
        item.SubItems.Add(written);
        item.SubItems.Add(firmware);
        lvLog.Items.Add(item);
        item.EnsureVisible();
    }

    // ---- Arduino replies (separate panel) ----
    void AddReply(string resp) {
        if (IsDisposed || Disposing) return;
        if (InvokeRequired) { try { BeginInvoke(new Action(() => AddReply(resp))); } catch { } return; }
        txtReplies.AppendText(Stamp() + "  " + resp + Environment.NewLine);
    }

    void SetTcpStatus(string text) {
        if (IsDisposed || Disposing) return;
        if (InvokeRequired) { try { BeginInvoke(new Action(() => lblTcpStatus.Text = text)); } catch { } return; }
        lblTcpStatus.Text = text;
    }

    // ---- Turn every output pin (2-13) LOW. ----
    // Sends the twelve off commands as one batched line (the firmware splits on ';') and waits for
    // the firmware's replies. Returns the number of pins the firmware confirmed with "OK".
    // Logged as one row; the command log is not cleared.
    int AllOff(string source) {
        if (sp == null || !sp.IsOpen) {
            AddLog(source, "all off", "(not connected)");
            ShutdownLog(source + ": all off skipped, port not open");
            return 0;
        }
        var sb = new StringBuilder();
        for (int pin = 2; pin <= 13; pin++) {
            if (sb.Length > 0) sb.Append(';');
            sb.Append("p=").Append(pin).Append(" v=0");
        }
        string line = sb.ToString();
        lock (sendLock) {
            try {
                lock (rxLock) { fwReplies.Clear(); }
                string tRecv = Stamp();
                sp.WriteLine(line);
                string tWritten = Stamp();
                List<string> fw = WaitForFirmwareReplies(12, FW_REPLY_TIMEOUT_MS);
                int ok = 0;
                foreach (string f in fw) if (f.StartsWith("OK")) ok++;
                AddLog(source, "all off", "p=2..13 v=0", tRecv, tWritten, ok + " of 12 confirmed");
                ShutdownLog(source + ": all off written, " + fw.Count + " replies, " + ok + " OK");
                return ok;
            } catch (Exception ex) {
                AddLog(source, "all off", "ERR: " + ex.Message);
                ShutdownLog(source + ": all off EXCEPTION " + ex.GetType().Name + ": " + ex.Message);
                return 0;
            }
        }
    }

    // ---- Shutdown diagnostics: appended to yelink_shutdown.log next to the EXE ----
    static void ShutdownLog(string msg) {
        try {
            string path = Path.Combine(Path.GetDirectoryName(Application.ExecutablePath), "yelink_shutdown.log");
            File.AppendAllText(path, Stamp() + "  " + msg + Environment.NewLine);
        } catch { }
    }

    // ---- CSV export: UTF-8 without a byte-order mark, so the header reads as "Time" ----
    void ExportCsv() {
        using (SaveFileDialog sfd = new SaveFileDialog()) {
            sfd.Filter = "CSV files (*.csv)|*.csv";
            sfd.FileName = "yelink_log_" + DateTime.Now.ToString("yyyyMMdd_HHmmss") + ".csv";
            if (sfd.ShowDialog() != DialogResult.OK) return;
            try {
                using (StreamWriter w = new StreamWriter(sfd.FileName, false, new UTF8Encoding(false))) {
                    w.WriteLine("Time,Source,Received,SentToArduino,WrittenToPort,FirmwareReply");
                    foreach (ListViewItem it in lvLog.Items) {
                        var cols = new List<string>();
                        for (int i = 0; i < 6; i++) cols.Add(Csv(it.SubItems[i].Text));
                        w.WriteLine(string.Join(",", cols));
                    }
                }
                MessageBox.Show("Saved: " + sfd.FileName);
            } catch (Exception ex) {
                MessageBox.Show("Export error: " + ex.Message);
            }
        }
    }

    static string Csv(string s) {
        if (s == null) return "";
        if (s.Contains(",") || s.Contains("\"") || s.Contains("\n"))
            return "\"" + s.Replace("\"", "\"\"") + "\"";
        return s;
    }

    // On close: turn every pin off first (and confirm), then stop the listener and release the port.
    bool shutdownDone = false;
    void Shutdown() {
        if (shutdownDone) return;
        shutdownDone = true;
        ShutdownLog("Shutdown started (port " + (sp != null && sp.IsOpen ? sp.PortName + " open" : "not open") + ")");
        StopTcp();                                   // no new commands while we switch off
        int ok = 0;
        try { ok = AllOff("SHUTDOWN"); } catch (Exception ex) { ShutdownLog("AllOff threw " + ex.Message); }
        try {
            if (sp != null && sp.IsOpen) {
                try { sp.BaseStream.Flush(); } catch { }
                Thread.Sleep(100);                   // let the last bytes leave the USB-serial link
                sp.Close();
                ShutdownLog("Port closed");
            }
        } catch (Exception ex) { ShutdownLog("Port close threw " + ex.Message); }
        if (sp != null && ok < 12) {
            MessageBox.Show("Only " + ok + " of 12 output pins were confirmed off by the firmware.\n" +
                            "Check the outputs. Details: yelink_shutdown.log next to the EXE.",
                            "YeLink: outputs may still be on", MessageBoxButtons.OK, MessageBoxIcon.Warning);
        }
        ShutdownLog("Shutdown finished, " + ok + " of 12 confirmed off");
    }

    [STAThread]
    static void Main() {
        Application.EnableVisualStyles();
        Application.Run(new AdapterForm());
    }
}

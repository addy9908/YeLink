// ZY_AdapterUI.cs
// Compile:
//   csc /target:winexe /out:ZY_AdapterUI.exe /reference:System.Windows.Forms.dll /reference:System.Drawing.dll ZY_AdapterUI.cs
//
// Resident adapter with GUI: pick COM + baud, Connect, view TCP status,
// send manual commands, receive EthoVision commands over TCP 127.0.0.1:13000,
// log commands sent to Arduino, export log to CSV.
// Arduino replies shown in a separate, hideable status panel (hidden by default).

using System;
using System.Collections.Generic;
using System.Drawing;
using System.Globalization;
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

    // --- TCP ---
    const string TCP_HOST = "127.0.0.1";
    const int TCP_PORT = 13000;
    TcpListener listener;
    Thread tcpThread;
    volatile bool tcpRunning = false;
    Label lblTcpInfo, lblTcpStatus;

    // --- Manual send + log ---
    TextBox txtManual;
    Button btnSend, btnExport, btnClear;
    ListView lvLog;

    // --- Arduino reply panel (hideable) ---
    Button btnToggleReplies;
	Button btnAllOff;  
    TextBox txtReplies;
    bool repliesVisible = false;

    public AdapterForm() {
        Text = "ZY_PinBridge";
		try { this.Icon = System.Drawing.Icon.ExtractAssociatedIcon(Application.ExecutablePath); } catch { }
        Width = 760; Height = 600;
        StartPosition = FormStartPosition.CenterScreen;

        // ----- Serial group -----
        GroupBox grpSerial = new GroupBox() { Text = "Comport", Left = 12, Top = 8, Width = 720, Height = 80 };
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
        GroupBox grpTcp = new GroupBox() { Text = "Video Tracking (TCP)", Left = 12, Top = 94, Width = 720, Height = 60 };
        lblTcpInfo = new Label() { Text = "Host: " + TCP_HOST + "    Port: " + TCP_PORT, Left = 12, Top = 26, Width = 260 };
        lblTcpStatus = new Label() { Text = "Listener: stopped", Left = 300, Top = 26, Width = 300 };
        grpTcp.Controls.AddRange(new Control[] { lblTcpInfo, lblTcpStatus });

        // ----- Manual send -----
        Label l3 = new Label() { Text = "Manual command:", Left = 16, Top = 168, Width = 110 };
        txtManual = new TextBox() { Left = 130, Top = 165, Width = 360 };
        txtManual.Text = "p=8,v=1,d=5";
        btnSend = new Button() { Text = "Send", Left = 500, Top = 163, Width = 80 };
        btnSend.Click += (s, e) => { SendToSerial(txtManual.Text.Trim(), "MANUAL"); };

        // ----- Legend -----
        Label lblLegend = new Label() {
            Left = 130, Top = 192, Width = 600, Height = 16,
            ForeColor = Color.DimGray,
            Text = "p = digital pin (2-13)   |   v = 1 HIGH, 0 LOW   |   d = duration in seconds (0 = latch, hold until changed)"
        };

        // ----- Log -----
        lvLog = new ListView() { Left = 12, Top = 214, Width = 720, Height = 250, View = View.Details, FullRowSelect = true, GridLines = true };
        lvLog.Columns.Add("Time", 150);
        lvLog.Columns.Add("Source", 90);
        lvLog.Columns.Add("Received", 230);
        lvLog.Columns.Add("Sent to Arduino", 230);

        btnExport = new Button() { Text = "Export CSV", Left = 560, Top = 470, Width = 90 };
        btnClear = new Button() { Text = "Clear Log", Left = 655, Top = 470, Width = 80 };
        btnToggleReplies = new Button() { Text = "Show Arduino replies", Left = 12, Top = 470, Width = 150 };
        btnAllOff = new Button() { Text = "Turn all off", Left = 170, Top = 470, Width = 90, BackColor = Color.Gold };
        btnExport.Click += (s, e) => ExportCsv();
        btnClear.Click += (s, e) => lvLog.Items.Clear();
        btnToggleReplies.Click += (s, e) => ToggleReplies();
        btnAllOff.Click += (s, e) => AllOff("MANUAL-ALLOFF");

        // ----- Arduino reply panel (hidden by default) -----
        txtReplies = new TextBox() {
            Left = 12, Top = 500, Width = 720, Height = 60,
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

    void ToggleConnect() {
        if (sp != null && sp.IsOpen) { Disconnect(); return; }
        if (cboPort.SelectedItem == null) { MessageBox.Show("Select a COM port."); return; }

        string portName = cboPort.SelectedItem.ToString();
        string sysName = portName;

		int baud = int.Parse(cboBaud.SelectedItem.ToString());
        try {
            sp = new SerialPort(sysName, baud, Parity.None, 8, StopBits.One);
            sp.DtrEnable = false;
            sp.RtsEnable = false;
            sp.NewLine = "\n";
            sp.ReadTimeout = 500;
            sp.Open();

            lblSerialStatus.Text = "Waiting for Arduino...";
            lblSerialStatus.BackColor = Color.DarkOrange;
            Application.DoEvents();   // let the label repaint before we block

            bool ready = WaitForArduino(12000);   // up to 12 s

            // now attach the live reply handler for normal operation
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
            sp = null;
        }
    }

	// Wait for the board to finish booting: look for READY, then confirm with PING/PONG.
    // Returns true once the Arduino answers correctly. Does NOT use the DataReceived handler
    // (that gets attached afterward), so we can read synchronously here.
    bool WaitForArduino(int timeoutMs) {
        DateTime deadline = DateTime.Now.AddMilliseconds(timeoutMs);
        StringBuilder sb = new StringBuilder();
        bool sawReady = false;
        DateTime nextPing = DateTime.Now.AddMilliseconds(500);

        while (DateTime.Now < deadline) {
            // drain whatever is available
            try {
                string chunk = sp.ReadExisting();
                if (chunk.Length > 0) {
                    sb.Append(chunk);
                    string all = sb.ToString();
                    if (all.IndexOf("READY", StringComparison.OrdinalIgnoreCase) >= 0) sawReady = true;
                    if (all.IndexOf("PONG", StringComparison.OrdinalIgnoreCase) >= 0) return true;
                }
            } catch { }

            // once we've seen READY (or after a short wait), start pinging to confirm parser is live
            if (DateTime.Now >= nextPing) {
                try { sp.WriteLine("PING"); } catch { }
                nextPing = DateTime.Now.AddMilliseconds(500);
            }

            Application.DoEvents();   // keep UI responsive
            Thread.Sleep(50);
        }
        return sawReady;   // fall back: at least we saw READY even if PONG was missed
    }

    void Disconnect() {
        StopTcp();
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
                if (!listener.Pending()) { Thread.Sleep(50); continue; }
                using (TcpClient client = listener.AcceptTcpClient())
                using (NetworkStream ns = client.GetStream()) {
                    byte[] buf = new byte[512];
                    int n = ns.Read(buf, 0, buf.Length);
                    if (n <= 0) continue;
                    string raw = Encoding.ASCII.GetString(buf, 0, n).Trim();
                    SetTcpStatus("Listener: last cmd " + DateTime.Now.ToString("HH:mm:ss"));
                    SendToSerial(raw, "TCP");
                    byte[] ack = Encoding.ASCII.GetBytes("OK\n");
                    ns.Write(ack, 0, ack.Length);
                }
            }
        } catch (Exception ex) {
            if (tcpRunning) SetTcpStatus("Listener error: " + ex.Message);
        }
    }

    // ---- Serial send + log ----
	// Split a (possibly batched) command on ';', send each piece as its own line, log each.
	void SendToSerial(string raw, string source) {
        if (string.IsNullOrEmpty(raw)) return;
        if (sp == null || !sp.IsOpen) { AddLog(source, raw, "(not connected)"); return; }

        string[] parts = raw.Split(';');
        // Build one combined line for the Arduino (its firmware splits on ';')
        var sb = new StringBuilder();
        foreach (string part in parts) {
            string cmd = part.Trim();
            if (cmd.Length == 0) continue;
            string serialCmd = cmd.Replace(",", " ");
            if (sb.Length > 0) sb.Append(';');
            sb.Append(serialCmd);
            AddLog(source, cmd, serialCmd);   // log each piece separately
        }
        try { sp.WriteLine(sb.ToString()); }   // send once, no inter-command sleep
        catch (Exception ex) { AddLog(source, raw, "ERR: " + ex.Message); }
    }

    void SerialDataReceived(object sender, SerialDataReceivedEventArgs e) {
        try {
            string resp = sp.ReadExisting().Trim();
            if (resp.Length > 0) AddReply(resp);
        } catch { }
    }
	
    // ---- Logging (thread-safe) : only commands sent to Arduino ----
    void AddLog(string source, string received, string sent) {
        if (InvokeRequired) { BeginInvoke(new Action(() => AddLog(source, received, sent))); return; }
        var item = new ListViewItem(DateTime.Now.ToString("yyyy-MM-dd HH:mm:ss.fff"));
        item.SubItems.Add(source);
        item.SubItems.Add(received);
        item.SubItems.Add(sent);
        lvLog.Items.Add(item);
        item.EnsureVisible();
    }

    // ---- Arduino replies : separate panel ----
    void AddReply(string resp) {
        if (InvokeRequired) { BeginInvoke(new Action(() => AddReply(resp))); return; }
        txtReplies.AppendText(DateTime.Now.ToString("HH:mm:ss.fff") + "  " + resp + Environment.NewLine);
    }

    void SetTcpStatus(string text) {
        if (InvokeRequired) { BeginInvoke(new Action(() => lblTcpStatus.Text = text)); return; }
        lblTcpStatus.Text = text;
    }

    // ---- CSV export ----
    void ExportCsv() {
        using (SaveFileDialog sfd = new SaveFileDialog()) {
            sfd.Filter = "CSV files (*.csv)|*.csv";
            sfd.FileName = "zy_adapter_log_" + DateTime.Now.ToString("yyyyMMdd_HHmmss") + ".csv";
            if (sfd.ShowDialog() != DialogResult.OK) return;
            try {
                using (StreamWriter w = new StreamWriter(sfd.FileName, false, Encoding.UTF8)) {
                    w.WriteLine("Time,Source,Received,SentToArduino");
                    foreach (ListViewItem it in lvLog.Items) {
                        w.WriteLine(Csv(it.SubItems[0].Text) + "," + Csv(it.SubItems[1].Text) + "," +
                                    Csv(it.SubItems[2].Text) + "," + Csv(it.SubItems[3].Text));
                    }
                }
                MessageBox.Show("Saved: " + sfd.FileName);
            } catch (Exception ex) { MessageBox.Show("Export error: " + ex.Message); }
        }
    }

    static string Csv(string s) {
        if (s == null) return "";
        if (s.Contains(",") || s.Contains("\"") || s.Contains("\n")) return "\"" + s.Replace("\"", "\"\"") + "\"";
        return s;
    }

	// Turn every output pin (2-13) LOW. Logs the action; does NOT clear the log.
    void AllOff(string source) {
        if (sp == null || !sp.IsOpen) {
            AddLog(source, "all off", "(not connected)");
            return;
        }
        try {
            for (int pin = 2; pin <= 13; pin++) {
                sp.WriteLine("p=" + pin + " v=0");
                Thread.Sleep(15);   // small gap so the Uno parses each line
            }
            AddLog(source, "all off", "p=2..13 v=0");
        } catch (Exception ex) {
            AddLog(source, "all off", "ERR: " + ex.Message);
        }
    }

    void Shutdown() {
        try { AllOff("SHUTDOWN"); } catch { }
        StopTcp();
        try { if (sp != null && sp.IsOpen) sp.Close(); } catch { }
    }

    [STAThread]
    static void Main() {
        Application.EnableVisualStyles();
        Application.Run(new AdapterForm());
    }
}
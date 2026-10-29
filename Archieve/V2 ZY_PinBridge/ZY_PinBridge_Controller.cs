// MyController.cs
// Compile: csc MyController.cs
// EthoVision "Select program to run": MyController.exe
// "Command line options": p=8,v=1   or   p=8,v=1,d=30.00
using System;
using System.Net.Sockets;
using System.Text;

class MyController {
    const string HOST = "127.0.0.1";
    const int PORT = 13000;

    static int Main(string[] args) {
        if (args.Length == 0) { Console.Error.WriteLine("No command"); return 1; }
        string cmd = string.Join(" ", args);   // e.g. "p=8,v=1,d=30.00"

        try {
            using (TcpClient c = new TcpClient()) {
                c.Connect(HOST, PORT);
                c.SendTimeout = 2000;
                c.ReceiveTimeout = 2000;
                byte[] data = Encoding.ASCII.GetBytes(cmd + "\n");
                c.GetStream().Write(data, 0, data.Length);
                // optional: read one-line ack
                try {
                    byte[] buf = new byte[256];
                    int n = c.GetStream().Read(buf, 0, buf.Length);
                    if (n > 0) Console.WriteLine(Encoding.ASCII.GetString(buf, 0, n).Trim());
                } catch { /* ack optional */ }
            }
        } catch (Exception e) {
            Console.Error.WriteLine("Connect/send error: " + e.Message);
            return 1;
        }
        return 0;
    }
}
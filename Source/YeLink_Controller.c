// ZY_Etholink_Controller.c  — native Win32, no .NET. Build: gcc ZY_Controller.c -o ZY_Controller.exe -lws2_32 -mwindows
#include <winsock2.h>
#include <windows.h>
#include <string.h>
#pragma comment(lib, "ws2_32.lib")

int WINAPI WinMain(HINSTANCE h, HINSTANCE p, LPSTR cmdline, int n) {
    if (!cmdline || !*cmdline) return 1;
    char buf[512]; int len = (int)strlen(cmdline);
    if (len > 500) len = 500;
    memcpy(buf, cmdline, len); buf[len++] = '\n'; buf[len] = 0;

    WSADATA w; if (WSAStartup(MAKEWORD(2,2), &w) != 0) return 1;
    SOCKET s = socket(AF_INET, SOCK_STREAM, 0);
    struct sockaddr_in a; memset(&a, 0, sizeof a);
    a.sin_family = AF_INET; a.sin_port = htons(13000);
    a.sin_addr.s_addr = inet_addr("127.0.0.1");
    int rc = 1;
    if (connect(s, (struct sockaddr*)&a, sizeof a) == 0) {
        send(s, buf, len, 0);
        char ack[64]; recv(s, ack, sizeof ack, 0);   // wait for adapter's OK
        rc = 0;
    }
    closesocket(s); WSACleanup();
    return rc;
}
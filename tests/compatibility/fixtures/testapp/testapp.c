/*
 * Boswas WinCompat test application (64-bit Windows console program).
 *
 * Built from source by tests/compatibility/build_fixtures.sh with MinGW-w64;
 * no binary is committed. Used by the image integration test and the QEMU
 * boot test to exercise boswas-winapp end to end:
 *
 *   boswas-testapp.exe /S                 "installer": copies itself to
 *                                         C:\Program Files\Boswas Test App\
 *   boswas-testapp.exe [PROBE...]         "application": prints
 *                                         BOSWAS-TESTAPP OK, records the run
 *                                         in %APPDATA%, then runs the probes
 *
 * Probes report what the sandbox allows; each prints one line
 *   PROBE <kind> <target> ALLOWED|BLOCKED (error N)
 *
 *   read PATH          open PATH and read from it
 *   write PATH         create PATH and write to it
 *   list DIR           list a directory
 *   connect IP PORT    TCP connection to IPv4 IP:PORT
 *   exit N             exit with code N
 *   sleep MS           sleep MS milliseconds
 */
#include <winsock2.h>
#include <windows.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

/* build_fixtures.sh builds byte-different variants (blocked, unlisted). */
#ifndef BOSWAS_VARIANT
#define BOSWAS_VARIANT 1
#endif

static void report(const char *kind, const char *target, int allowed, unsigned long err)
{
	if (allowed)
		printf("PROBE %s %s ALLOWED\n", kind, target);
	else
		printf("PROBE %s %s BLOCKED (error %lu)\n", kind, target, err);
	fflush(stdout);
}

static void probe_read(const char *path)
{
	char buf[64];
	DWORD n = 0;
	HANDLE h = CreateFileA(path, GENERIC_READ, FILE_SHARE_READ | FILE_SHARE_WRITE, NULL,
			       OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, NULL);
	if (h == INVALID_HANDLE_VALUE) {
		report("read", path, 0, GetLastError());
		return;
	}
	BOOL ok = ReadFile(h, buf, sizeof buf, &n, NULL);
	DWORD err = GetLastError();
	CloseHandle(h);
	report("read", path, ok, err);
}

static void probe_write(const char *path)
{
	static const char data[] = "boswas sandbox probe\r\n";
	DWORD n = 0;
	HANDLE h = CreateFileA(path, GENERIC_WRITE, 0, NULL, CREATE_ALWAYS, FILE_ATTRIBUTE_NORMAL, NULL);
	if (h == INVALID_HANDLE_VALUE) {
		report("write", path, 0, GetLastError());
		return;
	}
	BOOL ok = WriteFile(h, data, sizeof data - 1, &n, NULL);
	DWORD err = GetLastError();
	CloseHandle(h);
	report("write", path, ok && n == sizeof data - 1, err);
}

static void probe_list(const char *dir)
{
	char pattern[MAX_PATH];
	WIN32_FIND_DATAA fd;
	snprintf(pattern, sizeof pattern, "%s\\*", dir);
	HANDLE h = FindFirstFileA(pattern, &fd);
	if (h == INVALID_HANDLE_VALUE) {
		report("list", dir, 0, GetLastError());
		return;
	}
	int count = 0;
	do {
		count++;
	} while (FindNextFileA(h, &fd));
	FindClose(h);
	printf("PROBE list %s ALLOWED (%d entries)\n", dir, count);
	fflush(stdout);
}

static void probe_connect(const char *ip, const char *port)
{
	char target[64];
	WSADATA wsa;
	snprintf(target, sizeof target, "%s:%s", ip, port);
	if (WSAStartup(MAKEWORD(2, 2), &wsa) != 0) {
		report("connect", target, 0, (unsigned long)WSAGetLastError());
		return;
	}
	SOCKET s = socket(AF_INET, SOCK_STREAM, IPPROTO_TCP);
	if (s == INVALID_SOCKET) {
		report("connect", target, 0, (unsigned long)WSAGetLastError());
		WSACleanup();
		return;
	}
	struct sockaddr_in addr;
	memset(&addr, 0, sizeof addr);
	addr.sin_family = AF_INET;
	addr.sin_port = htons((u_short)atoi(port));
	addr.sin_addr.s_addr = inet_addr(ip);
	int rc = connect(s, (struct sockaddr *)&addr, sizeof addr);
	unsigned long err = rc == 0 ? 0 : (unsigned long)WSAGetLastError();
	closesocket(s);
	WSACleanup();
	report("connect", target, rc == 0, err);
}

static int install(void)
{
	char self[MAX_PATH];
	const char *dir = "C:\\Program Files\\Boswas Test App";
	char dest[MAX_PATH];
	if (!GetModuleFileNameA(NULL, self, sizeof self))
		return 10;
	if (!CreateDirectoryA(dir, NULL) && GetLastError() != ERROR_ALREADY_EXISTS)
		return 11;
	snprintf(dest, sizeof dest, "%s\\boswas-testapp.exe", dir);
	if (!CopyFileA(self, dest, FALSE))
		return 12;
	printf("BOSWAS-TESTAPP INSTALLED %s (variant %d)\n", dest, BOSWAS_VARIANT);
	return 0;
}

static void record_run(void)
{
	char appdata[MAX_PATH], dir[MAX_PATH], file[MAX_PATH];
	if (!GetEnvironmentVariableA("APPDATA", appdata, sizeof appdata))
		return;
	snprintf(dir, sizeof dir, "%s\\BoswasTestApp", appdata);
	CreateDirectoryA(dir, NULL);
	snprintf(file, sizeof file, "%s\\runs.txt", dir);
	HANDLE h = CreateFileA(file, FILE_APPEND_DATA, 0, NULL, OPEN_ALWAYS, FILE_ATTRIBUTE_NORMAL, NULL);
	if (h != INVALID_HANDLE_VALUE) {
		DWORD n;
		WriteFile(h, "run\r\n", 5, &n, NULL);
		CloseHandle(h);
	}
}

int main(int argc, char **argv)
{
	if (argc > 1 && strcmp(argv[1], "/S") == 0)
		return install();

	printf("BOSWAS-TESTAPP OK (variant %d)\n", BOSWAS_VARIANT);
	fflush(stdout);
	record_run();
	for (int i = 1; i < argc; i++) {
		if (strcmp(argv[i], "read") == 0 && i + 1 < argc)
			probe_read(argv[++i]);
		else if (strcmp(argv[i], "write") == 0 && i + 1 < argc)
			probe_write(argv[++i]);
		else if (strcmp(argv[i], "list") == 0 && i + 1 < argc)
			probe_list(argv[++i]);
		else if (strcmp(argv[i], "connect") == 0 && i + 2 < argc) {
			probe_connect(argv[i + 1], argv[i + 2]);
			i += 2;
		} else if (strcmp(argv[i], "sleep") == 0 && i + 1 < argc)
			Sleep((DWORD)atoi(argv[++i]));
		else if (strcmp(argv[i], "exit") == 0 && i + 1 < argc)
			return atoi(argv[i + 1]);
		else {
			fprintf(stderr, "boswas-testapp: unknown argument %s\n", argv[i]);
			return 2;
		}
	}
	return 0;
}

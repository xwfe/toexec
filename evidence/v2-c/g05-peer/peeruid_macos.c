/*
 * 查一条本机回环 TCP 连接的客户端 socket 属于哪个 uid（macOS）。
 *
 * 读的是 netstat 用的同一张表 net.inet.tcp.pcblist64，只用 SDK 公开头文件；
 * 不需要特权，其他用户的 socket 也在表里。
 *
 *   peeruid_macos <客户端端口> <服务端端口>   找到就打印 uid 退出 0；没找到退出 3；
 *                                           同一对端口出现不同 uid 退出 4（不该发生，按拒绝处理）
 *   peeruid_macos --list                     打印表里所有 IPv4 TCP socket 的 uid 分布
 */
#include <sys/types.h>
#include <sys/socket.h>
#include <sys/socketvar.h>
#include <sys/sysctl.h>
#include <netinet/in.h>
#include <netinet/in_systm.h>
#include <netinet/ip.h>
#include <netinet/in_pcb.h>
#include <netinet/tcp.h>
#include <netinet/tcp_timer.h>
#include <netinet/tcp_var.h>
#include <arpa/inet.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

int main(int argc, char **argv) {
    int list = argc == 2 && strcmp(argv[1], "--list") == 0;
    if (!list && argc != 3) {
        fprintf(stderr, "usage: %s <client_port> <server_port> | --list\n", argv[0]);
        return 2;
    }
    unsigned client_port = list ? 0 : (unsigned)atoi(argv[1]);
    unsigned server_port = list ? 0 : (unsigned)atoi(argv[2]);

    size_t len = 0;
    char *buf = NULL;
    /* 表在两次调用之间可能变长，多给点余量并重试。 */
    for (int attempt = 0; attempt < 5; attempt++) {
        if (sysctlbyname("net.inet.tcp.pcblist64", NULL, &len, NULL, 0) < 0) {
            perror("sysctl size");
            return 2;
        }
        len += len / 4 + 4096;
        free(buf);
        buf = malloc(len);
        if (buf == NULL) return 2;
        if (sysctlbyname("net.inet.tcp.pcblist64", buf, &len, NULL, 0) == 0) break;
        if (attempt == 4) {
            perror("sysctl read");
            return 2;
        }
    }

    const char *end = buf + len;
    struct xinpgen *head = (struct xinpgen *)buf;
    long found_uid = -1;
    int conflict = 0, total = 0;
    long uids[64];
    int counts[64], distinct = 0;

    for (struct xinpgen *xig = (struct xinpgen *)((char *)head + head->xig_len);
         (char *)xig + sizeof(struct xinpgen) <= end && xig->xig_len > sizeof(struct xinpgen);
         xig = (struct xinpgen *)((char *)xig + xig->xig_len)) {
        struct xtcpcb64 *tp = (struct xtcpcb64 *)xig;
        struct xinpcb64 *inp = &tp->xt_inpcb;
        struct xsocket64 *so = &inp->xi_socket;
        if (so->xso_protocol != IPPROTO_TCP || !(inp->inp_vflag & INP_IPV4)) continue;
        total++;
        long uid = (long)so->so_uid;
        if (list) {
            in_addr_t lo = htonl(INADDR_LOOPBACK);
            if (inp->inp_dependladdr.inp46_local.ia46_addr4.s_addr == lo &&
                inp->inp_dependfaddr.inp46_foreign.ia46_addr4.s_addr == lo && inp->inp_fport != 0)
                printf("loopback uid=%ld lport=%u fport=%u\n", uid, ntohs(inp->inp_lport), ntohs(inp->inp_fport));
            int i = 0;
            while (i < distinct && uids[i] != uid) i++;
            if (i == distinct && distinct < 64) { uids[distinct] = uid; counts[distinct++] = 0; }
            if (i < 64) counts[i]++;
            continue;
        }
        in_addr_t loopback = htonl(INADDR_LOOPBACK);
        if (ntohs(inp->inp_lport) == client_port && ntohs(inp->inp_fport) == server_port &&
            inp->inp_dependladdr.inp46_local.ia46_addr4.s_addr == loopback &&
            inp->inp_dependfaddr.inp46_foreign.ia46_addr4.s_addr == loopback) {
            if (found_uid >= 0 && found_uid != uid) conflict = 1;
            found_uid = uid;
        }
    }
    free(buf);
    if (list) {
        printf("ipv4_tcp_sockets=%d\n", total);
        for (int i = 0; i < distinct; i++) printf("uid=%ld count=%d\n", uids[i], counts[i]);
        return 0;
    }
    if (conflict) return 4;
    if (found_uid < 0) return 3;
    printf("%ld\n", found_uid);
    return 0;
}

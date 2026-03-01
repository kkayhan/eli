#!/usr/bin/env python3
"""
iperf traffic generator for eli containerlab.

Generates UDP traffic from server-wan to server1-4 (anycast VIP 1.1.1.1).
Servers run iperf in server mode; server-wan runs iperf clients.
"""

from concurrent.futures import ThreadPoolExecutor, as_completed
import subprocess
import time
import sys
import re
import os
import select
import termios
import tty

SERVER_NODES = ["server1", "server2", "server3", "server4"]
CLIENT_NODE = "server-wan"
BASE_PORT = 5001
MONITOR_IFACE = "bond0"


def discover_containers():
    """Discover container names for clab nodes, handling any prefix configuration."""
    result = subprocess.run(
        ["docker", "ps", "--format", "{{.Names}}\t{{.Label \"clab-node-name\"}}"],
        capture_output=True, text=True
    )
    node_to_container = {}
    for line in result.stdout.strip().splitlines():
        parts = line.split("\t")
        if len(parts) == 2 and parts[1]:
            node_to_container[parts[1]] = parts[0]

    servers = []
    for node in SERVER_NODES:
        if node not in node_to_container:
            sys.exit(f"Error: container for node '{node}' not found. Is the lab running?")
        servers.append(node_to_container[node])

    if CLIENT_NODE not in node_to_container:
        sys.exit(f"Error: container for node '{CLIENT_NODE}' not found. Is the lab running?")
    client = node_to_container[CLIENT_NODE]

    return servers, client


SERVERS, CLIENT = discover_containers()
ALL_HOSTS = SERVERS + [CLIENT]


def run_cmd(host, cmd):
    result = subprocess.run(
        ["docker", "exec", host, "bash", "-c", cmd],
        capture_output=True, text=True
    )
    return host, result.stdout.strip(), result.stderr.strip()


def run_bulk(tasks):
    """Run a list of (host, cmd) tuples in parallel via threads."""
    results = []
    with ThreadPoolExecutor(max_workers=len(tasks)) as pool:
        futures = {pool.submit(run_cmd, host, cmd): (host, cmd) for host, cmd in tasks}
        for future in as_completed(futures):
            results.append(future.result())
    return results


def start(flows, total_bandwidth, vips):
    """Start iperf servers on server1-4 and clients on server-wan.

    vips is a list of destination IPs. Each VIP gets `flows` flows at
    `total_bandwidth` Mbps. Ports are offset per VIP to avoid collisions.
    """
    bw_per_flow = total_bandwidth / flows

    # Clean up any existing iperf processes first
    stop()

    # Total ports needed across all VIPs
    total_ports = flows * len(vips)

    # Start all iperf servers (all ports for all VIPs)
    server_cmd = "; ".join(
        f"iperf -s -u -p {BASE_PORT + i} -D" for i in range(total_ports)
    )
    server_tasks = [(srv, server_cmd) for srv in SERVERS]
    print(f"  Starting {total_ports} iperf server(s) per host on ports {BASE_PORT}-{BASE_PORT + total_ports - 1}...")
    run_bulk(server_tasks)

    time.sleep(1)

    # Start client flows for each VIP
    client_parts = []
    for vip_idx, vip in enumerate(vips):
        port_offset = vip_idx * flows
        print(f"  VIP {vip}: {flows} flow(s), {total_bandwidth} Mbps ({bw_per_flow:.2f} Mbps/flow), ports {BASE_PORT + port_offset}-{BASE_PORT + port_offset + flows - 1}")
        for i in range(flows):
            port = BASE_PORT + port_offset + i
            client_parts.append(
                f"setsid sh -c 'iperf -c {vip} -u -p {port} "
                f"-b {bw_per_flow:.2f}M -t 86400 > /dev/null 2>&1' &"
            )

    client_cmd = " ".join(client_parts)
    print(f"  Starting {len(client_parts)} total iperf client(s) on {CLIENT}...")
    run_cmd(CLIENT, client_cmd)

    print("Traffic started.")


def stop():
    """Kill all iperf processes on every host."""
    print("Stopping iperf on all hosts...")
    tasks = [
        (host, "killall iperf 2>/dev/null; killall iperf3 2>/dev/null")
        for host in ALL_HOSTS
    ]
    run_bulk(tasks)
    print("Traffic stopped.")


def parse_proc_net_dev(output, iface):
    """Parse /proc/net/dev and return (rx_bytes, rx_packets, rx_drop) for iface."""
    for line in output.splitlines():
        line = line.strip()
        if line.startswith(iface + ":"):
            parts = line.split(":")[1].split()
            # /proc/net/dev columns: rx_bytes rx_packets rx_errs rx_drop ...
            rx_bytes = int(parts[0])
            rx_packets = int(parts[1])
            rx_drop = int(parts[3])
            return rx_bytes, rx_packets, rx_drop
    return 0, 0, 0


def read_counters():
    """Read rx byte/packet/drop counters from all servers + client tx in parallel."""
    targets = SERVERS + [CLIENT]
    tasks = [(host, "cat /proc/net/dev") for host in targets]
    results = {}
    for host, out, _ in run_bulk(tasks):
        results[host] = parse_proc_net_dev(out, MONITOR_IFACE)
    return results


def status():
    """Show running iperf processes on all hosts."""
    tasks = [(host, "pgrep -a iperf || echo 'no iperf running'") for host in ALL_HOSTS]
    for host, out, _ in sorted(run_bulk(tasks), key=lambda x: x[0]):
        print(f"  [{host}] {out}")


def build_monitor_table(prev, prev_time):
    """Read counters and build the monitor display table. Returns (lines, curr, curr_time)."""
    curr = read_counters()
    curr_time = time.time()
    dt = curr_time - prev_time

    stats = []
    for srv in SERVERS:
        p = prev.get(srv, (0, 0, 0))
        c = curr.get(srv, (0, 0, 0))

        delta_bytes = c[0] - p[0]
        delta_pkts = c[1] - p[1]
        delta_drops = c[2] - p[2]

        bw_mbps = (delta_bytes * 8) / (dt * 1_000_000) if dt > 0 else 0

        stats.append({
            'host': srv,
            'bandwidth_mbps': bw_mbps,
            'rx_packets': delta_pkts,
            'drops': delta_drops,
            'total_drops': c[2],
        })

    total_bw = sum(s['bandwidth_mbps'] for s in stats)
    total_pkts = sum(s['rx_packets'] for s in stats)
    total_drops = sum(s['drops'] for s in stats)

    lines = []
    lines.append("=" * 70)
    lines.append(f"  {'Receiver':<12} {'Throughput':>12} {'Rx Packets':>12} {'Drops':>8} {'Total Drops':>12}")
    lines.append("-" * 70)
    for s in sorted(stats, key=lambda x: x['host']):
        drop_str = str(s['drops'])
        if s['drops'] > 0:
            drop_str = f"\033[91m{drop_str}\033[0m"
        lines.append(f"  {s['host']:<12} {s['bandwidth_mbps']:>9.2f} Mb/s"
                     f"  {s['rx_packets']:>10}"
                     f"  {drop_str:>8}"
                     f"  {s['total_drops']:>10}")
    lines.append("-" * 70)
    total_drop_str = str(total_drops)
    if total_drops > 0:
        total_drop_str = f"\033[91m{total_drop_str}\033[0m"
    lines.append(f"  {'TOTAL':<12} {total_bw:>9.2f} Mb/s"
                 f"  {total_pkts:>10}"
                 f"  {total_drop_str:>8}"
                 f"  {sum(s['total_drops'] for s in stats):>10}")
    lines.append("=" * 70)

    return lines, curr, curr_time


def blocking_input(prompt):
    """Read a line of input with normal terminal behavior (echo + line editing)."""
    old_settings = termios.tcgetattr(sys.stdin)
    try:
        termios.tcsetattr(sys.stdin, termios.TCSADRAIN, old_settings)
        # Restore canonical mode for line input
        new_settings = termios.tcgetattr(sys.stdin)
        new_settings[3] |= termios.ECHO | termios.ICANON
        termios.tcsetattr(sys.stdin, termios.TCSADRAIN, new_settings)
        return input(prompt).strip()
    finally:
        termios.tcsetattr(sys.stdin, termios.TCSADRAIN, old_settings)


def main():
    prev = read_counters()
    prev_time = time.time()
    message = "Initializing..."

    # Save original terminal settings
    old_settings = termios.tcgetattr(sys.stdin)

    try:
        # Set terminal to raw-like mode for single keypress detection
        tty.setcbreak(sys.stdin.fileno())

        while True:
            time.sleep(1)

            # Build the monitor table
            monitor_lines, curr, curr_time = build_monitor_table(prev, prev_time)
            prev = curr
            prev_time = curr_time

            # Clear and draw screen
            os.system('clear')

            print("===== iperf Traffic Generator =====")
            print(f"  Sampling every 1s on interface: {MONITOR_IFACE}")
            print()
            for line in monitor_lines:
                print(line)
            print()

            if message:
                print(f"  >> {message}")
                print()

            print("  1) Start traffic")
            print("  2) Stop traffic")
            print("  3) Show status")
            print("  4) Exit")
            print()
            print("  Press 1, 2, 3, or 4: ", end="", flush=True)

            # Non-blocking check for keypress (wait up to 1s)
            ready, _, _ = select.select([sys.stdin], [], [], 1)
            if not ready:
                continue

            key = sys.stdin.read(1)

            if key == "1":
                # Restore terminal for normal input
                termios.tcsetattr(sys.stdin, termios.TCSADRAIN, old_settings)
                os.system('clear')
                print("===== Start Traffic =====\n")
                try:
                    vip_input = input("  Destination IP(s) (comma-separated): ").strip()
                    if not vip_input:
                        raise ValueError("IP address cannot be empty")
                    vips = [ip.strip() for ip in vip_input.split(",") if ip.strip()]
                    if not vips:
                        raise ValueError("No valid IPs provided")
                    bandwidth = int(input("  Total bandwidth per destination in Mb/s: ").strip())
                    flows = int(input("  Number of flows per destination: ").strip())
                    start(flows, bandwidth, vips)
                    vip_list = ", ".join(vips)
                    message = f"Traffic started: {flows} flow(s) x {len(vips)} dest(s), {bandwidth} Mbps each to {vip_list}"
                except ValueError:
                    message = "Invalid input, please enter a number."
                except KeyboardInterrupt:
                    message = "Start cancelled."
                # Switch back to cbreak mode
                tty.setcbreak(sys.stdin.fileno())

            elif key == "2":
                stop()
                message = "Traffic stopped."

            elif key == "3":
                # Restore terminal, show status, wait for keypress to return
                termios.tcsetattr(sys.stdin, termios.TCSADRAIN, old_settings)
                os.system('clear')
                print("===== iperf Status =====\n")
                status()
                print("\nPress Enter to return...")
                input()
                tty.setcbreak(sys.stdin.fileno())
                message = ""

            elif key == "4":
                print("\nBye.")
                break

    except KeyboardInterrupt:
        print("\nBye.")
    finally:
        # Always restore terminal settings
        termios.tcsetattr(sys.stdin, termios.TCSADRAIN, old_settings)


if __name__ == "__main__":
    main()

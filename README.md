# ELI - EVPN L3 IP Aliasing Datacenter Lab

A containerlab-based datacenter fabric demonstrating advanced Nokia SR Linux EVPN features including centralized routing with IP aliasing, OSPF unnumbered underlay, iBGP EVPN overlay with dynamic neighbors, and policy-based routing for service insertion — all with a fully integrated telemetry and observability stack.

## Topology

```
                        +-----------+
                        | server-wan|
                        |123.0.0.1  |
                        +-----+-----+
                              |
                        +-----+-----+
                        |  wan-core |
                        |100.1.1.100|
                        +--+-----+--+
                           |     |
                     +-----+     +-----+
                     |                 |
                 +---+---+         +---+---+
                 |  pe1  |         |  pe2  |
                 +---+---+         +---+---+
                     |                 |
                 +---+----+        +---+----+
                 | b-leaf1|        | b-leaf2|
                 +--+-+---+        +--+-+---+
                    | |               | |
               fw1--+ |               | +--fw2
                      |               |
           +----------+----+   +------+--------+
           |    spine1      |   |     spine2     |
           +--+--+--+--+--++   ++--+--+--+--+--+
              |  |  |  |  |      |  |  |  |  |
         +----+  |  |  |  +------+  |  |  |  +----+
         |       |  |  |            |  |  |        |
      +--+--+ +--+--+ +--+--+  +---+-+ +--+--+ +--+--+
      |leaf1| |leaf3| |leaf5|  |leaf2| |leaf4| |leaf6|
      +--+--+ +--+--+ +--+--+  +--+--+ +--+--+ +--+--+
         |       |  |     |        |       |  |     |
         +--srv1-+  |     +--srv4--+       |  |     |
                 +--srv2--+            +--srv3--+
```

**Nodes:** 2 spines, 6 leaves, 2 border-leaves, 2 PE routers, 1 WAN core, 4 servers, 1 WAN server, 2 firewall VMs

All servers are dual-homed to leaf pairs via LACP bonds (all-active EVPN multi-homing).

## Quick Start

### Prerequisites

- [Containerlab](https://containerlab.dev/) installed
- Nokia SR Linux container image `ghcr.io/nokia/srlinux:25.10.2`
- Docker

### Deploy the Lab

```bash
cd eli
sudo clab deploy -t eli.clab.yaml
```

### Access Grafana Dashboard

Open [http://localhost:3000](http://localhost:3000) in your browser. The dashboard loads automatically with the topology view and telemetry panels. No login required (anonymous admin access enabled).

### Access Network Devices

```bash
# SSH into any SR Linux node
ssh admin@clab-eli-leaf1     # password: NokiaSrl1!

# Or use containerlab
sudo clab inspect -t eli.clab.yaml
```

## Use Cases

### 1. EVPN L3 IP Aliasing with Centralized Routing

This is the primary use case of the lab. It demonstrates how to achieve **optimal north-south and east-west routing** in a datacenter fabric using SR Linux's centralized routing model with EVPN IP aliasing.

#### The Problem

Servers (simulating Kubernetes nodes) announce their workload IP addresses (pod IPs) to the fabric via BGP. These workloads are dynamic — they can move between servers, which means BGP peering would need to shift from one leaf pair to another. Reconfiguring BGP peerings every time a workload moves is operationally expensive.

A common workaround is to extend a L2 broadcast domain across the fabric so workloads can peer with a single pair of border-leaves. However, this makes **east-west traffic suboptimal** because all inter-server traffic must be routed via the border-leaves (tromboning).

#### The Solution

The lab uses two SR Linux features to solve both problems at once:

- **Centralized Routing Model with PE-CE Routes Resolved over EVPN-IFL** — Leaf5 and leaf6 act as **anchor leaves** for the BGP control plane. All servers peer with these two leaves regardless of which leaf pair they are physically connected to. Servers use eBGP multihop (GoBGP, AS 11111) to announce their loopback VIP addresses (1.1.1.1, 2.2.2.2, 3.3.3.3) to the anchor leaves (AS 65111).

- **L3 ESI (EVPN IP Aliasing)** — Virtual Ethernet Segments are configured on each leaf pair where a server is physically connected. This allows the fabric to route traffic **directly to the correct leaf pair** without tromboning through the anchor leaves (leaf5/leaf6).

#### How It Works

| Server   | Physical Leaves | Bond IP      | Loopback VIP | BGP Peers (Anchor Leaves) |
|----------|-----------------|-------------|-------------|---------------------------|
| server1  | leaf1, leaf2    | 10.10.10.1  | 1.1.1.1     | leaf5 (1.0.0.1), leaf6 (2.0.0.2) |
| server2  | leaf3, leaf4    | 10.10.10.2  | 2.2.2.2     | leaf5 (1.0.0.1), leaf6 (2.0.0.2) |
| server3  | leaf3, leaf4    | 10.10.10.3  | 3.3.3.3     | leaf5 (1.0.0.1), leaf6 (2.0.0.2) |
| server4  | leaf5, leaf6    | 10.10.10.4  | 4.4.4.4     | leaf5 (1.0.0.1), leaf6 (2.0.0.2) |

Traffic from `server-wan` destined to `1.1.1.1` enters via the WAN core, reaches the border-leaves, and is routed **directly to leaf1/leaf2** (where server1 is physically connected) — not to leaf5/leaf6. This is optimal routing.

#### The Best of Both Worlds

| Approach | East-West Routing | Control Plane Stability | This Lab |
|----------|------------------|------------------------|----------|
| L2 stretch to border-leaves | Suboptimal (trombone) | Stable | No |
| Per-leaf BGP peering | Optimal | Requires reconfig on move | No |
| Centralized routing + IP aliasing | Optimal | Stable (anchor leaves) | Yes |

#### Try It

Run the traffic generator to send traffic to the server VIPs and observe the traffic flow in Grafana:

```bash
sudo python3 iperf_traffic.py
```

The interactive menu allows you to:
1. **Start traffic** — specify destination VIP(s) (1.1.1.1, 2.2.2.2, 3.3.3.3), bandwidth, and number of flows
2. **Stop traffic**
3. **Show iperf process status**

Watch the Grafana dashboard topology panel — traffic flows directly from the border-leaves to the correct leaf pair, bypassing the anchor leaves (leaf5/leaf6).

---

### 2. Policy-Based Routing for Firewall Service Insertion

This use case demonstrates how to steer traffic through firewall VMs using **policy-based forwarding (PBF)** and how to automate SR Linux configuration using **JSON-RPC** with Ansible.

#### Overview

Two firewall VMs (fw1, fw2) are connected to the border-leaves. Each firewall has two VLAN subinterfaces:
- **VLAN 1** (ingress): connected to `fw-ipvrf` (VNI 55)
- **VLAN 2** (egress): connected to `aliasing_l3` (VNI 100)

The Ansible playbook configures PBF policies on the border-leaves to redirect traffic matching a specific prefix (e.g., `2.2.2.2/32`) through the firewall VMs before delivering it to the destination.

#### Apply PBR Policy

```bash
cd ansible
ansible-playbook fw.yml
```

This configures the border-leaves via JSON-RPC to:
1. Create inter-instance import/export policies between `aliasing_l3` and `fw-ipvrf`
2. Create a PBF policy matching traffic to `2.2.2.2/32`
3. Bind the PBF policy to the WAN-facing interface (`ethernet-1/32.1`)
4. Traffic matching the prefix is redirected to the firewall next-hops (fw1: `10.0.10.1`, fw2: `10.0.10.2`) with load balancing

#### Observe in Grafana

Start traffic to `2.2.2.2` and watch the Grafana dashboard — traffic now flows through the firewall VMs before reaching the destination server.

#### Cleanup

```bash
ansible-playbook cleanup.yml
```

This removes all PBF policies and inter-instance routing policies, restoring direct routing.

## Underlay and Overlay Design

### OSPF Unnumbered Underlay

All fabric links use **OSPF unnumbered** interfaces — no point-to-point IP addresses are assigned. Each interface borrows the IP from `system0.0` (the loopback). This results in lean configuration and simplified IP address management.

```
# Example: leaf interface to spine
set / interface ethernet-1/49 subinterface 1 ipv4 unnumbered interface system0.0
```

| Node | Loopback (system0) | OSPF Area |
|------|-------------------|-----------|
| spine1 | 192.168.100.100 | 0.0.0.0 |
| spine2 | 192.168.100.200 | 0.0.0.0 |
| leaf1-6 | 192.168.100.1-6 | 0.0.0.0 |
| b-leaf1 | 192.168.100.11 | 0.0.0.0 |
| b-leaf2 | 192.168.100.12 | 0.0.0.0 |
| wan-core | 100.1.1.100 | 1.1.1.1 |
| pe1 | 100.1.1.101 | 1.1.1.1 |
| pe2 | 100.1.1.102 | 1.1.1.1 |

### iBGP EVPN Overlay with Dynamic Neighbors

The spines act as **iBGP EVPN route reflectors** (AS 65000). Dynamic neighbor acceptance is configured on both spines — any peer from AS 65000 is automatically accepted into the `fabric` peer group:

```
set / network-instance default protocols bgp dynamic-neighbors accept match 0.0.0.0/0 peer-group fabric
set / network-instance default protocols bgp dynamic-neighbors accept match 0.0.0.0/0 allowed-peer-as [ 65000 ]
```

When new leaf switches are introduced to the fabric, **no configuration changes are needed on the spines**. The new leaf simply peers with the spine loopbacks and is automatically accepted.

### Border-Leaf WAN Handover

Border-leaves peer with PE devices using **eBGP within the VRF** (VLAN handover):

- **DC side:** `aliasing_l3` VRF (AS 65111) with EVPN (VNI 100)
- **WAN side:** PE routers (AS 111) with EVPN (VNI 500)
- **Peering:** b-leaf1 (`100.0.0.0/31`) <-> pe1 (`100.0.0.1/31`)

## Telemetry Stack

The lab includes a complete observability pipeline:

| Component | Purpose | Access |
|-----------|---------|--------|
| **gNMIC** | gNMI telemetry collector (2s interval) | — |
| **Prometheus** | Metrics storage | [localhost:9090](http://localhost:9090) |
| **Grafana** | Visualization with topology flow panel | [localhost:3000](http://localhost:3000) |
| **Promtail** | Syslog collector (UDP 1514) | — |
| **Loki** | Log aggregation | [localhost:3100](http://localhost:3100) |

**Collected metrics:** CPU/memory, interface statistics, BGP state, route table stats, bridge/MAC tables, network instance state.

**Logs:** All SR Linux nodes send structured syslog to Promtail which forwards to Loki for querying in Grafana.

The Grafana dashboard includes a **topology flow panel** that visualizes real-time traffic throughput on each link with color-coded indicators.

## Project Structure

```
eli/
├── eli.clab.yaml                    # Containerlab topology definition
├── eli.clab.drawio                  # Network diagram (draw.io)
├── iperf_traffic.py                 # Interactive traffic generator
├── ansible/
│   ├── fw.yml                       # PBR firewall policy playbook
│   ├── cleanup.yml                  # Policy cleanup playbook
│   └── vars.yml                     # Ansible variables
├── configs/
│   ├── startup_configs/             # Device startup configurations
│   │   ├── spine1.cfg, spine2.cfg
│   │   ├── leaf1.cfg .. leaf6.cfg
│   │   ├── b-leaf1.cfg, b-leaf2.cfg
│   │   ├── pe1.cfg, pe2.cfg
│   │   ├── wan-core.cfg
│   │   └── gobgpd.toml             # GoBGP config for servers
│   ├── gnmic/gnmic-config.yml       # gNMI collector config
│   ├── prometheus/prometheus.yml    # Prometheus scrape config
│   ├── grafana/                     # Grafana dashboards and provisioning
│   │   ├── dashboards/eli-telemetry.json
│   │   └── flow_panels/topology.svg
│   ├── loki/loki-config.yml
│   └── promtail/promtail-config.yml
```

## Destroy the Lab

```bash
sudo clab destroy -t eli.clab.yaml
```

# 📡 Networking & Topology
Welcome to `#networking`! This channel is dedicated to the P2P architecture, WireGuard mesh, and node-to-node telemetry.

The Distributed LLM Engine relies on a decentralized, zero-trust network overlay so that personal machines can safely collaborate without port forwarding or exposing internal IP addresses.

### The WireGuard Mesh
- **Virtual Subnets:** The engine builds a lightweight virtual subnet using WireGuard. 
- **No Port Forwarding Required:** The network seamlessly traverses NATs and firewalls.
- **Encrypted Swarm Tunnels:** End-to-end network encryption via WireGuard VPN ensures the central relay/coordinator never sees raw model weights over the open internet. They are encrypted securely between shards inside the private tunnel.

### Heartbeats & Telemetry
- **Dynamic Failover:** Nodes ping the Coordinator with UDP/HTTP heartbeats. If a node drops out, the network instantly redistributes the compute load to other workers to prevent prompt failure.
- **Metrics Polling:** Bandwidth, VRAM temperature, and active loads are securely transmitted during the heartbeat cycle to update the Swarm map.

### Troubleshooting Network Issues
- Ensure UDP packets aren't completely blocked by your corporate/ISP firewall.
- If you notice heavy latency drops during token generation, verify your MTU sizes and check if you are dropping packets on the WireGuard interface.

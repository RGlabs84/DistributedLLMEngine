
Welcome to the DLNE Engine Room! For the engineers, developers, and tech enthusiasts, here is the deep-dive breakdown of how the Distributed Large-Language Network Engine actually operates under the hood.

### 🔹 1. Topology & The "Ghost Primary" Failover
DLNE operates on a **Highly Resilient Star Topology**. 
- **The Coordinator:** Acts as the central hub, running a high-performance FastAPI server. It manages the global state, aggregates swarm hardware, and handles the inference proxying.
- **Workers:** Establish a secure connection to the Coordinator and run headless instances. They continuously send authenticated heartbeats containing their active hardware profiles.
- **High Availability & Split-Brain Immunity:** Redundancy nodes continuously monitor the Primary Coordinator. If it goes offline, they promote themselves. However, if the original Coordinator comes back online, it utilizes a cryptographically signed **Ghost Primary Reclaimer** UDP broadcast. This instantly forces the temporary leader to safely step down and allows the network to re-acquire the true Primary, completely preventing split-brain cluster collapse.

### 🔹 2. The Tensor Splitting Engine & Spillover
DLNE does not just "run a model"—it intelligently shatters it.
- When a user requests an inference, the Coordinator dynamically chunks the Tensor Graph across the swarm via RPC calls. 
- **VRAM Pooling:** Layer 1-10 goes to Worker A (12GB), Layer 11-20 goes to Worker B (8GB).
- **Spillover Mode:** If the active swarm's collective VRAM is exhausted, the Coordinator automatically routes the remaining tensor layers into the collective System RAM of the swarm. It prioritizes GPU speed, but guarantees execution through CPU fallback.
- **Diffusion Draft Routing (DFlash):** If the model has a registered block-diffusion draft companion, the Coordinator maps the draft model to the swarm alongside the main model. Draft blocks are generated locally or on fast peripheral nodes, and shotgunned via RPC to the heavy nodes for parallel verification, drastically hiding network latency.
- If total memory requirements exceed the swarm's capacity, the network gracefully pauses and rejects new heavy models until resources free up.



### 🔹 3. Smart Yielding (Zero Disruption)
A core pillar of DLNE is respecting the hardware of the community.
- Workers utilize low-level OS hooks to monitor active window focus, game processes, and GPU usage in real-time.
- If a user boots a heavy game or rendering task, their node instantly flags its heartbeat with an active yielding status.
- The Coordinator detects this and immediately hot-swaps that node's tensor layers to the System RAM or idle VRAM of another node. The gamer feels zero performance hit, and the swarm experiences zero downtime.

### 🔹 4. Secure Swarm Tunnels & Non-Contrib Enforcement
Security is baked into the network layer.
- **Encrypted Traffic & VPNs:** While the local Coordinator orchestrates the tensors, remote traffic is secured via built-in WireGuard VPN integrations. Your data is encrypted in transit between swarm nodes, ensuring it remains mathematically unreadable gibberish to anyone listening on the open internet, and never touches a third-party cloud.
- **AES-GCM Local Vault:** The Coordinator holds zero persistent conversational data. The WebUI forces the client's browser to encrypt the multi-chat history using AES-GCM before writing it to local storage. Only the user holds the key to decrypt their own history.
- **The Non-Contrib Blocker:** DLNE strictly enforces a fair-use ecosystem via custom FastAPI middleware. If a user attempts to access the network or WebUI but is contributing 0 compute resources to the pool, the middleware outright rejects the connection with a `403 Forbidden` ban. 

### 🔹 5. The "Ollama Illusion" (API Mirroring)
- **Zero-Config Integrations:** The Coordinator natively hijacks port `11434` and mirrors the exact API structure used by OpenAI and Ollama (`/v1/chat/completions`). 
- **Transparent Sharding:** Third-party frontends like AnythingLLM or SillyTavern can point directly to the Coordinator as if it were a standard local Ollama instance. The frontend has no idea the payload is being intercepted and sharded across a massive distributed swarm!

### 🔹 6. Telemetry & Auto-Discovery
- **Instant Bootstrapping:** The Coordinator continuously broadcasts an authenticated UDP packet. New workers on the network instantly auto-discover the Hub without manual IP configuration.
- **Live SSE Streaming:** The Coordinator pipes Server-Sent Events (SSE) directly from the internal inference engine, providing the WebUI and Admin Portal with real-time, per-node VRAM loading percentages.

# ❓ Frequently Asked Questions (FAQ)

**Q: Is DLNE completely free?**
A: Yes! The network is powered by the community. As long as you contribute some level of hardware (even just CPU), you get access to the network's collective power via the Contribution Clause.

**Q: Will this slow down my PC?**
A: No. DLNE features Smart Yielding. If you open a heavy application or game, the node instantly yields its workload to other nodes in the network, so your PC stays fast and you experience zero disruption.

**Q: What if I have an older GPU or a non-NVIDIA card? Can I still participate?**
A: Absolutely! We support a massive suite of hardware.
- **NVIDIA:** Full native CUDA support for maximum inference speed.
- **AMD & Intel (Vulkan):** We utilize a highly optimized Vulkan backend, meaning nearly any AMD Radeon card, Intel Arc GPU, or even modern integrated graphics chips can power the network.
- **Multi-GPU / Mining Rigs:** You can easily map specific cards or dictate precise VRAM allocations per GPU. Got an old 6-card mining rig gathering dust? Hook it up to the swarm!

**Q: What if I don't have a GPU at all?**
A: You can still join as a Worker! DLNE utilizes System RAM and CPU threads for fallback compute and redundancy. When the swarm's VRAM fills up, the network dynamically "spills over" into the collective RAM pool. 
*Note: Because CPU-only nodes are significantly slower, DLNE imposes a strict minimum requirement to ensure they don't bottleneck the network. If you are not contributing a GPU, your node MUST allocate at least **8GB of RAM** and **2 Physical CPU Cores** to join the swarm.*

**Q: How is my privacy protected?**
A: All chat data and prompts are encrypted via military-grade WireGuard VPN tunnels before they are ever sent across the network. The network only routes encrypted packets, ensuring your data never touches a public cloud like OpenAI.

**Q: Are my conversations saved on the network?**
A: No. DLNE utilizes zero-knowledge client-side persistence. Your chat history is encrypted using military-grade AES-GCM and stored locally in your own browser. The central Coordinator processes the tensors, but it has no permanent memory of your conversations.

**Q: Can I use third-party apps like AnythingLLM or SillyTavern?**
A: Yes! The DLNE Coordinator acts as a **zero-configuration drop-in replacement for Ollama**. It natively exposes the Ollama API structure on port `11434`. Just point your favorite frontend to the Coordinator, and it will instantly utilize the entire distributed swarm!

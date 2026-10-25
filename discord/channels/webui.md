# 🌐 Coordinator WebUI
Welcome to `#webui`! This channel is for discussing the frontend, dashboard analytics, and UX of the Coordinator Hub.

The Distributed LLM Engine comes with a built-in React/FastAPI WebUI. If you are running a `Coordinator` or a `Hybrid` node, you are hosting the hub interface. 

### 👑 Admin / Coordinator Dashboard
*Hosted locally for the Swarm Coordinator.*
- **Swarm Topology Map:** A live visualizer of the mesh network. Watch Worker nodes connect, disconnect, and process payloads in real-time.
- **Model Deployment Library:** Instantly download, shard, and deploy models across the network with one click.
- **Global Hardware Metrics:** Track the Swarm's total pooled VRAM, CPU Spillover RAM, physical core count, and generation speeds (Tokens/second).

### 👥 End User / Worker Interface
*Accessible via the Coordinator's public proxy IP or domain.*
- **Inference Chat:** A sleek, familiar chat interface (similar to ChatGPT) where you can interact with the deployed models.
- **Personal Metrics:** Workers can view their node status and verify their connection to the swarm.
- **Third-Party Integrations:** Don't want to use our WebUI? Because DLNE natively mimics the **Ollama API** on port `11434`, you can completely bypass our frontend and plug directly into your own apps like AnythingLLM or SillyTavern!



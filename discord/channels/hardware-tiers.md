# 🖥️ DLNE Hardware Tiers & Redistributables

To maximize performance while ensuring every single GPU released in the last decade can join the swarm, DLNE uses a segmented binary distribution model.

```text
=======================================================================
               DLNE DISTRIBUTED NODE RELEASES (CUDA)
=======================================================================

🟢 DLNE Modern Tier (CUDA Latest)
   For modern rigs. Uses cutting-edge compiler optimizations for 
   maximum Tokens/sec throughput. Requires recent NVIDIA drivers.
   
   [Supported GPU Architectures]
   ├─ Ampere   (RTX 3000 Series)
   ├─ Lovelace (RTX 4000 Series)
   └─ Hopper   (RTX 5000 / H100)

-----------------------------------------------------------------------

🟡 DLNE Legacy Tier (CUDA 11.8)
   The fallback tier. Built for older rigs or users who haven't 
   updated their NVIDIA drivers in a while. Maximum compatibility.

   [Supported GPU Architectures]
   ├─ Maxwell  (GTX 900 Series)
   ├─ Pascal   (GTX 1000 Series)
   ├─ Volta    (Titan V)
   └─ Turing   (RTX 2000 / GTX 1600)

=======================================================================
```

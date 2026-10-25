# CUDA Architecture Split in DLNE

To address the sheer size of the compiled binaries and the broad spectrum of NVIDIA GPUs supported by DLNE, we have implemented a clean split between our CUDA compilation targets. This split optimizes compilation times, reduces bloated file sizes, and ensures maximum compatibility across hardware generations.

The split is divided into two distinct builds: **CUDA 11.8 (Legacy Compatibility)** and **CUDA Latest (13.3)**.

## CUDA 11.8 (Legacy Compatibility)
This build is compiled with the older CUDA 11.8 toolkit to maintain compatibility with older drivers and hardware. It explicitly targets the following NVIDIA GPU architectures:
*   **Maxwell**: GTX 900 series, GTX 750
*   **Pascal**: GTX 1000 series
*   **Volta**: Titan V, V100
*   **Turing**: RTX 2000 series, GTX 1600 series

## CUDA Latest (13.3)
This build utilizes the latest available CUDA toolkit to take full advantage of modern features (like Flash Attention optimizations and newer PTX instructions) on recent hardware. It drops legacy architecture support and explicitly targets:
*   **Ampere (Data Center)**: A100
*   **Ampere (Consumer)**: RTX 3000 series
*   **Ada Lovelace**: RTX 4000 series
*   **Hopper**: H100
*   **Blackwell**: RTX 5000 series, B100/B200

## Why the Split?
1.  **Binary Size:** Compiling a single backend binary that contains PTX and SASS code for *every* architecture results in a massive binary that takes exceptionally long to compile, chunk, and distribute.
2.  **Toolkit Deprecation:** Newer CUDA toolkits explicitly drop support for compiling older architectures (like Maxwell). We *must* use an older toolkit like 11.8 to compile them natively without errors.
3.  **Performance:** By splitting the targets, modern RTX 3000/4000/5000 series owners can download a sleek binary compiled with the absolute latest toolkit, while GTX 1000 owners get a stable, legacy-compatible build perfectly tailored for their cards.

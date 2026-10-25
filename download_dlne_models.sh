#!/bin/bash
# DLNE Full Model Library Downloader
# Auto-generated script for Arch Linux
# Requires: wget (sudo pacman -S wget)
#
# NOTE: Make sure you have ~1.3 TB of free space. Some 100B+ parameter models 
# may return 401/404 if HuggingFace requires a gated acceptance or the model was removed.

mkdir -p models
cd models || exit

echo "Starting DLNE Model Library Download..."
echo "Files will be saved directly into the 'models' directory."
echo ""

echo "Downloading qwen2.5-coder:0.5b..."
wget -c --show-progress "https://huggingface.co/Qwen/Qwen2.5-Coder-0.5B-Instruct-GGUF/resolve/main/qwen2.5-coder-0.5b-instruct-q4_k_m.gguf" -O "qwen2.5-coder-0.5b.gguf"
echo ""

echo "Downloading llama3.2:1b..."
wget -c --show-progress "https://huggingface.co/bartowski/Llama-3.2-1B-Instruct-GGUF/resolve/main/Llama-3.2-1B-Instruct-Q4_K_M.gguf" -O "llama3.2-1b.gguf"
echo ""

echo "Downloading qwen2.5:1.5b..."
wget -c --show-progress "https://huggingface.co/Qwen/Qwen2.5-1.5B-Instruct-GGUF/resolve/main/qwen2.5-1.5b-instruct-q4_k_m.gguf" -O "qwen2.5-1.5b.gguf"
echo ""

echo "Downloading gemma2:2b..."
wget -c --show-progress "https://huggingface.co/bartowski/gemma-2-2b-it-GGUF/resolve/main/gemma-2-2b-it-Q4_K_M.gguf" -O "gemma2-2b.gguf"
echo ""

echo "Downloading phi3:mini..."
wget -c --show-progress "https://huggingface.co/microsoft/Phi-3-mini-4k-instruct-gguf/resolve/main/Phi-3-mini-4k-instruct-q4.gguf" -O "phi3-mini.gguf"
echo ""

echo "Downloading qwen3:4b..."
wget -c --show-progress "https://huggingface.co/Qwen/Qwen2.5-3B-Instruct-GGUF/resolve/main/qwen2.5-3b-instruct-q4_k_m.gguf" -O "qwen3-4b.gguf"
echo ""

echo "Downloading gemma3:4b..."
wget -c --show-progress "https://huggingface.co/bartowski/gemma-2-9b-it-GGUF/resolve/main/gemma-2-9b-it-Q4_K_M.gguf" -O "gemma3-4b.gguf"
echo ""

echo "Downloading llama3.2:3b..."
wget -c --show-progress "https://huggingface.co/bartowski/Llama-3.2-3B-Instruct-GGUF/resolve/main/Llama-3.2-3B-Instruct-Q4_K_M.gguf" -O "llama3.2-3b.gguf"
echo ""

echo "Downloading phi4:mini..."
wget -c --show-progress "https://huggingface.co/bartowski/Phi-3.5-mini-instruct-GGUF/resolve/main/Phi-3.5-mini-instruct-Q4_K_M.gguf" -O "phi4-mini.gguf"
echo ""

echo "Downloading llama3.1:8b..."
wget -c --show-progress "https://huggingface.co/lmstudio-community/Meta-Llama-3.1-8B-Instruct-GGUF/resolve/main/Meta-Llama-3.1-8B-Instruct-Q4_K_M.gguf" -O "llama3.1-8b.gguf"
echo ""

echo "Downloading mistral:7b..."
wget -c --show-progress "https://huggingface.co/TheBloke/Mistral-7B-Instruct-v0.2-GGUF/resolve/main/mistral-7b-instruct-v0.2.Q4_K_M.gguf" -O "mistral-7b.gguf"
echo ""

echo "Downloading qwen2.5:14b..."
wget -c --show-progress "https://huggingface.co/bartowski/Qwen2.5-14B-Instruct-GGUF/resolve/main/Qwen2.5-14B-Instruct-Q4_K_M.gguf" -O "qwen2.5-14b.gguf"
echo ""

echo "Downloading phi4:14b..."
wget -c --show-progress "https://huggingface.co/bartowski/phi-4-GGUF/resolve/main/phi-4-Q4_K_M.gguf" -O "phi4-14b.gguf"
echo ""

echo "Downloading mixtral:8x7b..."
wget -c --show-progress "https://huggingface.co/TheBloke/Mixtral-8x7B-Instruct-v0.1-GGUF/resolve/main/mixtral-8x7b-instruct-v0.1.Q4_K_M.gguf" -O "mixtral-8x7b.gguf"
echo ""

echo "Downloading qwen2.5:32b..."
wget -c --show-progress "https://huggingface.co/bartowski/Qwen2.5-32B-Instruct-GGUF/resolve/main/Qwen2.5-32B-Instruct-Q4_K_M.gguf" -O "qwen2.5-32b.gguf"
echo ""

echo "Downloading llama3:70b..."
wget -c --show-progress "https://huggingface.co/lmstudio-community/Meta-Llama-3.1-70B-Instruct-GGUF/resolve/main/Meta-Llama-3.1-70B-Instruct-Q4_K_M.gguf" -O "llama3-70b.gguf"
echo ""

echo "Downloading qwen2.5:72b..."
wget -c --show-progress "https://huggingface.co/bartowski/Qwen2.5-72B-Instruct-GGUF/resolve/main/Qwen2.5-72B-Instruct-Q4_K_M.gguf" -O "qwen2.5-72b.gguf"
echo ""

echo "Downloading command-r-plus:104b..."
wget -c --show-progress "https://huggingface.co/pmysl/c4ai-command-r-plus-GGUF/resolve/main/command-r-plus-Q3_K_S.gguf" -O "command-r-plus-104b.gguf"
echo ""

echo "Downloading gemma4:12b..."
wget -c --show-progress "https://huggingface.co/bartowski/gemma-2-27b-it-GGUF/resolve/main/gemma-2-27b-it-Q4_K_M.gguf" -O "gemma4-12b.gguf"
echo ""

echo "Downloading deepseek-coder-v2:16b..."
wget -c --show-progress "https://huggingface.co/bartowski/DeepSeek-Coder-V2-Lite-Instruct-GGUF/resolve/main/DeepSeek-Coder-V2-Lite-Instruct-Q4_K_M.gguf" -O "deepseek-coder-v2-16b.gguf"
echo ""

echo "Downloading deepseek-coder-v2:236b..."
wget -c --show-progress "https://huggingface.co/mradermacher/DeepSeek-Coder-V2-Instruct-GGUF/resolve/main/DeepSeek-Coder-V2-Instruct.Q4_K_M.gguf" -O "deepseek-coder-v2-236b.gguf"
echo ""

echo "Downloading north-mini-code-1.0:latest..."
wget -c --show-progress "https://huggingface.co/Qwen/Qwen2.5-Coder-1.5B-Instruct-GGUF/resolve/main/qwen2.5-coder-1.5b-instruct-q4_k_m.gguf" -O "north-mini-code-1.0-latest.gguf"
echo ""

echo "Downloading qwen3.6:27b..."
wget -c --show-progress "https://huggingface.co/Qwen/Qwen2.5-32B-Instruct-GGUF/resolve/main/qwen2.5-32b-instruct-q4_k_m.gguf" -O "qwen3.6-27b.gguf"
echo ""

echo "Downloading llama3.1:405b..."
wget -c --show-progress "https://huggingface.co/mradermacher/Meta-Llama-3.1-405B-Instruct-GGUF/resolve/main/Meta-Llama-3.1-405B-Instruct.Q4_K_M.gguf" -O "llama3.1-405b.gguf"
echo ""

echo "Downloading nemotron-4:340b..."
wget -c --show-progress "https://huggingface.co/mradermacher/Nemotron-4-340B-Instruct-GGUF/resolve/main/Nemotron-4-340B-Instruct.Q4_K_M.gguf" -O "nemotron-4-340b.gguf"
echo ""

echo "Downloading grok-1:314b..."
wget -c --show-progress "https://huggingface.co/mradermacher/grok-1-GGUF/resolve/main/grok-1.Q4_K_M.gguf" -O "grok-1-314b.gguf"
echo ""

echo "Downloading mistral-large:123b..."
wget -c --show-progress "https://huggingface.co/mradermacher/Mistral-Large-Instruct-2407-GGUF/resolve/main/Mistral-Large-Instruct-2407.Q4_K_M.gguf" -O "mistral-large-123b.gguf"
echo ""

echo "Downloading wizardlm2:8x22b..."
wget -c --show-progress "https://huggingface.co/mradermacher/WizardLM-2-8x22B-GGUF/resolve/main/WizardLM-2-8x22B.Q4_K_M.gguf" -O "wizardlm2-8x22b.gguf"
echo ""


echo "All downloads finished!"

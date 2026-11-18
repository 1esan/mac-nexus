# >>> token-hygiene xdg >>>
export XDG_CONFIG_HOME="$HOME/.config"
export XDG_CACHE_HOME="$HOME/.cache"
export XDG_DATA_HOME="$HOME/.local/share"
export XDG_STATE_HOME="$HOME/.local/state"
export UV_CACHE_DIR="$HOME/.cache/uv"

case ":$PATH:" in
  *":$HOME/.local/bin:"*) ;;
  *) export PATH="$HOME/.local/bin:$PATH" ;;
esac

if [[ -d "/Volumes/secret for mine/01_AI_Models_and_Caches" ]]; then
  export HF_HOME="/Volumes/secret for mine/01_AI_Models_and_Caches/huggingface"
  export TORCH_HOME="/Volumes/secret for mine/01_AI_Models_and_Caches/torch"
  export OLLAMA_MODELS="/Volumes/secret for mine/ollama_models"
elif [[ -d "/Volumes/secret for mine/ollama_models" ]]; then
  export OLLAMA_MODELS="/Volumes/secret for mine/ollama_models"
fi
# <<< token-hygiene xdg <<<

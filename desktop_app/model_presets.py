"""Editable connection presets; never contain or fetch user credentials."""
from urllib.parse import urlsplit

PRESETS = {
    "deepseek": {
        "label": "DeepSeek", "base_url": "https://api.deepseek.com",
        "models": ("deepseek-flash", "deepseek-v4-pro"),
        "vision_models": ("deepseek-flash",), "vision_model": "deepseek-flash",
        "note": "已预填官方地址和常用模型，也可以改为自己的服务地址。",
        "source": "https://api-docs.deepseek.com/",
    },
    "kimi": {
        "label": "Kimi（月之暗面）", "base_url": "https://api.moonshot.cn/v1",
        "models": ("kimi-k2.6", "kimi-k3"),
        "vision_models": ("kimi-k2.6", "kimi-k3"), "vision_model": "kimi-k2.6",
        "note": "填写 Kimi 开放平台的 Key；模型名称可按账号权限自行修改。",
        "source": "https://platform.kimi.com/docs/get-api-key",
    },
    "doubao": {
        "label": "Doubao 豆包（火山方舟）", "base_url": "https://ark.cn-beijing.volces.com/api/v3",
        "models": ("doubao-seed-2-0-pro-260215", "doubao-seed-2-0-lite-260215", "doubao-seed-2-0-mini-260215"),
        "vision_models": ("doubao-seed-2-0-pro-260215", "doubao-seed-2-0-lite-260215", "doubao-seed-2-0-mini-260215"),
        "vision_model": "doubao-seed-2-0-pro-260215",
        "note": "填写火山方舟 Key；也可将模型名称替换为控制台中的 ep- 开头接入点 ID。",
        "source": "https://console.volcengine.com/ark/region%3Acn-beijing/docs/agent-model-invocation-practice",
    },
    "qwen": {
        "label": "Qwen 通义千问（阿里云百炼）", "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "models": ("qwen-plus", "qwen-max", "qwen3-vl-plus"),
        "vision_models": ("qwen3-vl-plus",), "vision_model": "qwen3-vl-plus",
        "note": "预置北京地域地址。其他地域或专属业务空间请改为控制台提供的 base_url，并使用对应地域的 Key。",
        "source": "https://help.aliyun.com/zh/model-studio/compatibility-of-openai-with-dashscope",
    },
    "custom": {
        "label": "自定义 / 其他兼容服务", "base_url": "", "models": (),
        "vision_models": (), "vision_model": "",
        "note": "填写兼容 Chat Completions 的服务地址和模型 ID；支持本机模型及自建网关。",
        "source": "",
    },
}


def infer_provider(profile):
    if profile.get("provider_id") in PRESETS:
        return profile["provider_id"]
    base = profile.get("base_url", "")
    if not base:
        return "deepseek"
    host = urlsplit(base).hostname or ""
    if host == "api.deepseek.com": return "deepseek"
    if host in {"api.moonshot.cn", "api.moonshot.ai"}: return "kimi"
    if host == "ark.cn-beijing.volces.com": return "doubao"
    if host in {"dashscope.aliyuncs.com", "dashscope-intl.aliyuncs.com"} or host.endswith(".maas.aliyuncs.com"): return "qwen"
    return "custom"


def preset_profile(provider):
    preset = PRESETS[provider]
    return {"provider_id": provider, "base_url": preset["base_url"],
            "text_model": next(iter(preset["models"]), ""), "vision_model": preset["vision_model"],
            "review_model": "", "review_vision_model": "", "auto_supplements": True,
            "enable_model": True, "local_http": False}


def initial_profile(saved):
    # Existing user values, including an explicitly empty vision model, win.
    provider = infer_provider(saved)
    return {**preset_profile(provider), **saved, "provider_id": provider}

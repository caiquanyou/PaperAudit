"""User-selected Chat Completions adapter; failures never become low-risk results."""
from __future__ import annotations

import base64
import json
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


class ModelError(RuntimeError):
    def __init__(self,code,usage=None):
        super().__init__(code)
        self.usage=usage or {}


def endpoint_url(base, local_http=False):
    parts=urllib.parse.urlsplit(base)
    if parts.username or parts.password or parts.query or parts.fragment or not parts.hostname:
        raise ValueError("模型地址不得含凭据、查询或片段")
    if parts.scheme!="https" and not (local_http and parts.scheme=="http" and parts.hostname in {"localhost","127.0.0.1","::1"}):
        raise ValueError("模型地址需HTTPS；明确启用本机模型后仅允许回环HTTP")
    return base.rstrip("/")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs):
        return None


class ModelClient:
    def __init__(self,profile,key):
        self.profile,self.key=profile,key
        self.base=endpoint_url(profile["base_url"],profile.get("local_http",False))

    def request(self,path,payload=None):
        raw=json.dumps(payload,ensure_ascii=False).encode() if payload is not None else None
        request=urllib.request.Request(self.base+path,data=raw,headers={"Authorization":"Bearer "+self.key,"Content-Type":"application/json"},method="POST" if raw else "GET")
        try:
            with urllib.request.build_opener(NoRedirect()).open(request,timeout=90 if raw else 15) as response:
                data=response.read(6*1024*1024+1)
                if len(data)>6*1024*1024:
                    raise ModelError("response_too_large")
            return json.loads(data)
        except urllib.error.HTTPError as exc:
            raise ModelError("http_"+str(exc.code)) from None
        except (urllib.error.URLError,TimeoutError):
            raise ModelError("network_or_timeout_billing_uncertain") from None
        except (ValueError,KeyError,TypeError):
            raise ModelError("invalid_response") from None

    def list_models(self):
        data=self.request("/models")
        return [v["id"] for v in data.get("data",[]) if isinstance(v,dict) and isinstance(v.get("id"),str)][:500]

    def generation_options(self):
        # DeepSeek currently defaults to high-effort thinking, which shares
        # the output cap with the final JSON. Use an explicit structured mode.
        if urllib.parse.urlsplit(self.base).hostname=="api.deepseek.com":
            return {"max_tokens":8192,"thinking":{"type":"disabled"},"response_format":{"type":"json_object"}}
        return {"max_tokens":4096}

    def chat(self,system,text,model,images=()):
        content=[{"type":"text","text":text}]
        for path in images:
            raw=Path(path).read_bytes()
            if len(raw)>6*1024*1024:
                raise ModelError("image_too_large")
            content.append({"type":"image_url","image_url":{"url":"data:image/png;base64,"+base64.b64encode(raw).decode()}})
        data=self.request("/chat/completions",{"model":model,"messages":[{"role":"system","content":system},{"role":"user","content":content if images else text}],**self.generation_options(),"stream":False})
        try:
            choice=data["choices"][0]
            if choice.get("finish_reason") in {"length","content_filter"}:
                raise ModelError("response_incomplete",data.get("usage",{}))
            text=choice["message"]["content"]
            if text.strip().startswith("```"):
                text="\n".join(text.strip().splitlines()[1:-1])
            parsed=json.loads(text)
            if not isinstance(parsed,dict): raise ValueError()
            return parsed,data.get("usage",{})
        except (ValueError,KeyError,TypeError,IndexError,AttributeError):
            raise ModelError("invalid_model_json",data.get("usage",{})) from None

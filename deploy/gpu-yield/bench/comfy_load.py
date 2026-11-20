#!/usr/bin/env python3
"""t_0930e064: drive a ComfyUI instance with a fixed Anima text-to-image job, back to back.
usage: comfy_load.py <port> <label> <out.jsonl> <stop_file> [max_jobs]
Job: anima-aesthetic-v1.1 (UNET) + qwen_3_06b text encoder + qwen_image_vae, 1280x720, batch 4,
30 steps er_sde/simple, cfg 4.0 (the image stage of Ace's animegen workflow). Seed alternates 20261119/20261120
by job parity so ComfyUI's node cache never skips the sampler; outputs are compared per seed.
Per job: wall s and sha256 of every output image's decoded pixels (PNG metadata excluded). Runs until <stop_file> exists or max_jobs done."""
import hashlib, http.client, json, os, sys, time, uuid

from PIL import Image

port, label, out, stop = int(sys.argv[1]), sys.argv[2], sys.argv[3], sys.argv[4]
max_jobs = int(sys.argv[5]) if len(sys.argv) > 5 else 10**6
POS = ("masterpiece, best quality, anime screencap, 2D cel animation, clean line art, flat colors, an adult woman "
       "in a long coat walking through a rainy night market, paper lanterns, wet reflective street, food stalls")
NEG = "worst quality, low quality, score_1, score_2, score_3, blurry, jpeg artifacts, sepia, 3d render, cgi, realistic, photo"


def prompt(tag, seed):
    return {
        "1": {"class_type": "UNETLoader", "inputs": {"unet_name": "anima-aesthetic-v1.1.safetensors", "weight_dtype": "default"}},
        "2": {"class_type": "CLIPLoader", "inputs": {"clip_name": "qwen_3_06b_base.safetensors", "type": "stable_diffusion", "device": "default"}},
        "3": {"class_type": "VAELoader", "inputs": {"vae_name": "qwen_image_vae.safetensors"}},
        "4": {"class_type": "CLIPTextEncode", "inputs": {"text": POS, "clip": ["2", 0]}},
        "5": {"class_type": "CLIPTextEncode", "inputs": {"text": NEG, "clip": ["2", 0]}},
        "6": {"class_type": "EmptyLatentImage", "inputs": {"width": 1280, "height": 720, "batch_size": 4}},
        "7": {"class_type": "KSampler", "inputs": {"seed": seed, "steps": 30, "cfg": 4.0, "sampler_name": "er_sde",
                                                   "scheduler": "simple", "denoise": 1.0, "model": ["1", 0],
                                                   "positive": ["4", 0], "negative": ["5", 0], "latent_image": ["6", 0]}},
        "8": {"class_type": "VAEDecode", "inputs": {"samples": ["7", 0], "vae": ["3", 0]}},
        "9": {"class_type": "SaveImage", "inputs": {"filename_prefix": f"t0930/{tag}", "images": ["8", 0]}},
    }


def call(method, path, body=None):
    """JSON call to the ComfyUI instance on 127.0.0.1:<port>; host and scheme are fixed."""
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    conn.request(method, path, body=body, headers={"content-type": "application/json"} if body else {})
    r = conn.getresponse()
    data = r.read()
    conn.close()
    if r.status != 200:
        sys.exit(f"{method} {path}: HTTP {r.status}")
    return json.loads(data)


def get(path):
    return call("GET", path)


outdir = os.environ.get("COMFY_OUT", "/home/ace/t_0930e064/comfy/output")
n = 0
with open(out, "a") as f:
    while n < max_jobs and not os.path.exists(stop):
        tag = f"{label}-{n}"
        seed = 20261119 + (n % 2)
        body = json.dumps({"prompt": prompt(tag, seed), "client_id": str(uuid.uuid4())}).encode()
        t = time.time()
        pid = call("POST", "/prompt", body)["prompt_id"]
        while True:
            h = get(f"/history/{pid}")
            if pid in h and h[pid].get("status", {}).get("completed") is not None:
                break
            time.sleep(0.2)
        wall = time.time() - t
        st = h[pid]["status"]
        imgs = h[pid].get("outputs", {}).get("9", {}).get("images", [])
        shas = []
        for im in imgs:
            p = os.path.join(outdir, im.get("subfolder", ""), im["filename"])
            shas.append(hashlib.sha256(Image.open(p).tobytes()).hexdigest()[:16])
        f.write(json.dumps({"label": label, "job": n, "t0": t, "t1": time.time(), "seed": seed, "wall_s": round(wall, 2),
                            "status": st.get("status_str"), "sha": shas}) + "\n")
        f.flush()
        print(f"{label} job {n} {wall:.1f}s {st.get('status_str')} {shas}", flush=True)
        n += 1

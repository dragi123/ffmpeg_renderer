import os
import subprocess
from flask import Flask, request, jsonify

app = Flask(__name__)

API_KEY = os.environ.get("API_KEY", "")

def check_key(req):
    # API_KEY를 안 쓰면(빈 값) 인증 없이 통과
    if not API_KEY:
        return True
    return req.headers.get("X-API-Key", "") == API_KEY

@app.get("/health")
def health():
    return "ok", 200

@app.post("/render")
def render():
    if not check_key(request):
        return jsonify({"ok": False, "error": "unauthorized"}), 401

    data = request.get_json(force=True) or {}
    audio = data.get("audio")
    videos = data.get("videos", [])
    output = data.get("output")

    if not audio or not output or not isinstance(videos, list) or len(videos) == 0:
        return jsonify({"ok": False, "error": "payload must include audio, videos[], output"}), 400

    if not audio.startswith("gs://") or not output.startswith("gs://"):
        return jsonify({"ok": False, "error": "audio/output must be gs://..."}), 400
    for v in videos:
        if not isinstance(v, str) or not v.startswith("gs://"):
            return jsonify({"ok": False, "error": "videos must be gs://... list"}), 400

    workdir = "/tmp/work"
    os.makedirs(workdir, exist_ok=True)

    # 1) GCS에서 로컬로 받기
    local_audio = os.path.join(workdir, "audio.mp3")
    subprocess.check_call(["gsutil", "-q", "cp", audio, local_audio])

    local_videos = []
    for i, v in enumerate(videos):
        p = os.path.join(workdir, f"video_{i}.mp4")
        subprocess.check_call(["gsutil", "-q", "cp", v, p])
        local_videos.append(p)

    # 2) concat 리스트 만들기
    concat_txt = os.path.join(workdir, "concat.txt")
    with open(concat_txt, "w", encoding="utf-8") as f:
        for p in local_videos:
            f.write(f"file '{p}'\n")

    merged = os.path.join(workdir, "merged.mp4")
    final = os.path.join(workdir, "final.mp4")

    # 3) 영상 concat (코덱이 동일하면 copy로 빠르게 합쳐짐)
    subprocess.check_call([
        "ffmpeg", "-y",
        "-f", "concat", "-safe", "0",
        "-i", concat_txt,
        "-c", "copy",
        merged
    ])

    # 4) 오디오 mux (오디오 aac로 인코딩, 영상은 copy)
    subprocess.check_call([
        "ffmpeg", "-y",
        "-i", merged,
        "-i", local_audio,
        "-c:v", "copy",
        "-c:a", "aac",
        "-shortest",
        final
    ])

    # 5) 결과 업로드
    subprocess.check_call(["gsutil", "-q", "cp", final, output])

    return jsonify({"ok": True, "output": output, "videoCount": len(videos)}), 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8080"))
    app.run(host="0.0.0.0", port=port)

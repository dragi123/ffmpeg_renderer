import os
import json
import subprocess
import tempfile

from flask import Flask, request, jsonify
from google.cloud import storage

app = Flask(__name__)

# ---------- 기본 엔드포인트 ----------
@app.get("/")
def root():
    return "ffmpeg-renderer up", 200


@app.get("/health")
def health():
    return "ok", 200


# ---------- 핵심 렌더 엔드포인트 ----------
@app.post("/render")
def render():
    try:
        # 1) 요청 JSON 받기
        data = request.get_json(force=True, silent=True)

        # n8n에서 stringified JSON으로 오는 경우 대비
        if isinstance(data, str):
            data = json.loads(data)

        if not isinstance(data, dict):
            return jsonify({"ok": False, "error": "Invalid JSON payload"}), 400

        audio = data.get("audio")
        videos = data.get("videos")
        output = data.get("output")

        if not audio or not videos or not output:
            return jsonify({
                "ok": False,
                "error": "payload must include audio, videos[], output"
            }), 400

        if not isinstance(videos, list):
            return jsonify({
                "ok": False,
                "error": "videos must be an array"
            }), 400

        # 2) GCS 클라이언트
        client = storage.Client()

        def download_gs(gs_path: str, local_path: str):
            if not gs_path.startswith("gs://"):
                raise ValueError(f"Invalid GCS path: {gs_path}")
            bucket_name, blob_name = gs_path.replace("gs://", "").split("/", 1)
            bucket = client.bucket(bucket_name)
            blob = bucket.blob(blob_name)
            blob.download_to_filename(local_path)

        def upload_gs(local_path: str, gs_path: str):
            if not gs_path.startswith("gs://"):
                raise ValueError(f"Invalid GCS path: {gs_path}")
            bucket_name, blob_name = gs_path.replace("gs://", "").split("/", 1)
            bucket = client.bucket(bucket_name)
            blob = bucket.blob(blob_name)
            blob.upload_from_filename(local_path, content_type="video/mp4")

        # 3) 임시 디렉토리에서 작업
        with tempfile.TemporaryDirectory() as tmpdir:
            audio_path = os.path.join(tmpdir, "audio.mp3")
            download_gs(audio, audio_path)

            video_paths = []
            for i, v in enumerate(videos):
                vp = os.path.join(tmpdir, f"video_{i}.mp4")
                download_gs(v, vp)
                video_paths.append(vp)

            # concat 리스트 파일 생성
            concat_list = os.path.join(tmpdir, "concat.txt")
            with open(concat_list, "w", encoding="utf-8") as f:
                for vp in video_paths:
                    f.write(f"file '{vp}'\n")

            merged_video = os.path.join(tmpdir, "merged.mp4")
            final_video = os.path.join(tmpdir, "final.mp4")

            # 4) 영상 concat (재인코딩, 안정 모드)
            subprocess.check_call([
                "ffmpeg", "-y",
                "-f", "concat",
                "-safe", "0",
                "-i", concat_list,
                "-c:v", "libx264",
                "-preset", "fast",
                "-pix_fmt", "yuv420p",
                merged_video
            ])

            # 5) 오디오 합성
            subprocess.check_call([
                "ffmpeg", "-y",
                "-i", merged_video,
                "-i", audio_path,
                "-c:v", "copy",
                "-c:a", "aac",
                "-shortest",
                final_video
            ])

            # 6) GCS 업로드
            upload_gs(final_video, output)

        # 7) 성공 응답
        return jsonify({
            "ok": True,
            "output": output,
            "videoCount": len(videos)
        }), 200

    except subprocess.CalledProcessError as e:
        return jsonify({
            "ok": False,
            "error": "ffmpeg failed",
            "detail": str(e)
        }), 500

    except Exception as e:
        return jsonify({
            "ok": False,
            "error": "internal error",
            "detail": str(e)
        }), 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
